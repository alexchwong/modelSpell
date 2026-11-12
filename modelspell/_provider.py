"""Explicit OpenRouter provider selection. Selection lifetime belongs to the caller."""
from __future__ import annotations

import math
import threading
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from . import _http
from ._cancel import CancelHandle
from ._complete import check_settings, complete
from ._discovery import _call, _list, _providers
from ._json import FrozenMap, JSONValueError, freeze, thaw
from ._tools import ToolDefinition, check_tool_request, complete_with_tools
from ._types import Endpoint
from .errors import Cancelled, ConfigError, ModelSpellError, ResponseFormatError

_FIELDS = frozenset({"only", "ignore", "quantizations", "max_price", "zdr", "data_collection",
                     "require_parameters", "allow_fallbacks"})
_PRICE_UNITS = {"prompt": 1_000_000, "completion": 1_000_000, "request": 1, "image": 1}
_FINISHES = ("stop", "length", "tool_calls", "function_call", "content_filter", "error")
_PROBE_TOOL = ToolDefinition("route_check", "Inert provider acceptance check.",
                             {"type": "object", "properties": {}})
_PROBE_PROMPT = "Reply OK."


@dataclass(frozen=True)
class ProviderSelection:
    """Requested slug, effective immutable JSON policy, and bounded selection evidence."""

    slug: str | None
    policy: Mapping[str, Any] | None
    evidence: Mapping[str, Any]

    def __post_init__(self):
        if self.policy is not None and not isinstance(self.policy, FrozenMap):
            object.__setattr__(self, "policy", freeze(self.policy))
        if not isinstance(self.evidence, FrozenMap):
            object.__setattr__(self, "evidence", freeze(self.evidence))


