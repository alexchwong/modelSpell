"""Sampling probes for LM Studio: a diagnostic, not a test (modelSpell 0.2.1 technical specification v6, Section 13).

Sends a fixed set of requests through modelspell.complete() from this checkout and prints what came back:
one "P-CONFIG {...}" line, one "P {...}" JSON line per request as it returns, then a plain summary for a
person to read. It gives no verdicts, and nothing depends on its output. Model output is not reproducible,
so no release, support table or document is decided by it (M21-3).

A setting modelSpell sends on the selected route goes through its named argument; any other setting goes
through extra, once per candidate wire name.

    export MODELSPELL_LIVE_LMSTUDIO_URL=http://localhost:1234/v1
    export MODELSPELL_LIVE_LMSTUDIO_MODEL=<model key>
    python tests/probe_lmstudio.py --reasoning low 2>&1 | tee probe-low.txt
    python tests/probe_lmstudio.py --reasoning off 2>&1 | tee probe-off.txt   # native route

Probes, in run order: control, temperature, top_p, top_k, min_p, seed, repetition_penalty,
presence_penalty, frequency_penalty, stop. Select some with --probes top_k,min_p.
Exit status: 0 after the summary; 1 on a routing regression (a modelSpell defect); 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import sys
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # this checkout's modelspell
import modelspell  # noqa: E402
from modelspell import Endpoint, ModelSpellError, complete, list_models, supported_settings  # noqa: E402

KIND = "lmstudio"
REASONING = {"default": None, "off": "off", "low": "low", "medium": "medium", "high": "high"}
PROMPTS = {
    "lighthouse": "Write one unusual sentence about a lighthouse. Reply with the sentence only.",
    "count": "Count from 1 to 30, separated by commas. Reply with the numbers only.",
}
CONTROL_FIELD = "zz_not_a_field"
ALT_SEED = 43
STOP_REPORTED = ("responses", "chat_completions")  # the native route reports no finish reason (spec v6 §8.8)


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str                   # control | varied (temperature 1, N runs) | fixed (temperature 0, 2 runs)
    setting: str | None         # modelSpell argument name (None for the control)
    value: object               # treatment value
    prompt: str                 # key of PROMPTS
    temperature: float          # every arm; the temperature probe overrides it in the treatment arm
    wires: tuple[str, ...] = ()  # wire names tried through extra where modelSpell does not send the setting
    hint: str = ""              # what an applied setting usually looks like; a reading aid, not a criterion


PROBES = (
    Probe("control", "control", None, 1, "lighthouse", 1.0,
          hint="accepted means unknown fields are accepted, so acceptance of any field shows nothing"),
    Probe("temperature", "varied", "temperature", 0.0, "lighthouse", 1.0,
          hint="sent on every route; the treatment usually repeats one answer"),
    Probe("top_p", "varied", "top_p", 0.01, "lighthouse", 1.0, ("top_p",), "applied: usually one repeated answer"),
    Probe("top_k", "varied", "top_k", 1, "lighthouse", 1.0, ("top_k",), "applied: usually one repeated answer"),
    Probe("min_p", "varied", "min_p", 1.0, "lighthouse", 1.0, ("min_p",), "applied: usually one repeated answer"),
    Probe("seed", "varied", "seed", 42, "lighthouse", 1.0, ("seed",),
          "applied: usually one repeated answer, and seed 43 differs"),
    Probe("repetition_penalty", "fixed", "repetition_penalty", 2.0, "count", 0.0,
          ("repetition_penalty", "repeat_penalty"), "applied: usually differs from the baseline"),
    Probe("presence_penalty", "fixed", "presence_penalty", 2.0, "count", 0.0, ("presence_penalty",),
          "applied: usually differs from the baseline"),
    Probe("frequency_penalty", "fixed", "frequency_penalty", 2.0, "count", 0.0, ("frequency_penalty",),
          "applied: usually differs from the baseline"),
    Probe("stop", "fixed", "stop", ["17"], "count", 0.0, ("stop",), 'applied: the answer ends before "17"'),
)
BY_NAME = {p.name: p for p in PROBES}


class RoutingRegression(Exception):
    pass


# --- records ------------------------------------------------------------------------------------------

def _text(result_text: Any) -> str:
    return result_text.strip() if isinstance(result_text, str) else ""


def result_fields(result: Any) -> dict:
    text = _text(result.text)
    usage = result.usage
    return dict(returned_model=result.model, transport=result.transport, truncated=result.truncated,
                finish_reason=result.finish_reason,
                output_tokens=getattr(usage, "output_tokens", None),
                reasoning_tokens=getattr(usage, "reasoning_tokens", None),
                reasoning_chars=len(result.reasoning or ""),
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), text=text)


def error_fields(exc: BaseException) -> dict:
    return dict(error=type(exc).__name__, status=getattr(exc, "status", None), detail=str(exc)[:300])


def outcome(rec: Any) -> str:
    """The first that applies of: error, truncated, incomplete, blank, ok. Never raises."""
    get = rec.get if isinstance(rec, dict) else (lambda key: None)
    transport = get("transport")
    if get("error"):
        return "error"
    if get("truncated"):
        return "truncated"
    if isinstance(transport, str) and transport in STOP_REPORTED and get("finish_reason") != "stop":
        return "incomplete"
    if not _text(get("text")):
        return "blank"
    return "ok"


def model_config(models: list, requested: str) -> dict:
    """The model's LM Studio discovery entry: format (gguf/mlx), quantisation, load config, reasoning options."""
    keys = ("key", "publisher", "display_name", "architecture", "format", "quantization", "params_string",
            "size_bytes", "selected_variant", "max_context_length", "loaded_instances")
    for m in models:
        raw = getattr(m, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        instances = raw.get("loaded_instances") if isinstance(raw.get("loaded_instances"), list) else []
        if requested in {getattr(m, "id", None)} | {i.get("id") for i in instances if isinstance(i, dict)}:
            out = {k: raw[k] for k in keys if k in raw}
            caps = raw.get("capabilities")
            if isinstance(caps, dict) and "reasoning" in caps:
                out["reasoning_capability"] = caps["reasoning"]
            return out
    return {"error": f"{requested!r} not found among {len(models)} listed models"}


# --- running --------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    endpoint: Endpoint
    model: str
    reasoning: str | None
    transport: str
    runs: int
    max_tokens: int
    timeout: float
    session: str
    seq: int = 0


def variants(probe: Probe, reasoning: str | None) -> list[tuple[str, str | None]]:
    """(via, wire) pairs: the named argument where modelSpell sends the setting, else extra per wire name."""
    if probe.kind == "control":
        return [(f"extra:{CONTROL_FIELD}", CONTROL_FIELD)]
    if probe.setting in supported_settings(KIND, reasoning):
        return [("argument", None)]
    return [(f"extra:{w}", w) for w in probe.wires]


def _kwargs(probe: Probe, via: str, wire: str | None, value: object) -> dict:
    return {probe.setting: value} if via == "argument" else {"extra": {wire: value}}


def plan(probe: Probe, via: str, wire: str | None, runs: int) -> list[tuple[str, object, str | None, dict]]:
    """(arm, value, via, kwargs) per request, in order; baseline and treatment interleaved."""
    treat = ("treatment", probe.value, via, _kwargs(probe, via, wire, probe.value))
    if probe.kind == "control":
        return [treat]
    rounds = runs if probe.kind == "varied" else 2
    out = [arm for _ in range(rounds) for arm in (("baseline", None, None, {}), treat)]
    if probe.name == "seed":
        out.append(("alternate", ALT_SEED, via, _kwargs(probe, via, wire, ALT_SEED)))
    return out


def ask(ctx: Ctx, probe: Probe, arm: str, value: object, via: str | None, kw: dict) -> dict:
    kwargs = dict(reasoning=ctx.reasoning, temperature=probe.temperature, max_tokens=ctx.max_tokens,
                  timeout=ctx.timeout)
    kwargs.update(kw)
    rec = dict(check="P", session=ctx.session, probe=probe.name, seq=ctx.seq, arm=arm, setting=probe.setting,
               value=value, via=via, reasoning=ctx.reasoning, temperature=kwargs["temperature"],
               max_tokens=ctx.max_tokens, prompt=probe.prompt, requested_model=ctx.model)
    ctx.seq += 1
    try:
        result = complete(ctx.endpoint, ctx.model, PROMPTS[probe.prompt], **kwargs)
    except ModelSpellError as exc:
        rec.update(error_fields(exc))
    else:
        rec.update(result_fields(result))
    rec["outcome"] = outcome(rec)
    print("P " + json.dumps(rec), flush=True)
    if rec.get("transport") not in (None, ctx.transport):
        raise RoutingRegression(f"routing regression: expected {ctx.transport}, got {rec['transport']}")
    return rec


def describe(probe: Probe, records: list[dict]) -> str:
    """What came back, in words. Counts only; no verdict."""
    if probe.kind == "control":
        r = records[0]
        return "accepted" if r["outcome"] != "error" else f"refused or failed: {r.get('error')} (status {r.get('status')})"
    by: dict[str, list[dict]] = {}
    for r in records:
        by.setdefault(r["arm"], []).append(r)
    not_ok = sum(r["outcome"] != "ok" for r in records)
    note = f"; {not_ok} of {len(records)} records not ok (see outcome)" if not_ok else ""
    base, treat = by.get("baseline", []), by.get("treatment", [])
    texts = lambda rs: [r.get("text") or "" for r in rs]  # noqa: E731
    if probe.kind == "varied":
        line = (f"baseline {len(set(texts(base)))} distinct of {len(base)}; "
                f"treatment {len(set(texts(treat)))} distinct of {len(treat)}")
        alt = by.get("alternate")
        if alt:
            line += f"; seed {ALT_SEED} {'repeats' if texts(alt)[0] in texts(treat) else 'differs from'} a seed {probe.value} answer"
        return line + note
    same = sum(t == texts(base)[0] for t in texts(treat)) if base else 0
    line = (f"baseline answers {'identical' if len(set(texts(base))) == 1 else 'differ'}; "
            f"{same} of {len(treat)} treatment answers equal the first baseline")
    if probe.name == "stop" and treat:
        line += f"; treatment ends {texts(treat)[0][-16:]!r}"
    return line + note


def _config(ctx: Ctx, url: str, probes: list[str]) -> dict:
    try:
        model = model_config(list(list_models(ctx.endpoint)), ctx.model)
    except ModelSpellError as exc:
        model = error_fields(exc)
    return dict(check="P", session=ctx.session,
                started=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                url=url, requested_model=ctx.model, reasoning=ctx.reasoning, transport=ctx.transport,
                runs=ctx.runs, max_tokens=ctx.max_tokens, probes=probes, prompts=PROMPTS,
                modelspell=modelspell.__version__, modelspell_path=str(pathlib.Path(modelspell.__file__).parent),
                python=platform.python_version(), client_platform=platform.platform(), model=model,
                manual=("LM Studio version and build", "engine runtime and version", "server platform",
                        "preset or default sampler settings"))


def _positive_int(text: str, name: str, minimum: int = 1) -> int:
    if not text.strip().isdigit() or int(text) < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {text!r}")
    return int(text)


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("MODELSPELL_LIVE_LMSTUDIO_URL"),
                   help="LM Studio base URL including /v1 (default: $MODELSPELL_LIVE_LMSTUDIO_URL)")
    p.add_argument("--model", default=os.environ.get("MODELSPELL_LIVE_LMSTUDIO_MODEL"),
                   help="model key (default: $MODELSPELL_LIVE_LMSTUDIO_MODEL)")
    p.add_argument("--reasoning", choices=list(REASONING), default="low",
                   help='"off" selects the native route; the others the Responses route (default: low)')
    p.add_argument("--probes", default=",".join(BY_NAME), help="comma-separated subset, run in catalogue order")
    p.add_argument("--runs", default="3", help="runs per arm for the temperature-1 probes (>= 2; default 3)")
    p.add_argument("--max-tokens", default="1500")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--session", default=None, help="label recorded on every line (default: UTC start time)")
    a = p.parse_args(argv)
    if not (a.url and a.model):
        p.error("set --url and --model, or MODELSPELL_LIVE_LMSTUDIO_URL and MODELSPELL_LIVE_LMSTUDIO_MODEL")
    names = [n.strip() for n in a.probes.split(",") if n.strip()]
    unknown = sorted(set(names) - set(BY_NAME))
    if unknown or not names:
        p.error(f"unknown or empty --probes {unknown}; choose from {', '.join(BY_NAME)}")
    a.probes = [n for n in BY_NAME if n in names]
    try:
        a.runs = _positive_int(a.runs, "--runs", minimum=2)
        a.max_tokens = _positive_int(a.max_tokens, "--max-tokens")
    except ValueError as exc:
        p.error(str(exc))
    if not a.timeout > 0:
        p.error("--timeout must be > 0")
    return a


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    reasoning = REASONING[a.reasoning]
    ctx = Ctx(endpoint=Endpoint(kind=KIND, base_url=a.url), model=a.model, reasoning=reasoning,
              transport="lmstudio_native" if reasoning == "off" else "responses", runs=a.runs,
              max_tokens=a.max_tokens, timeout=a.timeout,
              session=a.session or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print("P-CONFIG " + json.dumps(_config(ctx, a.url, a.probes)), flush=True)
    summary = []
    try:
        for name in a.probes:
            probe = BY_NAME[name]
            for via, wire in variants(probe, reasoning):
                records = [ask(ctx, probe, arm, value, v, kw) for arm, value, v, kw in plan(probe, via, wire, ctx.runs)]
                summary.append((name, via, describe(probe, records), probe.hint))
    except RoutingRegression as exc:
        print(f"ABORTED: {exc}. This is a modelSpell defect.", flush=True)
        return 1
    print(f"\nSUMMARY (session {ctx.session}, route {ctx.transport}, reasoning {a.reasoning}). "
          "A diagnostic for a person to read: model output is not reproducible, so none of this is a test result.")
    for name, via, what, hint in summary:
        print(f"  {name:<20} {via:<26} {what}\n  {'':<20} {'':<26} ({hint})")
    print("  Record by hand if you keep this: LM Studio version and build, engine runtime, server platform, presets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
