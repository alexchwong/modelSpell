"""Expected parse results per fixture stem; unlisted Result fields take DEFAULTS."""
from modelspell import Usage

TXT = "Héllo 🌍 world"
DEFAULTS = dict(text="", reasoning=None, usage=None, cost=None, finish_reason=None, truncated=False,
                model=None, provider=None, response_id=None)
_OR = dict(model="author/model", provider="DeepInfra", transport="chat_completions")
_NAT = dict(model="qwen3-coder-next", transport="lmstudio_native")

# (route dir, stem) -> (kind, expected fields, number of reasoning_details or None)
EXPECTED = {
    ("chat", "or_text_synthetic"): ("openrouter", dict(
        _OR, text=TXT, usage=Usage(12, 4, 16), cost=0.00012, finish_reason="stop", response_id="gen-abc"), None),
    ("chat", "or_reasoning_synthetic"): ("openrouter", dict(
        _OR, text="ans", reasoning="Let me think.", usage=Usage(5, 9, 14, reasoning_tokens=7), cost=0.0003,
        finish_reason="stop", response_id="gen-r"), "nonempty"),
    ("chat", "or_reasoning_field_synthetic"): ("openrouter", dict(
        _OR, text="ans", reasoning="Let me think.", finish_reason="stop", response_id="gen-abc"), None),
    ("chat", "or_length_usage_synthetic"): ("openrouter", dict(
        _OR, provider="Fireworks", text="cut off mid", finish_reason="length", truncated=True, response_id="gen-len",
        usage=Usage(100, 50, 150, 20, 80, 10), cost=0.002), None),
    ("chat", "compat_text_synthetic"): ("openai_compat", dict(
        text=TXT, finish_reason="stop", model="local", response_id="chatcmpl-1", transport="chat_completions"), None),
    ("chat", "compat_reasoning_content_synthetic"): ("openai_compat", dict(
        text="ans", reasoning="thinking...", finish_reason="stop", model="local", response_id="chatcmpl-2",
        usage=Usage(3, 5, 8), transport="chat_completions"), None),
    ("responses", "resp_text_synthetic"): ("lmstudio", dict(
        text=TXT, finish_reason="stop", model="m", response_id="resp_1", usage=Usage(12, 4, 16),
        transport="responses"), None),
    ("responses", "resp_reasoning_synthetic"): ("lmstudio", dict(
        text="ans", reasoning="Let me think.", finish_reason="stop", model="m", response_id="resp_2",
        usage=Usage(20, 9, 29, reasoning_tokens=5, cached_input_tokens=2), transport="responses"), "nonempty"),
    ("responses", "resp_incomplete_synthetic"): ("lmstudio", dict(
        text="partial", finish_reason="max_output_tokens", truncated=True, model="m", response_id="resp_3",
        usage=Usage(5, 8, 13), transport="responses"), None),
    ("native", "native_text_synthetic"): ("lmstudio", dict(
        _NAT, text=TXT, usage=Usage(10, 4, 14, reasoning_tokens=0)), None),
    ("native", "native_reasoning_synthetic"): ("lmstudio", dict(
        _NAT, text="ans", reasoning="Let me think.", usage=Usage(10, 9, 19, reasoning_tokens=5)), "nonempty"),
    ("native", "native_no_stats_synthetic"): ("lmstudio", dict(_NAT, text="ans"), None),
    ("native", "native_cap_synthetic"): ("lmstudio", dict(_NAT, text="long answer", usage=Usage(10, 1500, 1510)), None),
}


def check(result, fields, details):
    expected = {**DEFAULTS, **fields}
    for name, value in expected.items():
        assert getattr(result, name) == value, f"{name}: {getattr(result, name)!r} != {value!r}"
    if details is None:
        assert result.reasoning_details is None
    else:
        assert isinstance(result.reasoning_details, tuple) and result.reasoning_details
