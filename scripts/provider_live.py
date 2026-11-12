"""Live cases shared by the verification command and targeted checks."""
from __future__ import annotations

import math
import os
import secrets

from modelspell import (ConfigError, Endpoint, Message, ModelSpellError, ProviderSelectionError,
                        ResponseFormatError, ToolDefinition, ToolOutput, choose_provider, complete,
                        complete_with_tools, supported_settings)
from modelspell._http import count_posts
from modelspell._json import thaw

MODELS = ("z-ai/glm-5.3-flash", "openai/gpt-oss-120b", "minimax/minimax-m3.1-flash", "qwen/qwen3.8-27b")
VT = "Automatic Balanced ZDR tool round trip / VT"
LIVE_CASES = (VT, "OpenRouter-managed", "Automatic Balanced", "Manual / V1", "Automatic Cheapest",
              "Automatic Fastest", "Automatic Balanced override", "V2 settings matrix")
REASONING = None  # preserve the runtime/backend default on every turn and on the confirmation
PROMPT = "Reply OK."
V2_VALUES = dict(temperature=0.7, max_tokens=64, top_p=0.9, top_k=20, min_p=0.05, repetition_penalty=1.1,
                 presence_penalty=0.5, frequency_penalty=0.5, seed=42, stop=["\n\n\n\n"])
V2_BOUNDS = [("temperature", 2), ("temperature", 2.01), ("repetition_penalty", 2),
             ("repetition_penalty", 2.01), *[(name, value)
             for name in ("presence_penalty", "frequency_penalty") for value in (-2, -2.01, 2, 2.01)]]
FINISHES = ("stop", "length", "tool_calls", "function_call", "content_filter", "error")
READ_TOOL = ToolDefinition("read_label", "Read the label of the fixture item.",
                           {"type": "object", "properties": {}, "additionalProperties": False})
OTHER_TOOL = ToolDefinition("read_shape", "Read the shape of the fixture item.",
                            {"type": "object", "properties": {}, "additionalProperties": False})


def attribution(slug, rows, reported, scope=None):
    """Attribute a response using exact discovery tags and published provider names."""
    def normal(value):
        return value.strip().casefold() if isinstance(value, str) else ""

    def provider_root(value):
        return normal(value).split("/", 1)[0]

    def under(value):
        return normal(value) == normal(slug) or (
            scope == "provider-level" and normal(value).startswith(normal(slug) + "/"))

    matched = [normal(r.get("provider_name")) for r in rows if under(r.get("slug"))]
    expected = set(matched)
    actual = normal(reported)
    if len(expected) != 1 or "" in expected:
        return "unverified", "ambiguous or missing expected provider"
    if not actual:
        return "unverified", "missing reported provider"
    roots = {}
    for row in rows:
        name = normal(row.get("provider_name"))
        if name:
            roots.setdefault(name, set()).add(provider_root(row.get("slug")))
    expected_name = next(iter(expected))
    if actual == expected_name and len(roots[actual]) == 1:
        return "confirmed", None
    if actual not in roots:
        return "unverified", "unrecognised provider name"
    if (len(roots[actual]) != 1 or len(roots[expected_name]) != 1
            or roots[actual] == roots[expected_name] or "" in roots[actual] or "" in roots[expected_name]):
        return "unverified", "ambiguous provider name"
    return "conflicting", "discovery identifies a different provider"


class Blocked(Exception):
    pass


class Incorrect(Exception):
    pass


