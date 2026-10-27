"""Packaging and concurrency: K1–K6 (spec §12.8; K1 updated in spec v6)."""
import dataclasses
import importlib.util
import pathlib
import re
import subprocess
import sys
import threading

import pytest

import modelspell

from modelspell import (Endpoint, Message, ModelInfo, Pricing, ProviderInfo, Result, Usage, complete)

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIELDS = {
    Endpoint: ["kind", "base_url", "api_key", "api_key_env", "headers"],
    Message: ["role", "content"],
    Usage: ["input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_input_tokens",
            "cache_write_tokens"],
    Result: ["text", "reasoning", "usage", "cost", "finish_reason", "truncated", "model", "provider", "response_id",
             "transport", "reasoning_details", "raw_usage"],
    Pricing: ["prompt", "completion", "raw"],
    ModelInfo: ["id", "name", "context_length", "loaded", "supports_tools", "supports_reasoning", "reasoning_options",
                "pricing", "raw"],
    ProviderInfo: ["name", "slug", "context_length", "max_completion_tokens", "quantization", "pricing",
                   "supported_parameters", "raw"],
}


def test_k1_public_api():
    assert modelspell.__all__ == [
        "complete", "list_models", "list_providers", "supported_settings", "check_settings",
        "Endpoint", "Message", "Result", "Usage", "ModelInfo", "ProviderInfo", "Pricing",
        "ModelSpellError", "ConfigError", "UnsupportedSetting", "UnsupportedOperation",
        "AuthError", "ConnectError", "RequestTimeout", "BackendError", "ResponseFormatError", "Cancelled"]
    for cls, names in FIELDS.items():
        assert [f.name for f in dataclasses.fields(cls)] == names


def test_k2_import_makes_no_network_calls():
    code = ("import socket\n"
            "class Boom(socket.socket):\n"
            "    def __init__(self, *a, **k): raise AssertionError('socket created at import')\n"
            "socket.socket = Boom\nimport modelspell\n")
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def _build_release_module():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_k3_k7_wheel_contents_and_clean_venv_install(tmp_path):
    build = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(tmp_path / "dist"), str(ROOT)],
                           capture_output=True, text=True)
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable (needs hatchling): {build.stderr[-300:]}")
    wheel = next((tmp_path / "dist").glob("modelspell-*.whl"))
    _build_release_module().verify_wheel(wheel, ROOT, modelspell.__version__)  # K7
    subprocess.run([sys.executable, "-m", "venv", str(tmp_path / "venv")], check=True)
    py = tmp_path / "venv" / "bin" / "python"
    subprocess.run([py, "-m", "pip", "install", "-q", "--no-index", str(wheel)], check=True)
    out = subprocess.run([py, "-I", "-c", "import modelspell; print(modelspell.__all__)"], cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout
    assert "complete" in out
    smoke = subprocess.run([py, "-I", str(ROOT / "scripts" / "smoke_installed.py")], cwd=tmp_path,
                           capture_output=True, text=True, check=True).stdout
    assert "smoke ok" in smoke and str(tmp_path / "venv") in smoke


def test_k4_concurrency(make_server):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps({"choices": [{"message": {"content": body["messages"][-1]["content"]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True).start()
    endpoint = Endpoint(kind="openai_compat", base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1")
    results = {}

    def worker(i):
        results[i] = complete(endpoint, "m", f"prompt-{i}").text

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    httpd.shutdown()
    httpd.server_close()
    assert results == {i: f"prompt-{i}" for i in range(16)}


def test_k5_stdlib_only_and_no_urlopen():
    stdlib = set(sys.stdlib_module_names)
    for path in (ROOT / "modelspell").rglob("*.py"):
        src = path.read_text()
        assert "urlopen(" not in src, path
        for mod in re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M):
            if not mod.startswith(".") and mod != "modelspell":
                assert mod.split(".")[0] in stdlib or mod == "__future__", (path, mod)


def test_version_single_source():
    src = (ROOT / "modelspell" / "_version.py").read_text()
    assert src == f'__version__ = "{modelspell.__version__}"\n'
    assert "__version__" not in modelspell.__all__
    assert not re.search(r"^version\s*=", (ROOT / "pyproject.toml").read_text(), re.M)  # dynamic only


def test_k6_hashable():
    pricing = Pricing(1.0, 2.0, {"a": 1})
    instances = [
        Endpoint(kind="lmstudio", headers={"X": "1"}), Message("user", "x"), Usage(1, 2, 3), pricing,
        Result("t", None, Usage(1, 2, 3), None, None, False, None, None, None, "responses", ({"a": 1},), {"u": 1}),
        ModelInfo("id", None, None, None, None, None, ("off",), pricing, {"r": 1}),
        ProviderInfo("n", "s", None, None, None, pricing, ("tools",), {"r": 1}),
    ]
    for obj in instances:
        hash(obj)
    assert Pricing(1.0, 2.0, {"a": 1}) == pricing and Pricing(1.0, 2.0, {"a": 2}) != pricing
