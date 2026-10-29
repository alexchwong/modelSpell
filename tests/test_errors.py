"""Errors, transport and secrets: E1–E9 (spec §12.6). E4 is asserted inside the other tests."""
import socket
import traceback

import pytest

from conftest import KEY, Reply, canned, ep, fx
from modelspell import (AuthError, BackendError, ConfigError, ConnectError, Endpoint, RequestTimeout, complete,
                        ModelInfo, list_models)

NOOP = lambda s: None  # noqa: E731


def test_model_info_backend_error_names_id(server):
    info = ModelInfo(id="m", name=None, context_length=None, loaded=None, supports_tools=None,
                     supports_reasoning=None, reasoning_options=None, pricing=None, raw={})
    server.reply(Reply(status=500))
    with pytest.raises(BackendError) as exc:
        complete(ep(server, "lmstudio"), info, "x", reasoning="off")
    assert "'m'" in str(exc.value) and "ModelInfo(" not in str(exc.value)
    assert len(server.requests) == 1


def call(server, kind="openai_compat", reasoning=None, endpoint=None, **kw):
    return complete(endpoint or ep(server, kind), "m", "x", reasoning=reasoning, **kw)


@pytest.mark.parametrize("status,exc", [(401, AuthError), (403, AuthError), (400, BackendError), (404, BackendError),
                                        (422, BackendError), (429, BackendError), (500, BackendError),
                                        (502, BackendError)])
def test_e1_status_mapping(server, status, exc):
    server.reply(Reply(status=status, body=b'{"error":"x"}'))
    with pytest.raises(exc) as e:
        call(server)
    assert type(e.value) is exc and e.value.status == status
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [404, 405])
@pytest.mark.parametrize("reasoning,version", [(None, "0.3.29"), ("off", "0.4.0")])
def test_e1_lmstudio_version_messages(server, status, reasoning, version):
    server.reply(Reply(status=status))
    with pytest.raises(BackendError, match=version):
        call(server, "lmstudio", reasoning)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_e1_redirect_statuses(server, status):
    server.reply(Reply(status=status, headers={"Location": "http://elsewhere.invalid/v1"}))
    with pytest.raises(BackendError, match="redirects are not followed") as e:
        call(server)
    assert e.value.status == status and "elsewhere.invalid" in str(e.value)


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_e2_connection_refused():
    url = f"http://127.0.0.1:{_dead_port()}/v1"
    with pytest.raises(ConnectError, match=url):
        complete(Endpoint(kind="openai_compat", base_url=url), "m", "x")


