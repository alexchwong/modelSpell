"""Discovery: D1–D5 (spec §12.7)."""
import pytest

from conftest import Reply, ep, json_reply
from modelspell import (AuthError, BackendError, ConfigError, Pricing, ResponseFormatError, UnsupportedOperation,
                        list_models, list_providers)


def test_d1_openrouter_models(server):
    server.reply(json_reply("discovery/openrouter_models_synthetic.json"))
    models = list_models(ep(server, "openrouter"))
    assert [m.id for m in models] == ["a/model-one", "b/model-two"]
    one, two = models
    assert server.requests[0]["path"] == "/v1/models"
    assert two.name == "Model Two" and two.context_length == 32768
    assert two.supports_tools is True and two.supports_reasoning is True
    assert two.pricing == Pricing(1e-6, 2e-6, {"prompt": "0.000001", "completion": "0.000002"})
    assert one.supports_tools is None and one.pricing.prompt == 0.0 and one.pricing.completion is None
    assert one.loaded is None and one.reasoning_options is None


def test_d1_lmstudio_models(server):
    server.reply(json_reply("discovery/lmstudio_models_synthetic.json"))
    models = list_models(ep(server, "lmstudio"))
    assert server.requests[0]["path"] == "/api/v1/models"
    assert [m.id for m in models] == ["google/gemma-3", "qwen/qwen3-8b"]
    gemma, qwen = models
    assert qwen.loaded is True and qwen.supports_tools is True and qwen.reasoning_options == ("off", "on")
    assert qwen.supports_reasoning is True and qwen.context_length == 32768 and qwen.name == "Qwen3 8B"
    assert gemma.loaded is False and gemma.supports_reasoning is False and gemma.reasoning_options is None
    assert qwen.pricing is None


def test_d1_compat_models(server):
    server.reply(json_reply("discovery/compat_models_synthetic.json"))
    assert [m.id for m in list_models(ep(server, "openai_compat"))] == ["alpha", "beta", "gamma", "zeta"]
    server.reply(Reply(body=b'[{"id": "x"}, "y"]'))
    assert [m.id for m in list_models(ep(server, "openai_compat"))] == ["x", "y"]


def test_d2_providers(server):
    server.reply(json_reply("discovery/openrouter_endpoints_synthetic.json"))
    providers = list_providers(ep(server, "openrouter"), "a/model-one")
    assert server.requests[0]["path"] == "/v1/models/a/model-one/endpoints"
    assert [(p.name, p.slug) for p in providers] == [("DeepInfra", "deepinfra/turbo"), ("Together", "Together"),
                                                     ("NoTag", None)]
    d = providers[0]
    assert (d.context_length, d.max_completion_tokens, d.quantization) == (8192, 4096, "fp8")
    assert d.supported_parameters == ("reasoning", "max_tokens") and d.pricing.prompt == 2e-7
    assert providers[1].supported_parameters == () and providers[1].pricing is None


def test_d2_providers_no_io_errors(server):
    with pytest.raises(ConfigError):
        list_providers(ep(server, "openrouter"), "no-slash")
    with pytest.raises(ConfigError):
        list_providers(ep(server, "openrouter"), "a/")
    for kind in ("lmstudio", "openai_compat"):
        with pytest.raises(UnsupportedOperation):
            list_providers(ep(server, kind), "a/b")
    with pytest.raises(AuthError):
        list_providers(ep(server, "openrouter", api_key=None), "a/b")
    assert server.requests == []


def test_d2_model_id_quoting(server):
    server.reply(Reply(body=b'{"data": {"endpoints": []}}'))
    list_providers(ep(server, "openrouter"), "a b/c:free")
    assert server.requests[0]["path"] == "/v1/models/a%20b/c:free/endpoints"


def test_d3_openrouter_models_without_key(server):
    server.reply(json_reply("discovery/openrouter_models_synthetic.json"))
    list_models(ep(server, "openrouter", api_key=None))
    assert "authorization" not in server.requests[0]["headers"]


def test_d4_failures(server):
    server.reply(Reply(status=500))
    with pytest.raises(BackendError):
        list_models(ep(server, "openrouter"))
    server.reply(Reply(body=b'{"data": {}}'))
    with pytest.raises(ResponseFormatError):
        list_models(ep(server, "openrouter"))
    server.reply(Reply(status=404))
    with pytest.raises(BackendError, match="0.4.0"):
        list_models(ep(server, "lmstudio"))


def test_d5_no_caching(server):
    server.reply(json_reply("discovery/compat_models_synthetic.json"))
    e = ep(server, "openai_compat")
    a, b = list_models(e), list_models(e)
    assert a == b and a is not b and len(server.requests) == 2