class ProviderSelectionError(ModelSpellError):
    """Selection failed. Evidence contains a fixed reason and safe routing observations."""

    def __init__(self, reason: str, evidence: Mapping[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence
        self.sent: bool | None = None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str) and len(value) > 96:
        return None
    try:
        number = Decimal(str(value))
        if number.is_finite() and number >= 0 and math.isfinite(float(number)):
            if number == 0:
                return Decimal(0)
            if float(number) > 0:
                return number
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return None


def _field(obj, key):
    return obj.get(key) if isinstance(obj, Mapping) else None


def _selector_json(value):
    if isinstance(value, Decimal):
        value = float(value)
        if not math.isfinite(value):
            raise ConfigError("provider policy must contain finite JSON numbers")
    if isinstance(value, Mapping):
        return {k: _selector_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_selector_json(v) for v in value]
    return value


def _contains_secret(value, key):
    if isinstance(value, str):
        return key in value
    if isinstance(value, Mapping):
        return any(key in k or _contains_secret(v, key) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(v, key) for v in value)
    return False


def _policy_input(policy, mode, key):
    if policy is not None and not isinstance(policy, Mapping):
        raise ConfigError("provider must be a mapping")
    try:
        policy = _selector_json(freeze({} if policy is None else policy))
    except JSONValueError:
        raise ConfigError("provider policy must contain JSON values") from None
    if key and _contains_secret(policy, key):
        raise ConfigError("provider selection inputs contain the API credential")
    if mode != "automatic":
        return policy
    for field in policy:
        if field not in _FIELDS or (field == "allow_fallbacks" and policy[field] is not False):
            raise ConfigError(f"provider.{field} is incompatible with Automatic; supported fields: "
                              + ", ".join(sorted(_FIELDS)))
    for field in ("only", "ignore", "quantizations"):
        if field in policy and (not isinstance(policy[field], list)
                                or not all(isinstance(v, str) and v.strip() for v in policy[field])):
            raise ConfigError(f"provider.{field} must be a list of non-empty strings")
    for field in ("zdr", "require_parameters"):
        if field in policy and not isinstance(policy[field], bool):
            raise ConfigError(f"provider.{field} must be a boolean")
    if "data_collection" in policy and policy["data_collection"] not in ("allow", "deny"):
        raise ConfigError("provider.data_collection must be allow or deny")
    if "max_price" in policy:
        caps = policy["max_price"]
        if not isinstance(caps, dict) or any(k not in _PRICE_UNITS or _number(v) is None for k, v in caps.items()):
            raise ConfigError("provider.max_price must map prompt, completion, request or image to nonnegative caps")
    return policy


def _pin(policy, slug, tool, *, automatic):
    effective = {**policy, "order": [slug], "allow_fallbacks": False}
    if automatic and "only" in policy:
        effective["only"] = [slug]
    if tool:
        if "require_parameters" in effective and effective["require_parameters"] is not True:
            raise ConfigError("tool requests require provider.require_parameters=True")
        effective["require_parameters"] = True
    return freeze(effective)


def _matches(prefix, slug):
    prefix, slug = prefix.lower(), slug.lower()
    return slug == prefix or slug.startswith(prefix + "/")


def _p50(row, field):
    return _number(_field(row.raw.get(field), "p50"))


def _metrics(row, input_tokens, output_tokens):
    prices = row.pricing.raw if row.pricing else {}
    prompt, completion, request = (_number(prices.get(k)) for k in ("prompt", "completion", "request"))
    discount = _number(row.raw.get("discount", prices.get("discount")))
    cost = None
    if prompt is not None and completion is not None and ("request" not in prices or request is not None):
        cost = prompt * input_tokens + completion * output_tokens + (request or 0)
        if discount is not None and discount <= 1:
            cost *= 1 - discount
    speed = _p50(row, "throughput_last_30m")
    latency = _p50(row, "latency_last_30m")
    duration = latency + Decimal(output_tokens) / speed if latency is not None and speed else None
    return prices, cost, speed, latency, duration


def _quantity(value):
    if value is None:
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _exclusion(row, overlaps, policy, required, prices, input_tokens, output_tokens):
    if not row.slug:
        return "missing tag"
    if overlaps:
        return "ambiguous tag"
    if "only" in policy and not any(_matches(v, row.slug) for v in policy["only"]):
        return "outside allowlist"
    if any(_matches(v, row.slug) for v in policy.get("ignore", ())):
        return "excluded by ignore"
    if "quantizations" in policy and (row.quantization or "").lower() not in {
        q.lower() for q in policy["quantizations"]
    }:
        return "quantization"
    if required.difference(row.supported_parameters):
        return "required parameters"
    if ((row.context_length is not None and input_tokens + output_tokens > row.context_length)
            or (row.max_completion_tokens is not None and output_tokens > row.max_completion_tokens)
            or (type(row.raw.get("max_prompt_tokens")) is int and input_tokens > row.raw["max_prompt_tokens"])):
        return "token limits"
    for key, limit in policy.get("max_price", {}).items():
        published = _number(prices.get(key))
        if published is not None and published * _PRICE_UNITS[key] > _number(limit):
            return "price cap"
    return None


def _rank(rows, policy, required, mode, floor, input_tokens, output_tokens, evidence):
    best = None
    ranked = []
    tags = Counter(row.slug.lower() for row in rows if row.slug)
    for row, snapshot in zip(rows, evidence["discovery"]):
        tag = row.slug.lower() if row.slug else None
        overlaps = tag and (tags[tag] > 1 or any(other.startswith(tag + "/") for other in tags))
        prices, cost, speed, latency, duration = _metrics(row, input_tokens, output_tokens)
        snapshot.update(p50_tps=_quantity(speed), p50_ttft=_quantity(latency), estimated_cost=_quantity(cost),
                        estimated_seconds=_quantity(duration),
                        published_prices={k: _quantity(_number(prices.get(k))) for k in _PRICE_UNITS},
                        discount=_quantity(_number(row.raw.get("discount", prices.get("discount")))))
        reason = _exclusion(row, overlaps, policy, required, prices, input_tokens, output_tokens)
        primary, secondary = (duration, cost) if mode == "fastest" else (cost, duration)
        if not reason and primary is None:
            reason = "missing ranking estimate"
        if not reason and speed is not None:
            best = speed if best is None else max(best, speed)
        if not reason and floor is not None and (speed is None or speed <= floor):
            reason = "throughput floor"
        if reason:
            snapshot["excluded"] = reason
            continue
        secondary = secondary if secondary is not None else Decimal("Infinity")
        key = ((primary, -speed, secondary, row.slug) if mode == "balanced"
               else (primary, secondary, row.slug))
        ranked.append((key, row))
    evidence["best_observed_tps"] = _quantity(best)
    return [row for _, row in sorted(ranked, key=lambda item: item[0])]


def _clean(value, key):
    if isinstance(value, str):
        return (value.replace(key, "***") if key else value)[:240]
    if isinstance(value, list):
        return [_clean(v, key) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v, key) for k, v in value.items()}
    return value


def _snapshot(row):
    retention = row.raw.get("safety_retention")
    if type(retention) is not bool and row.raw.get("data_policy") in (
            "safety_retention", "zero_data_retention", "retention_and_training"):
        retention = row.raw["data_policy"] == "safety_retention"
    return {"slug": row.slug, "provider_name": row.raw.get("provider_name")
            if isinstance(row.raw.get("provider_name"), str) else None,
            "quantization": row.quantization, "safety_retention":
            "yes" if retention is True else "no" if retention is False else "unknown"}