class LiveRun:
    def __init__(self, report, release):
        self.report = report
        self.release = release
        self.endpoint = Endpoint("openrouter")
        self.budget = 64
        self.global_stop = False
        self.case = None

    def reserve(self, sent, reserved=1):
        self.case["posts"] += sent
        self.case["reserved"] += reserved
        self.budget -= reserved

    def incident(self, row, diagnostic):
        build = self.report["build"]
        index = len(self.report["incidents"]) + 1
        identity = "/".join(build[k] for k in ("commit", "version", "wheel_sha256"))
        incident = {**row, "diagnostic": thaw(diagnostic), "build": build, "run_id": self.report["run_id"],
                    "id": f"{self.report['run_id']}/{identity}/{index}", "case": self.case["name"]}
        self.report["incidents"].append(incident)
        return incident["id"]

    def case_run(self, name, action):
        self.case = {"name": name, "observations": [], "posts": 0, "reserved": 0}
        self.report["cases"].append(self.case)
        first = len(self.report["incidents"])
        try:
            if self.global_stop:
                raise Blocked("account restriction")
            action()
        except Incorrect as exc:
            self.case.update(state="FAIL", reason=str(exc))
        except Blocked as exc:
            self.case.update(state="BLOCKED", reason=str(exc))
        except Exception as exc:
            # Keep exception type for debugging. Exception messages may contain response bodies.
            self.case.update(state="FAIL", reason=f"verification raised {type(exc).__name__}")
        else:
            self.case.update(state="PASS", reason=None)
        self.case["observed_state"] = self.case["state"]
        self.case["observed_reason"] = self.case["reason"]
        if self.case["state"] != "FAIL" and len(self.report["incidents"]) > first:
            self.case.update(state="BLOCKED", reason="response rejected — triage required")

    def policy(self, *, named=False):
        return {"max_price": {"prompt": self.release["max_input_price"] if named else 0.50,
                              "completion": self.release["max_output_price"] if named else 1.50,
                              "request": 0}, **({"zdr": True} if named else {})}

    def selection(self, model, *, reserve=0, **kw):
        cap = min(4, self.budget - reserve)
        if cap < 1:
            raise Blocked("budget exhausted")
        row = {"model": model, "phase": "selection", "preset": kw.get("preset", "balanced")}
        self.case["observations"].append(row)
        try:
            selected = choose_provider(self.endpoint, model, max_attempts=cap, **kw)
        except ProviderSelectionError as exc:
            evidence = thaw(exc.evidence)
            row.update(outcome="blocked", reason=exc.reason)
            selected = None
        else:
            evidence = thaw(selected.evidence)
            row.update(outcome="accepted", pin=selected.slug, policy=thaw(selected.policy))
        row["evidence"] = evidence
        attempts = evidence["attempts"]
        self.reserve(sum(a["sent"] is True for a in attempts), len(attempts))
        for number, attempt in enumerate(attempts, 1):
            if attempt["outcome"] == "response rejected":
                attempt["incident"] = self.incident(
                    {"model": model, "pin": attempt["slug"], "stream": False, "phase": "probe",
                     "turn": number, "transport": "chat_completions",
                     "provider": attempt["diagnostic"].get("reported_provider")}, attempt["diagnostic"])
        if selected is None:
            statuses = [evidence.get("discovery_status"), *(a.get("status") for a in attempts)]
            if (401 in statuses or 402 in statuses or row["reason"] == "missing API credential"
                    or any(a["outcome"] == "budget" for a in attempts)):
                self.global_stop = True
            if self.global_stop or 403 in statuses:
                raise Blocked("account restriction")
            if any(status is not None and status >= 500 for status in statuses) or row["reason"] in (
                    "deadline expired", "rate limit"):
                raise Blocked("upstream outage")
            if row["reason"] == "attempt budget exhausted" and self.budget <= reserve:
                raise Blocked("budget exhausted")
            raise Blocked("no eligible route" if row["reason"] in ("no eligible route", "discovery failed")
                          or all(a.get("status") == 404 for a in attempts) else "upstream outage")
        if attempts:
            accepted = [a["slug"] for a in attempts if a["outcome"] == "accepted"]
            eligible = [r for r in evidence["discovery"] if r["slug"] == selected.slug and "excluded" not in r]
            if accepted != [selected.slug] or len(eligible) != 1 or attempts[-1]["outcome"] != "accepted":
                raise Incorrect("selection differs from the recorded acceptance order or eligible snapshot")
            actual_order = [(a["pool"], a["slug"]) for a in attempts if a["retry"] == 0]
            expected_order = [(r["pool"], r["slug"]) for r in evidence["probe_order"]]
            if actual_order != expected_order[:len(actual_order)]:
                raise Incorrect("probe sequence differs from the recorded ranking")
            floor = evidence["threshold"]
            if floor is not None and (eligible[0]["p50_tps"] is None or eligible[0]["p50_tps"] <= floor):
                raise Incorrect("automatic selection violated its throughput floor")
            if selected.policy["order"] != (selected.slug,) or selected.policy["allow_fallbacks"] is not False:
                raise Incorrect("automatic selection returned a non-strict pin")
            probe = attempts[-1]
            state, reason = attribution(selected.slug, evidence["discovery"], probe["reported_provider"],
                                        evidence["scope"])
            probe.update(attribution=state, attribution_reason=reason, served_variant=None)
            if state == "conflicting":
                raise Incorrect("conflicting provider attribution on confirmation probe")
        return selected

    def request(self, model, *, selection=None, tools=None, history=PROMPT, stream=False, turn=None,
                replay=False, setting=None, **kw):
        if self.budget < 1:
            raise Blocked("budget exhausted")
        row = {"model": model, "stream": stream, "turn": turn, "transport": "chat_completions",
               "pin": selection.slug if selection else None, "reasoning": REASONING}
        if setting is not None:
            row["setting"], row["value"], row["via"] = setting
        self.case["observations"].append(row)
        if selection:
            kw["provider"] = selection.policy
        if stream:
            kw["on_text"] = lambda text: None
        if tools:
            kw["tools"] = tools
        with count_posts() as sent:
            try:
                out = (complete_with_tools if tools else complete)(
                    self.endpoint, model, history, timeout=90, reasoning=REASONING, **kw)
            except ResponseFormatError as exc:
                diagnostic = dict(exc.diagnostic)
                candidate = diagnostic.pop("reported_provider", None)
                published = [r.get("provider_name") for r in selection.evidence.get("discovery", ())] if selection else []
                row.update(outcome="response rejected", status=exc.status,
                           provider=candidate if candidate in published else None)
                row["incident"] = self.incident(row, diagnostic)
                raise Blocked("response rejected — triage required") from None
            except ConfigError:
                if replay:
                    raise Incorrect("modelSpell refused locally constructed same-policy replay") from None
                raise
            except ModelSpellError as exc:
                status = getattr(exc, "status", None)
                reason = getattr(exc, "_routing_reason", None)
                row.update(outcome="request rejected", status=status, reason=reason, error=type(exc).__name__)
                if status in (401, 402) or reason == "budget":
                    self.global_stop = True
                if replay and reason == "replay validation":
                    raise Incorrect("upstream rejected same-policy replay") from None
                if (setting is not None and reason == "request validation" and status in (400, 422)
                        and getattr(exc, "_routing_parameter", None) == setting[0]):
                    row.update(outcome="specifically refused")
                    return None
                if setting is not None:
                    row.update(outcome="unrelated and unverified")
                raise Blocked("account restriction" if status in (401, 402, 403) else
                              "no eligible route" if status == 404 else "upstream outage") from None
            finally:
                self.reserve(sent[0])
        result = out.result if tools else out
        row.update(outcome="accepted", provider=result.provider, served_variant=None,
                   finish_reason=result.finish_reason if result.finish_reason in FINISHES else None,
                   cost=result.cost if result.cost is not None and math.isfinite(result.cost) else None)
        if setting is not None:
            row["effect"] = "unverified"
        if selection and selection.slug is not None:
            state, reason = attribution(selection.slug, selection.evidence["discovery"], result.provider,
                                        selection.evidence["scope"])
            row.update(attribution=state, attribution_reason=reason)
            if state == "conflicting":
                raise Incorrect("conflicting provider attribution")
        return out, row

    def vt_turns(self, model, selection, stream):
        challenge = secrets.token_hex(4)
        prompt = [Message("user", "Call read_label to find the item label. Then reply with the exact label only.")]
        kwargs = dict(selection=selection, stream=stream, max_tokens=2048)
        first, first_row = self.request(model, tools=[READ_TOOL, OTHER_TOOL], history=prompt, turn=1, **kwargs)
        if not first.clean:
            return "truncation" if first.result.truncated else "another diagnostic finish"
        if not first.tool_calls:
            return "no tool call"
        calls = first.tool_calls
        if not any(call.name == READ_TOOL.name for call in calls):
            return "no requested tool call"
        first_row["requested_tool_called"] = True
        history = [*prompt, first, *(ToolOutput(call.id, challenge if call.name == READ_TOOL.name else "unavailable")
                                    for call in calls)]
        follow, follow_row = self.request(model, tools=[READ_TOOL, OTHER_TOOL], history=history, turn=2,
                                          replay=True, **kwargs)
        if not follow.clean:
            return "truncation" if follow.result.truncated else "another diagnostic finish"
        follow_row["used_tool_result"] = not follow.tool_calls and follow.result.text.strip() == challenge
        if not follow_row["used_tool_result"]:
            return "continuation that did not use the tool result"
        self.request(model, tools=[READ_TOOL], history=[*history, Message("user", "Reply with the label only.")],
                     turn=3, replay=True, **kwargs)
        return None

    def named(self):
        models = (self.release["model"],) if self.release["model"] else MODELS
        reason = "no eligible route"
        for model in models:
            if self.budget < 7:
                raise Blocked("budget exhausted")
            try:
                selection = self.selection(model, reserve=6, tools=True, provider=self.policy(named=True),
                                           output_tokens=2048, settings={"reasoning": REASONING, "max_tokens": 128})
            except Blocked as exc:
                reason = str(exc)
                if self.global_stop:
                    break
                continue
            model_record = {"model": model, "pin": selection.slug, "phase": "VT",
                            "transport": "chat_completions", "base_url": self.endpoint.base_url,
                            "policy": thaw(selection.policy), "scope": selection.evidence["scope"],
                            "quantization": selection.evidence["quantization"], "reasoning": REASONING,
                            "sequences": []}
            self.case["observations"].append(model_record)
            for stream in (False, True):
                try:
                    subcause = self.vt_turns(model, selection, stream)
                except Blocked as exc:
                    reason = str(exc)
                    model_record["sequences"].append({"stream": stream, "reason": reason})
                    if self.global_stop:
                        break
                else:
                    model_record["sequences"].append({"stream": stream, "reason": subcause})
                    if subcause:
                        reason = "model behaviour — tool round trip not observed"
            if len(model_record["sequences"]) == 2 and not any(x["reason"] for x in model_record["sequences"]):
                self.report["complete_vt"] = True
                return
            if self.global_stop:
                break
        raise Blocked(reason)

    def first_model(self, action):
        reason = "no eligible route"
        for model in MODELS:
            try:
                return model, action(model)
            except Blocked as exc:
                reason = str(exc)
                if self.global_stop or reason == "budget exhausted":
                    break
        raise Blocked(reason)

    def ordinary(self, preset="balanced", **kw):
        return self.first_model(lambda model: self.selection(
            model, preset=preset, provider=self.policy(), settings={"reasoning": REASONING, "max_tokens": 64}, **kw))

    def managed(self):
        def run(model):
            selection = choose_provider(self.endpoint, model, mode="managed", provider=self.policy())
            return self.request(model, selection=selection, max_tokens=64)
        self.first_model(run)

    def manual(self, baseline):
        if baseline is None:
            raise Blocked("compatible automatic baseline unavailable")
        model, auto = baseline
        selection = self.selection(model, mode="manual", pin=auto.slug, provider=auto.policy)
        _, row = self.request(model, selection=selection, max_tokens=64)
        row.update(pin_source="manual", pin_origin="automatic", variant_count=selection.evidence["variant_count"])
        if row["attribution"] != "confirmed":
            raise Blocked("provider attribution unverified")

    def override(self):
        try:
            self.ordinary(balanced_min_tps=100)
        except Blocked:
            if self.global_stop or self.budget < 1:
                raise
            speeds = [r["p50_tps"] for row in self.case["observations"]
                      for r in row.get("evidence", {}).get("discovery", [])
                      if r.get("p50_tps") and r.get("excluded") in (None, "throughput floor")]
            if not speeds:
                raise
            # A diagnostic override is explicit in evidence and leaves the named gate untouched.
            self.ordinary(balanced_min_tps=min(99.0, max(speeds) * 0.9))

    def v2(self, baseline):
        if baseline is None:
            raise Blocked("compatible minimal baseline unavailable")
        model, selection = baseline
        if self.budget < 1:
            raise Blocked("budget exhausted")
        self.case["observations"].append({"model": model, "pin": selection.slug,
                                         "phase": "baseline", "origin": "Balanced confirmation",
                                         "policy": thaw(selection.policy)})
        names = sorted(supported_settings("openrouter") - {"provider", "reasoning"})
        arguments = [(name, V2_VALUES[name], "argument") for name in names]
        extras = [(name, value, "extra") for name, value in V2_BOUNDS]
        reasons = []
        for name, value, via in [*arguments, *extras]:
            kw = {"max_tokens": 64, name: value} if via == "argument" else {
                "max_tokens": 64, "extra": {name: value}}
            try:
                self.request(model, selection=selection, setting=(name, value, via), **kw)
            except Blocked as exc:
                reasons.append(str(exc))
                if self.global_stop or str(exc) == "budget exhausted":
                    break
        if reasons:
            raise Blocked(reasons[0])

    def run(self):
        self.case_run(VT, self.named)
        self.case_run("OpenRouter-managed", self.managed)
        baseline = []

        def balanced():
            baseline.append(self.ordinary())

        self.case_run("Automatic Balanced", balanced)
        selected = baseline[0] if baseline else None
        self.case_run("Manual / V1", lambda: self.manual(selected))
        for preset in ("cheapest", "fastest"):
            self.case_run(f"Automatic {preset.title()}", lambda: self.ordinary(preset))
        self.case_run("Automatic Balanced override", self.override)
        self.case_run("V2 settings matrix", lambda: self.v2(selected))


def worker(report):
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        for name in LIVE_CASES:
            reason = "account restriction — no funded credential"
            report["cases"].append({"name": name, "state": "BLOCKED", "observed_state": "BLOCKED",
                                    "observed_reason": reason, "reason": reason, "observations": [],
                                    "posts": 0, "reserved": 0})
    else:
        LiveRun(report, report["overrides"]).run()
    return report