def test_e3_timeouts(server):
    server.reply(Reply(delay=1.0, body=fx("chat/or_text_synthetic.json")))
    with pytest.raises(RequestTimeout):
        call(server, timeout=0.3)
    server.reply(Reply(body=[b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'], hold=True))
    with pytest.raises(RequestTimeout):
        call(server, timeout=0.3, on_text=NOOP)
    raw = fx("chat/or_text_synthetic.sse")
    server.reply(Reply(body=[e + b"\n\n" for e in raw.split(b"\n\n") if e], chunk_delay=0.15))
    assert call(server, timeout=0.5, on_text=NOOP).text == "Héllo 🌍 world"  # total > timeout, gaps < timeout


def _assert_clean(exc):
    text = str(exc) + repr(exc) + "".join(traceback.format_exception(exc)) + str(getattr(exc, "body", ""))
    assert KEY not in text


SECRET_CASES = {
    "401": (Reply(status=401, body=f"bad key {KEY}".encode()), {}),
    "500-echo": (Reply(status=500, body=f'{{"error":"{KEY}"}}'.encode()), {}),
    "500-straddle": (Reply(status=500, body=("x" * 995 + KEY + "y" * 50).encode()), {}),
    "malformed-body": (Reply(body=f"<html>{KEY}</html>".encode()), {}),
    "malformed-sse": (Reply(body=[f"data: {{bad {KEY}\n\n".encode()]), {"on_text": NOOP}),
    "midstream-error": (Reply(body=[f'data: {{"error":{{"message":"{KEY}"}}}}\n\n'.encode()]), {"on_text": NOOP}),
    "redirect": (Reply(status=302, headers={"Location": f"http://evil.invalid/?k={KEY}"}), {}),
    "timeout": (Reply(delay=1.0), {"timeout": 0.3}),
}


@pytest.mark.parametrize("case", sorted(SECRET_CASES))
def test_e5_sentinel_secrecy(server, case):
    reply, kw = SECRET_CASES[case]
    server.reply(reply)
    with pytest.raises(Exception) as e:
        call(server, endpoint=ep(server, "openai_compat", api_key=KEY), **kw)
    _assert_clean(e.value)
    if case == "500-straddle":
        assert "sk-S" not in e.value.body and len(e.value.body) <= 1000


def test_e5_secret_in_url_and_refused_connection(server):
    server.reply(Reply(status=500))
    with pytest.raises(BackendError) as e:
        complete(Endpoint(kind="openai_compat", base_url=f"{server.url}/v1?k={KEY}", api_key=KEY), "m", "x")
    _assert_clean(e.value)
    with pytest.raises(ConnectError) as e:
        complete(Endpoint(kind="openai_compat", base_url=f"http://127.0.0.1:{_dead_port()}/?k={KEY}", api_key=KEY),
                 "m", "x")
    _assert_clean(e.value)


def test_e5_repr_and_callback_boundary(server):
    server.reply(canned("chat", True))
    e = ep(server, "openai_compat", api_key=KEY)
    assert KEY not in repr(e) and "***" in repr(e)
    assert KEY not in repr(call(server, endpoint=e, on_text=NOOP))

    def leak(s):
        raise ValueError(KEY)

    with pytest.raises(ValueError, match=KEY):
        call(server, endpoint=e, on_text=leak)


def test_e6_key_resolution(server, monkeypatch):
    server.reply(canned("chat", False))
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    call(server, endpoint=ep(server, "openrouter", api_key="explicit"))
    call(server, endpoint=ep(server, "openrouter", api_key="   "))
    assert [r["headers"]["authorization"] for r in server.requests] == ["Bearer explicit", "Bearer env-key"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "  ")
    with pytest.raises(AuthError, match="OPENROUTER_API_KEY"):
        call(server, endpoint=ep(server, "openrouter", api_key=None))
    assert len(server.requests) == 2
    server.reply(canned("responses", False))
    call(server, "lmstudio")
    assert "authorization" not in server.requests[-1]["headers"]


@pytest.mark.parametrize("kw", [dict(kind="ollama"), dict(kind="openai_compat"),
                                dict(kind="lmstudio", base_url="ftp://x"),
                                dict(kind="lmstudio", headers={"authorization": "Bearer x"})])
def test_e7_endpoint_validation(kw):
    with pytest.raises(ConfigError):
        Endpoint(**kw)
    assert Endpoint(kind="lmstudio", base_url="http://h:1/v1//").base_url == "http://h:1/v1"
    headers = {"X-A": "1"}
    e = Endpoint(kind="lmstudio", headers=headers)
    headers["X-B"] = "2"
    assert e.headers == {"X-A": "1"}


@pytest.mark.parametrize("status", [302, 307])
def test_e8_redirect_not_followed(make_server, status):
    a, b = make_server(), make_server()
    b.reply(canned("chat", False))
    a.reply(Reply(status=status, headers={"Location": b.url + "/v1/chat/completions"}))
    with pytest.raises(BackendError) as e:
        call(a, endpoint=ep(a, "openai_compat", api_key="k"))
    assert e.value.status == status and b.url in str(e.value)
    assert len(a.requests) == 1 and b.requests == []


def test_e8_redirect_on_get(make_server):
    a, b = make_server(), make_server()
    a.reply(Reply(status=301, headers={"Location": b.url + "/v1/models"}))
    with pytest.raises(BackendError):
        list_models(ep(a, "openai_compat"))
    assert b.requests == []


def test_e9_proxy_honoured(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", server.url)
    server.reply(canned("chat", False))
    complete(Endpoint(kind="openai_compat", base_url="http://unreachable.invalid/v1"), "m", "x")
    assert server.requests[0]["path"] == "http://unreachable.invalid/v1/chat/completions"