class _Selection:
    def __init__(self, endpoint, model, evidence, key, deadline, caller, handle):
        self.endpoint = endpoint
        self.model = model
        self.evidence = evidence
        self.key = key
        self.deadline = deadline
        self.caller = caller
        self.handle = handle

    def frozen_evidence(self):
        return freeze(_clean(self.evidence, self.key))

    def fail(self, reason):
        raise ProviderSelectionError(_clean(reason, self.key), self.frozen_evidence()) from None

    def remaining(self):
        if self.caller is not None and self.caller.cancelled:
            raise Cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.handle.cancelled:
            self.fail("deadline expired")
        return remaining

    def get(self, path):
        call = _call(self.endpoint, f"{self.endpoint.base_url}/{path}", self.remaining())
        doc = _http.watched_get_json(call, self.endpoint.headers, self.handle)
        self.remaining()
        return _list(doc, "data")

    def optional(self, path):
        try:
            return self.get(path), "available"
        except Cancelled as exc:
            self.check_after_cancel(exc)
            raise
        except ProviderSelectionError:
            raise
        except ModelSpellError as exc:
            self.remaining()
            status = getattr(exc, "status", None)
            if isinstance(exc, ResponseFormatError):
                return None, "invalid response"
            return None, f"HTTP {status}" if status is not None else type(exc).__name__

    def check_after_cancel(self, exc):
        if self.caller is not None and self.caller.cancelled:
            raise exc
        try:
            self.remaining()
        except ProviderSelectionError as error:
            error.sent = exc.sent
            raise error from None

    def pool(self, rows, policy):
        if policy.get("zdr") is not True:
            return [("ranked", row) for row in rows]
        catalogue, outcome = self.optional("endpoints/zdr")
        pairs = {(v["model_id"], v["tag"]) for v in catalogue or []
                 if isinstance(v, dict) and all(isinstance(v.get(k), str) and v[k] for k in ("model_id", "tag"))}
        if catalogue is not None and any(
                not isinstance(v, dict) or not all(isinstance(v.get(k), str) and v[k]
                                                   for k in ("model_id", "tag")) for v in catalogue):
            catalogue, outcome = None, "invalid response"
        self.evidence["catalogue"] = outcome
        if catalogue is None:
            return [("fallback", row) for row in rows]
        identities = {row.slug: row.raw.get("model_id") for row in rows}
        missing = [slug for slug, value in identities.items() if not isinstance(value, str) or not value]
        if missing:
            models, metadata_outcome = self.optional("models")
            matches = {row["canonical_slug"] for row in models or [] if isinstance(row, dict)
                       and row.get("id") == self.model and isinstance(row.get("canonical_slug"), str)
                       and row["canonical_slug"]}
            canonical = next(iter(matches)) if len(matches) == 1 else None
            self.evidence["model_metadata"] = metadata_outcome
            for slug in missing:
                identities[slug] = canonical
        unresolved = [slug for slug, value in identities.items() if not value]
        preferred = [row for row in rows if identities[row.slug] and (identities[row.slug], row.slug) in pairs]
        self.evidence.update(unresolved_canonical_identity=unresolved,
                             catalogue_matches=[{"slug": row.slug, "permaslug": identities[row.slug]}
                                                for row in preferred])
        return ([("catalogue", row) for row in preferred]
                + [("fallback", row) for row in rows if row not in preferred])

    def probe(self, rows, policy, tools, settings, cap):
        attempts = self.evidence["attempts"]
        pool_rows = self.pool(rows, policy)
        self.evidence["probe_order"] = [{"pool": pool, "slug": row.slug} for pool, row in pool_rows
                                        if self.key not in row.slug]
        for pool, row in pool_rows:
            if self.key in row.slug:
                continue
            self.remaining()
            if len(attempts) >= cap:
                self.fail("attempt budget exhausted")
            effective = _pin(policy, row.slug, tools, automatic=True)
            kwargs = dict(settings, provider=effective, cancel=self.handle)
            if tools:
                kwargs["tools"] = (_PROBE_TOOL,)
            retries = 0
            while len(attempts) < cap:
                kwargs["timeout"] = self.remaining()
                attempt = {"slug": row.slug, "pool": pool, "reserved": True, "sent": False,
                           "stream": False, "tools": tools, "retry": retries, "outcome": "interrupted"}
                attempts.append(attempt)
                with _http.count_posts() as sent:
                    try:
                        result = (complete_with_tools if tools else complete)(
                            self.endpoint, self.model, _PROBE_PROMPT, **kwargs)
                        result = result.result if tools else result
                    except Cancelled as exc:
                        attempt["sent"] = exc.sent
                        self.check_after_cancel(exc)
                        raise
                    except ModelSpellError as exc:
                        attempt["sent"] = bool(sent[0])
                        self.remaining()
                        status = getattr(exc, "status", None)
                        reason = getattr(exc, "_routing_reason", None) or _http.routing_reason(
                            getattr(exc, "body", ""))
                        if isinstance(exc, ResponseFormatError):
                            diagnostic = dict(exc.diagnostic)
                            if diagnostic.get("reported_provider") not in {item.name for item in rows}:
                                diagnostic["reported_provider"] = None
                            attempt.update(outcome="response rejected", diagnostic=diagnostic)
                            break
                        attempt.update(status=status, outcome=reason or (
                            "forbidden — reason unavailable" if status == 403 else
                            "no route" if status == 404 else type(exc).__name__))
                        if (status in (401, 402, 403)
                                or (status == 404 and reason == "model or account")):
                            self.fail(attempt["outcome"])
                        if status != 429:
                            break
                        if reason == "provider rate limit":
                            break
                        delay = max(min(2 ** retries, 4), getattr(exc, "_retry_after", None) or 0)
                        if len(attempts) == cap or delay >= self.remaining():
                            self.fail("rate limit")
                        attempt["retry_scheduled_seconds"] = delay
                        if _wait(self.handle, delay):
                            self.remaining()
                        retries += 1
                        continue
                    attempt["sent"] = bool(sent[0])
                self.remaining()
                finish = result.finish_reason
                attempt.update(finish_reason=finish if finish in _FINISHES else None, cost=_quantity(result.cost),
                               reported_provider=result.provider)
                if finish == "content_filter":
                    attempt["outcome"] = "policy block"
                    break
                if finish not in ("stop", "length", "tool_calls", "function_call"):
                    attempt.update(outcome="response rejected", diagnostic={
                        "parser": "terminal finish not established", "structure": None, "finish_reason": None})
                    break
                attempt["outcome"] = "accepted"
                self.evidence.update(pool=pool, requested_slug=row.slug, reported_provider=result.provider,
                                     served_variant=None, quantization=row.quantization,
                                     scope="specific as published", safety_retention=_snapshot(row)["safety_retention"])
                return ProviderSelection(row.slug, effective, self.frozen_evidence())
        self.fail("attempt budget exhausted" if len(attempts) >= cap else "no route confirmed")


