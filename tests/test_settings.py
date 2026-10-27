"""Generation settings: R10–R16 (spec v6 §12.3; modelSpell 0.2.0 tech spec v3 Section 8.1).

R10 unsupported settings, R11 value checks, R12 finiteness, R13 route ranges, R14 supported_settings(),
R15 stop tuples, R16 check_settings() agreement with complete(). Every refusal sends zero requests (R5).
"""
import math
import os
import socket

import pytest

from conftest import MSGS, canned, ep, route_of
from modelspell import (AuthError, ConfigError, Endpoint, UnsupportedSetting, check_settings, complete,
                        supported_settings)

NEW = ("top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty", "seed", "stop")
NUMERIC = ("temperature", "top_p", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty")
VALID = dict(max_tokens=16, temperature=0.5, top_p=0.5, top_k=5, min_p=0.1, repetition_penalty=1.1,
             presence_penalty=0.5, frequency_penalty=0.5, seed=3, stop=["END"])
ROUTES = [("lmstudio", r) for r in (None, "off", "low", "medium", "high")] + \
         [("openrouter", r) for r in (None, "off", "low", "medium", "high")] + [("openai_compat", None)]

_RESP = {"reasoning", "max_tokens", "temperature"}
_NATIVE = _RESP | {"top_p", "top_k", "min_p", "repetition_penalty"}
_OR = {"reasoning", "max_tokens", "temperature", "provider", *NEW}
_COMPAT = {"max_tokens", "temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty", "seed",
           "stop"}
# Section 4.2 of the tech spec, written out independently of the implementation's tables.
EXPECTED = {(k, r): (_NATIVE if r == "off" else _RESP) if k == "lmstudio" else (_OR if k == "openrouter" else _COMPAT)
            for k, r in ROUTES}
# Wire names per route family (Section 5.1), also independent of the implementation.
WIRE = {
    "chat": {**{n: n for n in VALID}, "provider": "provider"},
    "native": {"max_tokens": "max_output_tokens", "temperature": "temperature", "top_p": "top_p", "top_k": "top_k",
               "min_p": "min_p", "repetition_penalty": "repeat_penalty"},
    "responses": {"max_tokens": "max_output_tokens", "temperature": "temperature"},
}
ROUTE_WORDS = {"chat": "route", "native": "native route", "responses": "Responses route"}


def _complete_error(server, kind, reasoning, kw):
    """Run complete() and return the exception it raises (or None), asserting zero requests on refusal."""
    server.requests.clear()
    server.reply(canned(route_of(kind, reasoning), False))
    try:
        complete(ep(server, kind), "m", MSGS, reasoning=reasoning, **kw)
    except (ConfigError, UnsupportedSetting) as exc:
        assert server.requests == []  # R5: refused before any network I/O
        return exc
    return None


# --- cases shared by R10–R13 and R16: (kind, reasoning, kwargs, expected exception class or None) ---

def _r10_cases():
    for kind, reasoning in ROUTES:
        for name in NEW:
            if name not in EXPECTED[(kind, reasoning)]:
                yield pytest.param(kind, reasoning, {name: VALID[name]}, UnsupportedSetting,
                                   id=f"R10-{kind}-{reasoning}-{name}")


def _r11_cases():
    bad = []
    for name in NUMERIC + ("top_k", "seed"):
        bad.append((name, True))
    bad += [("top_k", 1.0), ("seed", 1.0), ("top_k", -1), ("repetition_penalty", -0.1)]
    for name in ("top_p", "min_p"):
        bad += [(name, -0.01), (name, 1.01)]
    bad += [("stop", "END"), ("stop", []), ("stop", [""]), ("stop", ["a", 1]), ("stop", {"a"})]
    for name, value in bad:
        yield pytest.param("openrouter", None, {name: value}, ConfigError, id=f"R11-bad-{name}-{value!r}")
    for name, value in (("top_p", 0), ("top_p", 1), ("min_p", 0), ("min_p", 1), ("top_k", 0), ("top_p", 0.0)):
        yield pytest.param("openrouter", None, {name: value}, None, id=f"R11-ok-{name}-{value!r}")


def _r12_cases():
    for name in NUMERIC:
        for value in (math.inf, -math.inf, math.nan):
            yield pytest.param("openrouter", None, {name: value}, ConfigError, id=f"R12-{name}-{value}")
            # value check precedes support, so a non-finite value is a ConfigError even where unsupported
            yield pytest.param("lmstudio", "low", {name: value}, ConfigError, id=f"R12-resp-{name}-{value}")


def _r13_cases():
    ok, bad = [], []
    ok += [("openrouter", None, "temperature", 2), ("openrouter", None, "repetition_penalty", 2)]
    bad += [("openrouter", None, "temperature", 2.01), ("openrouter", None, "repetition_penalty", 2.01)]
    for name in ("presence_penalty", "frequency_penalty"):
        ok += [("openrouter", None, name, -2), ("openrouter", None, name, 2)]
        bad += [("openrouter", None, name, -2.01), ("openrouter", None, name, 2.01)]
        ok += [("openai_compat", None, name, 5)]  # no documented range there
    ok += [("lmstudio", "off", "temperature", 1), ("lmstudio", None, "temperature", 5),
           ("lmstudio", "high", "temperature", 5), ("openai_compat", None, "temperature", 5),
           ("lmstudio", "off", "repetition_penalty", 5)]
    bad += [("lmstudio", "off", "temperature", 1.01)]
    for kind, reasoning, name, value in ok:
        yield pytest.param(kind, reasoning, {name: value}, None, id=f"R13-ok-{kind}-{reasoning}-{name}-{value}")
    for kind, reasoning, name, value in bad:
        yield pytest.param(kind, reasoning, {name: value}, ConfigError, id=f"R13-bad-{kind}-{reasoning}-{name}-{value}")


ALL_CASES = [*_r10_cases(), *_r11_cases(), *_r12_cases(), *_r13_cases()]


@pytest.mark.parametrize("kind,reasoning,kw,expected", list(_r10_cases()))
def test_r10_unsupported_settings_are_refused(server, kind, reasoning, kw, expected):
    exc = _complete_error(server, kind, reasoning, kw)
    assert type(exc) is UnsupportedSetting
    (name,) = kw
    route = route_of(kind, reasoning)
    assert name in str(exc) and ROUTE_WORDS[route] in str(exc)
    if route == "responses":
        assert 'reasoning="off"' in str(exc)


@pytest.mark.parametrize("kind,reasoning,kw,expected", list(_r11_cases()))
def test_r11_value_checks(server, kind, reasoning, kw, expected):
    exc = _complete_error(server, kind, reasoning, kw)
    if expected is None:
        assert exc is None
        (name, value), = kw.items()
        assert server.requests[0]["body"][name] == value
    else:
        assert type(exc) is ConfigError and next(iter(kw)) in str(exc)


@pytest.mark.parametrize("kind,reasoning,kw,expected", list(_r12_cases()))
def test_r12_non_finite_numbers_are_refused(server, kind, reasoning, kw, expected):
    exc = _complete_error(server, kind, reasoning, kw)
    assert type(exc) is ConfigError and "finite" in str(exc)


@pytest.mark.parametrize("kind,reasoning,kw,expected", list(_r13_cases()))
def test_r13_route_ranges(server, kind, reasoning, kw, expected):
    exc = _complete_error(server, kind, reasoning, kw)
    if expected is None:
        assert exc is None and len(server.requests) == 1
    else:
        assert type(exc) is ConfigError
        msg = str(exc)
        assert next(iter(kw)) in msg and "route" in msg and ("<=" in msg or "between" in msg)


@pytest.mark.parametrize("kind,reasoning", ROUTES)
def test_r14_supported_settings_table(kind, reasoning):
    result = supported_settings(kind, reasoning)
    assert isinstance(result, frozenset) and result == EXPECTED[(kind, reasoning)]


@pytest.mark.parametrize("kind,reasoning", ROUTES)
def test_r14_supported_settings_agrees_with_complete(server, kind, reasoning):
    supported = supported_settings(kind, reasoning)
    route = route_of(kind, reasoning)
    for name in [*VALID, "provider"]:
        kw = {name: {"order": ["x"]} if name == "provider" else VALID[name]}
        server.requests.clear()
        server.reply(canned(route, False))
        if name in supported:
            complete(ep(server, kind), "m", MSGS, reasoning=reasoning, **kw)
            body = server.requests[0]["body"]
            assert WIRE[route][name] in body, (name, body)
        else:
            with pytest.raises(UnsupportedSetting):
                complete(ep(server, kind), "m", MSGS, reasoning=reasoning, **kw)
            assert server.requests == []
    assert ("reasoning" in supported) == (kind != "openai_compat")


@pytest.mark.parametrize("kind,reasoning,cls", [("nope", None, ConfigError), ("lmstudio", "max", ConfigError),
                                                 ("openrouter", "", ConfigError),
                                                 ("openai_compat", "low", UnsupportedSetting),
                                                 ("openai_compat", "off", UnsupportedSetting)])
def test_r14_errors_match_complete(server, kind, reasoning, cls):
    with pytest.raises(cls) as from_supported:
        supported_settings(kind, reasoning)
    with pytest.raises(cls) as from_check:
        check_settings(kind, reasoning=reasoning)
    assert str(from_supported.value) == str(from_check.value)
    if kind in ("lmstudio", "openrouter", "openai_compat"):
        with pytest.raises(cls) as from_complete:
            complete(ep(server, kind), "m", "x", reasoning=reasoning)
        assert str(from_complete.value) == str(from_supported.value) and server.requests == []


class _NoEnv(dict):
    def __getitem__(self, k):
        raise AssertionError(f"environment read: {k}")

    def get(self, k, default=None):
        raise AssertionError(f"environment read: {k}")

    def __contains__(self, k):
        raise AssertionError(f"environment read: {k}")


@pytest.fixture
def no_io(monkeypatch):
    """Fail on any socket creation or environment read inside the block."""
    class Boom(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("socket created")

    def enter():
        monkeypatch.setattr(socket, "socket", Boom)
        monkeypatch.setattr(os, "environ", _NoEnv())

    return enter


def test_r14_r16_pure(no_io):
    no_io()
    for kind, reasoning in ROUTES:
        supported_settings(kind, reasoning)
        check_settings(kind, reasoning=reasoning, max_tokens=8, temperature=0.5)
    check_settings("openrouter", top_k=5, provider={"order": ["x"]})


def test_r15_stop_tuple_sent_as_array_in_order(server):
    server.reply(canned("chat", False))
    complete(ep(server, "openrouter"), "m", MSGS, stop=("b", "a", "c"))
    assert server.requests[0]["body"]["stop"] == ["b", "a", "c"]


@pytest.mark.parametrize("kind,reasoning,kw,expected", ALL_CASES)
def test_r16_check_settings_agrees_with_complete(server, kind, reasoning, kw, expected):
    from_complete = _complete_error(server, kind, reasoning, kw)
    if expected is None:
        assert from_complete is None
        assert check_settings(kind, reasoning=reasoning, **kw) is None
        return
    with pytest.raises(expected) as from_check:
        check_settings(kind, reasoning=reasoning, **kw)
    assert type(from_check.value) is type(from_complete) is expected
    assert str(from_check.value) == str(from_complete)


@pytest.mark.parametrize("kind,reasoning", ROUTES)
def test_r16_accepts_every_supported_setting(kind, reasoning):
    kw = {n: VALID[n] for n in EXPECTED[(kind, reasoning)] if n in VALID}
    if kind == "openrouter":
        kw["provider"] = {"order": ["x"], "require_parameters": True}
    assert check_settings(kind, reasoning=reasoning, **kw) is None


def test_r16_no_key_needed(server, no_io):
    endpoint = Endpoint(kind="openrouter", base_url=server.url + "/v1")  # no api_key; env var unset (conftest)
    with pytest.raises(AuthError):
        complete(endpoint, "m", "x", temperature=1.5, top_k=5)
    assert server.requests == []
    no_io()
    assert check_settings("openrouter", temperature=1.5, top_k=5) is None


def test_r16_provider_checks_match(server):
    for kind, provider, cls in (("lmstudio", {"order": ["x"]}, UnsupportedSetting),
                                ("openai_compat", {"order": ["x"]}, UnsupportedSetting),
                                ("openrouter", ["x"], ConfigError)):
        with pytest.raises(cls) as a:
            check_settings(kind, provider=provider)
        with pytest.raises(cls) as b:
            complete(ep(server, kind), "m", "x", provider=provider)
        assert str(a.value) == str(b.value) and server.requests == []


def test_value_check_precedes_support(server):
    """Spec v6 §6.8.4: a malformed value is reported as malformed even where the setting is unsupported."""
    exc = _complete_error(server, "lmstudio", "low", {"top_k": -1})
    assert type(exc) is ConfigError
    exc = _complete_error(server, "lmstudio", "low", {"top_k": 1})
    assert type(exc) is UnsupportedSetting
