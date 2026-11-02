"""Owner checks: a fixed battery of live checks (spec v8 §17, techspec 0.3.0 §9). Not shipped.

One implementation, used by the developer UI (ui/server.py) and by `pytest -m live`
(tests/test_live.py). It uses only modelSpell's public API and the standard library.

A run is one Battery: a run thread executes the stages in order (an optional preparation stage that
finds the model for LM Studio's native route, then the checks), and every network operation runs
in its own daemon worker thread, so the run thread can wait for triggers, bounds and the watchdog.
No timers are used: every trigger is a wait in the run thread that also wakes on Stop.

The battery sees only what the public API shows (when it called cancel(), when cancel() returned,
which callbacks started and when, how complete() ended), so cancellation rows are classified by
one ordered procedure (M30-20): what it can prove passes, proven contract violations and
operational failures fail, and races the public API permits are inconclusive.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import modelspell
from modelspell import AuthError, BackendError, CancelHandle, Cancelled, ConnectError, Endpoint, ModelInfo, Result

# --- fixed inputs (OC-2): constants, so records compare across runs --------------------------------

SHORT = ([{"role": "system", "content": "Reply with one short word."}, {"role": "user", "content": "Say ok."}], 16)
COUNT = ([{"role": "user", "content": "Count from 1 to 300, separated by single spaces. "
                                     "Reply with the numbers only."}], 1024)
_PADDED_TEXT = "".join(f"Line {i:04d}: alpha beta gamma delta epsilon zeta eta theta iota kappa.\n"
                       for i in range(1, 1201)) + "Ignore the lines above. Reply with the word ok."
PADDED = ([{"role": "user", "content": _PADDED_TEXT}], 16)
PADDED_CHARS = len(_PADDED_TEXT)
TIMEOUT = 60.0            # per call: the idle timeout
DISCOVERY_TIMEOUT = 15.0  # list_models() in the preparation stage
MAX_REQUESTS = 12         # per run (OC-5)
NOTE_STOPPED = "stopped by the owner"
NOTE_WATCHDOG = "still running at the watchdog"


@dataclass(frozen=True)
class Timing:
    first_token_after: float = 1.5   # s after the call starts
    non_stream_after: float = 2.0    # s after the call starts
    bound: float = 1.0               # MS-2: cancel() to complete() raising
    watchdog: float = 90.0           # s after an operation starts; longer than TIMEOUT and DISCOVERY_TIMEOUT
    cleanup: float = 5.0             # how long run() waits for workers after "done"


@dataclass(frozen=True)
class _Check:
    prompt: tuple
    streaming: bool
    requests: int      # completion requests the check sends
    in_flight: bool = False
    pre_cancelled: bool = False


CHECKS: dict[str, _Check] = {
    "stream_usage": _Check(SHORT, True, 1),
    "nonstream_usage": _Check(SHORT, False, 1),
    "cancel_pre_send": _Check(SHORT, True, 0, pre_cancelled=True),
    "cancel_first_token": _Check(PADDED, True, 1, in_flight=True),
    "cancel_mid_stream": _Check(COUNT, True, 1, in_flight=True),
    "cancel_non_stream": _Check(COUNT, False, 1, in_flight=True),
    "cancel_late": _Check(SHORT, True, 1),
    "cancel_reused": _Check(SHORT, True, 0, pre_cancelled=True),
}
PRESETS: dict[str, tuple[str, ...]] = {
    "Quick": ("stream_usage", "cancel_first_token", "cancel_mid_stream"),
    "Cancellation": ("cancel_pre_send", "cancel_first_token", "cancel_mid_stream", "cancel_non_stream",
                     "cancel_late", "cancel_reused"),
    "Usage": ("stream_usage", "nonstream_usage"),
    "Full": tuple(CHECKS),
}
DEFAULT_PRESET = "Full"
ROUTES = ("default", "native")
STATES = ("pass", "fail", "not_applicable", "inconclusive", "blocked")
_BLOCKED_STATUS = {402, 404, 429}


def validate_preset(checks) -> int:
    """Requests the checks send; refuses an unknown check or a preset above MAX_REQUESTS (OC-5)."""
    unknown = [c for c in checks if c not in CHECKS]
    if unknown:
        raise ValueError(f"unknown checks: {', '.join(unknown)}")
    requests = sum(CHECKS[c].requests for c in checks)
    if requests > MAX_REQUESTS:
        raise ValueError(f"preset sends {requests} requests; at most {MAX_REQUESTS} are allowed per run")
    return requests


def preset_info(name: str, route: str = "default") -> dict:
    """The guard's figures for a preset (OC-5), as the UI shows them before a run."""
    checks = PRESETS[name]
    return {"checks": list(checks), "calls": len(checks), "requests": validate_preset(checks),
            "discovery_requests_native": 1,
            "max_tokens": {c: CHECKS[c].prompt[1] for c in checks},
            "padded_chars": PADDED_CHARS if "cancel_first_token" in checks else None,
            "padded_tokens_approx": PADDED_CHARS // 4 if "cancel_first_token" in checks else None}


def verdict(rows: list[dict], stopped: bool = False, stuck: bool = False) -> str:
    """The run verdict (M30-17): fail if any row fails; pass only if all applicable rows pass and the
    run was neither stopped nor stuck; otherwise incomplete."""
    states = [r["state"] for r in rows]
    if "fail" in states:
        return "fail"
    applicable = [s for s in states if s != "not_applicable"]
    if applicable and all(s == "pass" for s in applicable) and not stopped and not stuck:
        return "pass"
    return "incomplete"


def pytest_outcome(record: "Record") -> tuple[str, str]:
    """Maps the run verdict to a pytest action: ("fail" | "skip" | "pass", reason)."""
    v = record.summary["verdict"]
    if v == "fail":
        failed = [r["check"] for r in record.rows if r["state"] == "fail"]
        return "fail", "failed: " + ", ".join(failed)
    if v == "incomplete":
        others = [f"{r['check']} {r['state']}" for r in record.rows if r["state"] != "pass"]
        if record.summary.get("stopped"):
            others.append("run stopped")
        return "skip", "incomplete: " + (", ".join(others) or "no checks ran")
    return "pass", "all checks passed"


# --- the record (OC-6) ----------------------------------------------------------------------------

@dataclass
class Record:
    config: dict
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    secret: str | None = None

    def _scrub(self, text: str) -> str:
        return text.replace(self.secret, "***") if self.secret else text

    def jsonl(self) -> str:
        lines = ["OC-CONFIG " + json.dumps(self.config, sort_keys=False)]
        lines += ["OC " + json.dumps(r) for r in self.rows]
        return self._scrub("\n".join(lines) + "\n")

    def markdown(self) -> str:
        def fmt(v, spec="{:.2f}"):
            return "" if v is None else (spec.format(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                                         else str(v))

        out = ["| check | state | elapsed (s) | cancel→end (s) | sent | usage in/out | finish | cost | note |",
               "|---|---|---|---|---|---|---|---|---|"]
        for r in self.rows:
            usage = "" if r.get("input_tokens") is None and r.get("output_tokens") is None else \
                f"{fmt(r.get('input_tokens'), '{}')}/{fmt(r.get('output_tokens'), '{}')}"
            note = (r.get("note") or "").replace("|", "/")
            out.append(f"| {r['check']} | {r['state']} | {fmt(r.get('elapsed_s'))} | {fmt(r.get('cancel_to_end_s'), '{:.3f}')} "
                       f"| {fmt(r.get('sent'))} | {usage} | {fmt(r.get('finish_reason'))} "
                       f"| {fmt(r.get('cost'), '{:.6f}')} | {note} |")
        out.append("")
        out.append(summary_line(self.summary))
        return self._scrub("\n".join(out) + "\n")


def summary_line(s: dict) -> str:
    if not s:
        return ""
    parts = [f"Verdict: {s['verdict']}", f"{s['passed']} of {s['total']} passed"]
    others = [f"{n} {k}" for k, n in s["counts"].items() if k != "pass" and n]
    if others:
        parts.append(", ".join(others))
    if s.get("reported_cost_subtotal") is not None:
        label = "reported cost subtotal (incomplete)" if s["unknown_cost"] else "reported cost subtotal"
        parts.append(f"{label} ${s['reported_cost_subtotal']:.6f}")
    parts.append(f"unknown cost: {s['unknown_cost']}")
    for flag in ("stopped", "stuck", "cleanup_pending"):
        if s.get(flag):
            parts.append(flag)
    return " · ".join(parts)


# --- the controller (M30-16) ----------------------------------------------------------------------

class _Op:
    """One network operation in a daemon worker thread."""

    def __init__(self, fn: Callable[[], Any], done: threading.Event):
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = time.monotonic()
        self.ended: float | None = None
        self._done = done
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        self.ended = time.monotonic()
        self._done.set()

    @property
    def finished(self) -> bool:
        return self.ended is not None


class _Stuck(Exception):
    pass


class Battery:
    """One owner-check run. The UI uses start/stop/next_event/ended/finished/record; pytest uses run()."""

    def __init__(self, endpoint: Endpoint, model: str | ModelInfo, preset: str = DEFAULT_PRESET, *,
                 route: str = "default", provider_pin: str | None = None, timing: Timing = Timing(),
                 triggers: dict[str, Callable[[], threading.Event]] | None = None,
                 complete: Callable = modelspell.complete, list_models: Callable = modelspell.list_models):
        if not isinstance(endpoint, Endpoint):
            raise ValueError("endpoint must be an Endpoint")
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; choose one of {', '.join(PRESETS)}")
        if route not in ROUTES:
            raise ValueError(f"route must be one of {ROUTES}")
        if route == "native" and endpoint.kind != "lmstudio":
            raise ValueError("the native route exists only for LM Studio")
        if not (isinstance(model, ModelInfo) or (isinstance(model, str) and model)):
            raise ValueError("model must be a non-empty id or a ModelInfo")
        self.endpoint, self.model, self.preset, self.route = endpoint, model, preset, route
        self.provider_pin = provider_pin if endpoint.kind == "openrouter" else None
        self.timing, self._triggers = timing, dict(triggers or {})
        self._complete, self._list_models = complete, list_models
        self.checks = PRESETS[preset]
        self.requests = validate_preset(self.checks)
        self.discovery = route == "native" and not isinstance(model, ModelInfo)
        if self.requests + self.discovery > MAX_REQUESTS:
            raise ValueError(f"run would send more than {MAX_REQUESTS} requests")
        self._lock = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stopped = False
        self._stop_time: float | None = None
        self._stuck = False
        self._ended = False
        self._active: CancelHandle | None = None
        self._ops: list[_Op] = []
        self._thread: threading.Thread | None = None
        self.current: dict | None = None  # {"check": id, "started": monotonic} while a stage runs
        self._late_handle: CancelHandle | None = None
        self._late_blocked: str | None = None
        self._model_arg = "ModelInfo" if isinstance(model, ModelInfo) else "id"
        self._model_used: str | ModelInfo = model
        self._record = Record(config=self._config(), secret=self._secret())

    # -- public ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("a Battery runs once")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        with self._lock:
            if self._ended:
                return False
            if not self._stopped:
                self._stop_time = time.monotonic()
            self._stopped = True
            handle = self._active
        if handle is not None:
            handle.cancel()
        self._wake.set()
        return True

    def next_event(self, timeout: float | None = None) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def finished(self) -> bool:
        threads_done = self._thread is not None and not self._thread.is_alive()
        return threads_done and all(not op.thread.is_alive() for op in self._ops)

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def stuck(self) -> bool:
        return self._stuck

    def record(self) -> Record:
        return self._record

    def run(self) -> Record:
        """start(), drain events until "done", then wait at most Timing.cleanup for the workers."""
        self.start()
        while True:
            ev = self.next_event(1.0)
            if ev is not None and ev["type"] == "done":
                break
        deadline = time.monotonic() + self.timing.cleanup
        while not self.finished and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.finished:
            self._record.summary["cleanup_pending"] = True
        return self._record

    # -- configuration and plan -------------------------------------------------------------------

    def _secret(self) -> str | None:
        e = self.endpoint
        if isinstance(e.api_key, str) and e.api_key.strip():
            return e.api_key.strip()
        if e.api_key_env:
            return os.environ.get(e.api_key_env, "").strip() or None
        return None

    def _transport(self) -> str:
        if self.route == "native":
            return "lmstudio_native"
        return "responses" if self.endpoint.kind == "lmstudio" else "chat_completions"

    def _model_id(self) -> str:
        m = self._model_used
        return m.id if isinstance(m, ModelInfo) else m

    def _config(self) -> dict:
        return {"utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "modelspell": modelspell.__version__, "python": platform.python_version(),
                "platform": sys.platform, "kind": self.endpoint.kind, "route": self.route,
                "transport": self._transport(), "base_url": self.endpoint.base_url,
                "model": self.model.id if isinstance(self.model, ModelInfo) else self.model,
                "model_arg": self._model_arg, "provider_pin": self.provider_pin, "preset": self.preset,
                "timing": dataclasses.asdict(self.timing), "timeout": TIMEOUT,
                "padded_chars": PADDED_CHARS}

    def _plan(self) -> dict:
        info = preset_info(self.preset)
        return {"type": "plan", "kind": self.endpoint.kind, "route": self.route, "model": self._config()["model"],
                "provider_pin": self.provider_pin, "preset": self.preset, "checks": list(self.checks),
                "calls": len(self.checks), "requests": self.requests,
                "discovery_requests": int(self.discovery), "max_tokens": info["max_tokens"],
                "padded_chars": info["padded_chars"], "padded_tokens_approx": info["padded_tokens_approx"],
                "max_requests": MAX_REQUESTS}

    # -- the run thread ---------------------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        if not self._ended:
            self._events.put(event)

    def _start_op(self, fn) -> _Op:
        op = _Op(fn, self._wake)  # the worker wakes the run thread when it ends
        self._ops.append(op)
        return op

    def _run(self) -> None:
        rows = self._record.rows
        try:
            self._emit(self._plan())
            if self.discovery and not self._prepare():
                return
            blocked_reason = None
            for name in self.checks:
                if blocked_reason is not None:
                    rows.append(self._row(name, "blocked", blocked_reason))
                    self._emit({"type": "check", "record": rows[-1]})
                    continue
                with self._lock:
                    if self._stopped:
                        break
                row = self._run_check(name)
                if row is None:  # stopped before the check sent anything
                    break
                rows.append(row)
                self._emit({"type": "check", "record": row})
                if self._stuck or (row.get("note") == NOTE_STOPPED):
                    break
                if row["state"] == "blocked" and row.get("error") in ("AuthError", "ConnectError"):
                    blocked_reason = row["note"]
        except BaseException as exc:  # noqa: BLE001  (a battery defect must still end the run)
            rows.append(self._row(self.current["check"] if self.current else "battery", "fail",
                                  f"battery error: {type(exc).__name__}: {exc}"))
        finally:
            self._finish()

    def _finish(self) -> None:
        self.current = None
        rows = self._record.rows
        counts = {s: sum(r["state"] == s for r in rows) for s in STATES}
        cost_known = [r["cost"] for r in rows if r.get("cost") is not None]
        unknown = sum(1 for r in rows if r.get("_unknown_cost"))
        for r in rows:
            r.pop("_unknown_cost", None)
        summary = {"verdict": verdict(rows, self._stopped, self._stuck),
                   "passed": counts["pass"], "total": len(rows), "counts": counts,
                   "reported_cost_subtotal": round(sum(cost_known), 10) if self.endpoint.kind == "openrouter" else None,
                   "unknown_cost": unknown, "cost_incomplete": bool(unknown),
                   "stopped": self._stopped, "stuck": self._stuck, "cleanup_pending": False}
        self._record.summary = summary
        done = {"type": "done", "verdict": summary["verdict"], "summary": summary, "stopped": self._stopped,
                "stuck": self._stuck, "record_markdown": self._record.markdown(),
                "record_jsonl": self._record.jsonl()}
        with self._lock:
            self._active = None
            self._events.put(done)
            self._ended = True

    def _prepare(self) -> bool:
        """The preparation stage: one list_models() call to find the model's ModelInfo (M30-18)."""
        self.current = {"check": "preparation", "started": time.monotonic()}
        self._emit({"type": "stage", "stage": "preparation", "label": "Preparing (finding the model)"})
        op = self._start_op(lambda: self._list_models(self.endpoint, timeout=DISCOVERY_TIMEOUT))
        while not op.finished:
            if self._stopped:
                return False  # the discovery worker cannot be cancelled: abandoned, result discarded
            if time.monotonic() - op.started >= self.timing.watchdog:
                self._stuck = True
                self._block_all("discovery did not finish")
                return False
            self._wake.wait(0.05)
            self._wake.clear()
        if self._stopped:
            return False
        if op.error is not None:
            self._block_all(f"discovery failed: {type(op.error).__name__}: {op.error}")
            return False
        found = next((m for m in op.result or [] if getattr(m, "id", None) == self.model), None)
        if found is not None:
            self._model_used, self._model_arg = found, "ModelInfo"
        self._record.config["model_arg"] = self._model_arg
        return True

    def _block_all(self, reason: str) -> None:
        for name in self.checks:
            self._record.rows.append(self._row(name, "blocked", reason))
            self._emit({"type": "check", "record": self._record.rows[-1]})

    # -- one check --------------------------------------------------------------------------------

    @staticmethod
    def _row(name: str, state: str, note: str = "", **kw) -> dict:
        row = {"check": name, "state": state, "note": note, "elapsed_s": None, "cancel_to_end_s": None,
               "sent": None, "callbacks_before_cancel": 0, "callbacks_after_cancel": 0, "input_tokens": None,
               "output_tokens": None, "reasoning_tokens": None, "finish_reason": None, "transport": None,
               "cost": None, "served_by": None, "error": None, "status": None}
        row.update(kw)
        return row

    def _kwargs(self) -> dict:
        kw: dict = {"reasoning": "off" if self.route == "native" else None, "timeout": TIMEOUT}
        if self.provider_pin:
            kw["provider"] = {"order": [self.provider_pin], "allow_fallbacks": False}
        return kw

    def _run_check(self, name: str) -> dict | None:
        spec = CHECKS[name]
        if name == "cancel_reused" and self._late_blocked is not None:
            return self._row(name, "blocked", self._late_blocked)
        if name == "cancel_reused" and self._late_handle is not None:
            handle = self._late_handle
        else:
            handle = CancelHandle()
        with self._lock:
            if self._stopped:
                return None
            self._active = handle
        trigger = self._triggers.get(name)
        trigger_event = trigger() if trigger is not None else None
        callbacks: list[tuple[str, float, str]] = []  # (channel, start time, fragment)
        cb_lock = threading.Lock()

        def recorder(channel):
            def cb(s):
                with cb_lock:
                    callbacks.append((channel, time.monotonic(), s))
                    n = len(callbacks)
                if name == "cancel_mid_stream" and n == 3:
                    self._wake.set()
            return cb

        messages, max_tokens = spec.prompt
        kw = self._kwargs()
        if spec.streaming:
            kw.update(on_text=recorder("text"), on_reasoning=recorder("reasoning"))
        cancel_called = cancel_returned = None
        if spec.pre_cancelled:
            handle.cancel()
            cancel_called = cancel_returned = time.monotonic()
        self.current = {"check": name, "started": time.monotonic()}
        self._wake.clear()
        model = self._model_used
        op = self._start_op(lambda: self._complete(self.endpoint, model, messages, max_tokens=max_tokens,
                                                   cancel=handle, **kw))
        stopped_during = False
        try:
            if spec.in_flight:
                fired = self._wait_trigger(name, op, trigger_event, callbacks, cb_lock)
                if fired and not op.finished and not self._stopped:
                    cancel_called = time.monotonic()
                    handle.cancel()
                    cancel_returned = time.monotonic()
            stopped_during = self._wait_end(op)
        except _Stuck:
            handle.cancel()
            self._stuck = True
            return self._row(name, "fail", NOTE_WATCHDOG, elapsed_s=round(time.monotonic() - op.started, 3))
        finally:
            with self._lock:
                self._active = None
        # A call that ended after Stop cancelled its handle was stopped, even if it ended before the
        # run thread looked (the race in which it ends between the trigger wait and the end wait).
        if self._stopped and self._stop_time is not None and op.ended is not None and op.ended >= self._stop_time:
            stopped_during = True
        if self._stopped and stopped_during:
            return self._measure(self._row(name, "inconclusive", NOTE_STOPPED), op, callbacks,
                                 cancel_called, cancel_returned)
        row = self._classify(name, spec, op, callbacks, cancel_called, cancel_returned)
        if name == "cancel_late":
            if row["state"] == "blocked":
                self._late_blocked = row["note"]
            elif op.error is None:
                self._late_handle = handle
                before = dataclasses.replace(op.result) if isinstance(op.result, Result) else op.result
                handle.cancel()
                if op.result != before:
                    row.update(state="fail", note="the Result changed after a late cancel()")
        return row

    def _wait_trigger(self, name, op, trigger_event, callbacks, cb_lock) -> bool:
        """Wait for the check's trigger; False if the call ended or the run stopped first."""
        timing = self.timing
        while True:
            if op.finished or self._stopped:
                return False
            if time.monotonic() - op.started >= timing.watchdog:
                raise _Stuck
            if trigger_event is not None:
                if trigger_event.is_set():
                    return True
            elif name == "cancel_mid_stream":
                with cb_lock:
                    if len(callbacks) >= 3:
                        return True
            else:
                after = timing.first_token_after if name == "cancel_first_token" else timing.non_stream_after
                remaining = op.started + after - time.monotonic()
                if remaining <= 0:
                    return True
                self._wake.wait(min(remaining, 0.05))
                self._wake.clear()
                continue
            self._wake.wait(0.02)
            self._wake.clear()

    def _wait_end(self, op: _Op) -> bool:
        """Wait for the call to end; True if the run was stopped while it was still running."""
        stopped_during = False
        while not op.finished:
            if self._stopped:
                stopped_during = True
            if time.monotonic() - op.started >= self.timing.watchdog:
                raise _Stuck
            self._wake.wait(0.05)
            self._wake.clear()
        return stopped_during

    def _measure(self, row, op, callbacks, cancel_called, cancel_returned) -> dict:
        row["elapsed_s"] = round((op.ended or time.monotonic()) - op.started, 3)
        if cancel_called is not None and op.ended is not None:
            row["cancel_to_end_s"] = round(op.ended - cancel_called, 3)
        if cancel_returned is None:
            row["callbacks_before_cancel"] = len(callbacks)
        else:
            row["callbacks_before_cancel"] = sum(1 for c in callbacks if c[1] <= cancel_returned)
            row["callbacks_after_cancel"] = sum(1 for c in callbacks if c[1] > cancel_returned)
        r, e = op.result, op.error
        if isinstance(r, Result):
            u = r.usage
            row.update(input_tokens=u and u.input_tokens, output_tokens=u and u.output_tokens,
                       reasoning_tokens=u and u.reasoning_tokens, finish_reason=r.finish_reason,
                       transport=r.transport, cost=r.cost, served_by=r.provider, sent=True,
                       _unknown_cost=r.cost is None)
        elif isinstance(e, Cancelled):
            row.update(sent=e.sent, error="Cancelled", _unknown_cost=e.sent is True)
            if e.partial is not None:
                row.update(finish_reason=e.partial.finish_reason, transport=e.partial.transport)
        elif e is not None:
            row.update(error=type(e).__name__, status=getattr(e, "status", None))
        return row

    def _blocked_reason(self, e: BaseException) -> str | None:
        if isinstance(e, (AuthError, ConnectError)):
            return f"{type(e).__name__}: {e}"
        if isinstance(e, BackendError) and e.status is not None and (
                e.status in _BLOCKED_STATUS or e.status >= 500):
            return f"{type(e).__name__} (HTTP {e.status}): {e}"
        return None

    def _other_exception(self, row, e) -> dict:
        reason = self._blocked_reason(e)
        if reason is not None:
            row.update(state="blocked", note=reason[:300])
        else:
            status = getattr(e, "status", None)
            row.update(state="fail", note=(f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")
                                           + f": {e}")[:300])
        return row

    def _classify(self, name, spec, op, callbacks, cancel_called, cancel_returned) -> dict:
        row = self._measure(self._row(name, "fail"), op, callbacks, cancel_called, cancel_returned)
        r, e = op.result, op.error
        bound = self.timing.bound
        texts = "".join(c[2] for c in callbacks if c[0] == "text")
        reasons = "".join(c[2] for c in callbacks if c[0] == "reasoning")

        if spec.pre_cancelled:
            if isinstance(e, Cancelled) and e.sent is False and op.ended - op.started <= bound:
                row.update(state="pass", note="")
            elif isinstance(e, Cancelled) and e.sent is False:
                row.update(note=f"Cancelled after {op.ended - op.started:.3f}s, above the {bound}s bound")
            elif isinstance(e, Cancelled):
                row.update(note=f"Cancelled with sent={e.sent!r}; a cancelled handle must send nothing")
            elif e is None:
                row.update(note="returned a Result although the handle was cancelled before the call")
            else:
                row.update(note=f"{type(e).__name__}: {e}"[:300])
            return row

        if not spec.in_flight:  # stream_usage, nonstream_usage, cancel_late
            if e is not None:
                return self._other_exception(row, e)
            if not isinstance(r, Result):
                row.update(note=f"returned {type(r).__name__}, not a Result")
                return row
            problems = []
            if r.transport != self._transport():
                problems.append(f"transport {r.transport!r}, expected {self._transport()!r}")
            if spec.streaming and (texts != r.text or reasons != (r.reasoning or "")):
                problems.append("callback concatenation differs from the Result")
            if problems:
                row.update(note="; ".join(problems))
                return row
            if name in ("stream_usage", "nonstream_usage"):
                u = r.usage
                if u is None or u.input_tokens is None or u.output_tokens is None:
                    if self.endpoint.kind == "openrouter":
                        row.update(note="usage not reported (OpenRouter documents it as always included)")
                    else:
                        row.update(state="inconclusive", note="usage not reported (allowed on this backend)")
                    return row
            row.update(state="pass", note="")
            return row

        # in-flight cancellation checks (spec 9.3, steps 1–4; step 1 is the watchdog, handled by the caller)
        after = [c for c in callbacks if cancel_returned is not None and c[1] > cancel_returned]
        per_channel = {ch: sum(1 for c in after if c[0] == ch) for ch in ("text", "reasoning")}
        if e is None:  # step 2: ended with a Result
            worst = max(per_channel, key=per_channel.get)
            if per_channel[worst] >= 2:
                cb = "on_text" if worst == "text" else "on_reasoning"
                row.update(note=f"{per_channel[worst]} {cb} callbacks started after cancel() returned, "
                                "then a Result: at most one per channel is allowed after the commit point")
                return row
            if cancel_called is None:
                row.update(state="inconclusive", note="the call returned before the trigger (fast backend)")
            else:
                row.update(state="inconclusive",
                           note="returned a Result after cancel(): the call may have committed first")
            return row
        if isinstance(e, Cancelled):  # step 3
            if cancel_called is None:
                row.update(note="Cancelled although cancel() was never called")
                return row
            if e.sent is False and callbacks:
                row.update(note="sent=False although a callback was delivered")
                return row
            if e.sent is False:
                row.update(state="inconclusive",
                           note="cancelled before the request was written (possibly during name resolution)")
                return row
            if e.sent is not True:
                row.update(note=f"Cancelled.sent is {e.sent!r}")
                return row
            if len(after) >= 2:
                row.update(note=f"{len(after)} callbacks started after cancel() returned; at most one is allowed")
                return row
            p = e.partial
            if p is None or p.text != texts or (p.reasoning or "") != reasons:
                row.update(note="partial differs from the delivered fragments")
                return row
            to_end = op.ended - cancel_called
            if to_end > bound:
                row.update(note=f"cancel() to end took {to_end:.3f}s, above the {bound}s bound")
                return row
            if name == "cancel_first_token" and callbacks:
                row.update(state="inconclusive", note="a callback arrived: the stage before the first token "
                                                      "had already passed (a slower model can make this conclusive)")
                return row
            row.update(state="pass", note="")
            return row
        return self._other_exception(row, e)  # step 4