def _wait(handle, seconds):
    from ._cancel import _Stop, _Watch

    try:
        with _Watch(handle) as watch:
            watch.pause(seconds)
    except _Stop:
        return True
    return False


def choose_provider(
    endpoint: Endpoint,
    model: str,
    *,
    mode: str = "automatic",
    pin: str | None = None,
    provider: Mapping[str, Any] | None = None,
    preset: str = "balanced",
    balanced_min_tps: float | None = None,
    tools: bool = False,
    settings: Mapping[str, Any] | None = None,
    input_tokens: int = 600,
    output_tokens: int = 200,
    timeout: float = 30,
    max_attempts: int = 4,
    cancel: CancelHandle | None = None,
) -> ProviderSelection:
    """Resolve OpenRouter routing for one conversation or compatible run.

    Managed performs no network request. Manual validates the exact pin using discovery.
    Automatic confirms a ranked pin with a synthetic probe. Settings are generation
    arguments shared with the intended completion, with a default probe cap of 64.
    """
    if not isinstance(endpoint, Endpoint) or endpoint.kind != "openrouter":
        raise ConfigError("choose_provider requires an OpenRouter Endpoint")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("model must be a non-empty string")
    if mode != "managed" and ("/" not in model or not all(model.split("/", 1))):
        raise ConfigError("model must have the form 'author/slug'")
    if mode not in ("managed", "manual", "automatic") or preset not in ("cheapest", "balanced", "fastest"):
        raise ConfigError("use managed, manual or automatic and cheapest, balanced or fastest")
    if (mode == "manual" and (not isinstance(pin, str) or not pin.strip())) or (mode != "manual" and pin is not None):
        raise ConfigError("an explicit pin is required only in manual mode")
    if type(tools) is not bool:
        raise ConfigError("tools must be a boolean")
    if (type(max_attempts) is not int or not 1 <= max_attempts <= 4
            or any(type(v) is not int or not 1 <= v <= 10**9 for v in (input_tokens, output_tokens))):
        raise ConfigError("max_attempts must be 1–4 and token estimates must be positive integers up to one billion")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not _number(timeout) or timeout > threading.TIMEOUT_MAX):
        raise ConfigError("timeout must be a finite positive number within the platform timer limit")
    deadline = time.monotonic() + timeout
    if cancel is not None and not isinstance(cancel, CancelHandle):
        raise ConfigError("cancel must be a CancelHandle")
    if settings is not None and not isinstance(settings, Mapping):
        raise ConfigError("settings must be a mapping")
    settings = dict(settings or {})
    from ._complete import _SAMPLING
    if any(k not in (*_SAMPLING, "reasoning") for k in settings):
        raise ConfigError("settings must contain only supported generation arguments")
    key = endpoint._resolve_key()
    if key and ((pin and key in pin) or key in model):
        raise ConfigError("provider selection inputs contain the API credential")
    policy = _policy_input(provider, mode, key)
    floor = {"cheapest": Decimal(20), "balanced": Decimal(50), "fastest": None}[preset]
    if balanced_min_tps is not None:
        if mode != "automatic" or preset != "balanced" or not isinstance(balanced_min_tps, (int, float)) \
                or not _number(balanced_min_tps):
            raise ConfigError("balanced_min_tps requires Automatic Balanced and a finite positive number")
        floor = _number(balanced_min_tps)
    if tools:
        if mode == "managed":
            raise ConfigError("OpenRouter tool requests require Automatic or Manual pin")
        provisional = _pin(policy, pin or "probe", True, automatic=mode == "automatic")
        # An automatic candidate is filtered against the original ignore list after discovery.
        if mode == "automatic":
            provisional = freeze({k: v for k, v in provisional.items() if k != "ignore"})
        check_tool_request("openrouter", tools=(_PROBE_TOOL,), provider=provisional, **settings)
    else:
        check_settings("openrouter", provider=policy, **settings)
    evidence = {"model": model, "mode": mode, "preset": preset if mode == "automatic" else None,
                "threshold": _quantity(floor) if mode == "automatic" else None, "pin_source": mode,
                "catalogue": "not requested", "attempts": [], "input_tokens": input_tokens,
                "output_tokens": output_tokens, "requested_slug": pin, "reported_provider": None,
                "served_variant": None, "reasoning": settings.get("reasoning")}
    handle = CancelHandle()
    selection = _Selection(endpoint, model, evidence, key, deadline, cancel, handle)
    if mode == "managed":
        selection.remaining()
        return ProviderSelection(None, freeze(policy) if provider is not None else None, selection.frozen_evidence())
    if not key:
        selection.fail("missing API credential")
    timer = threading.Timer(selection.remaining(), handle.cancel)
    timer.daemon = True
    with cancel._link(handle) if cancel is not None else nullcontext():
        timer.start()
        try:
            try:
                rows = _providers(endpoint, model, selection.remaining(), handle)
            except Cancelled as exc:
                selection.check_after_cancel(exc)
                raise
            except ProviderSelectionError:
                raise
            except ModelSpellError as exc:
                selection.remaining()
                evidence["discovery_status"] = getattr(exc, "status", None)
                selection.fail("discovery failed")
            selection.remaining()
            evidence["discovery"] = [_snapshot(row) for row in rows]
            if mode == "manual":
                matches = [row for row in rows if row.slug == pin]
                matches_scope = [row for row in rows if row.slug and _matches(pin, row.slug)]
                if not matches and ("/" in pin or not matches_scope):
                    available = ", ".join(sorted({row.slug for row in rows if row.slug}))
                    selection.fail("invalid manual pin; available slugs: " + _clean(available, key))
                representative = (matches or matches_scope)[0]
                evidence.update(scope="provider-level" if len(matches_scope) != 1 or "/" not in pin
                                else "specific as published", quantization=representative.quantization
                                if len(matches_scope) == 1 else None, variant_count=len(matches_scope),
                                safety_retention=_snapshot(representative)["safety_retention"]
                                if len(matches_scope) == 1 else "unknown")
                selection.remaining()
                return ProviderSelection(pin, _pin(policy, pin, tools, automatic=False),
                                         selection.frozen_evidence())
            required = {"tools"} if tools else set()
            if tools or policy.get("require_parameters"):
                required.update(k for k, v in settings.items() if v is not None)
            ranked = _rank(rows, policy, required, preset, floor, input_tokens, output_tokens, evidence)
            if not ranked:
                selection.remaining()
                selection.fail("no eligible route")
            settings = {**settings, "max_tokens": settings.get("max_tokens") or 64}
            chosen = selection.probe(ranked, policy, tools, settings, max_attempts)
            selection.remaining()
            return chosen
        finally:
            timer.cancel()
