# modelSpell

A small, dependency-free Python (≥ 3.10) library that calls language models on **LM Studio**, **OpenRouter** and generic **OpenAI-compatible** servers and returns answer, reasoning, token usage and (OpenRouter only) cost in one normalised `Result`.

The distribution is named `modelSpell`; the import name is `modelspell`.

modelSpell is a **controlled adaptation** of the completion client in [NEL](https://github.com/alexchwong/ngs_evidence_layer) (`devel` @ `7075a47`, `workflows/proforma_v1/model_client.py`), not a verbatim extraction. Behaviour that intentionally differs from NEL is listed under [Deviations from NEL](#deviations-from-nel).

## Install

Install from a [GitHub release](https://github.com/alexchwong/modelSpell/releases). Each release attaches one wheel and gives its sha256 in the notes. In a downstream `pyproject.toml` or requirements file, pin it by URL and hash:

```
modelSpell @ https://github.com/alexchwong/modelSpell/releases/download/v0.2.0/modelspell-0.2.0-py3-none-any.whl#sha256=<sha256 from the release notes>
```

The wheel contains only the library; it has no dependencies and needs no build step. Releases are the supported install channel; installing from an arbitrary commit is unsupported. `modelspell.__version__` reports the installed version. Licensed under MIT (`LICENSE`).

Development, tests, the developer test UI and the release process are described in [`DEVEL.md`](DEVEL.md).

## Public API

```python
from modelspell import (
    complete, list_models, list_providers, supported_settings, check_settings,
    Endpoint, Message, Result, Usage, ModelInfo, ProviderInfo, Pricing,
    ModelSpellError, ConfigError, UnsupportedSetting, UnsupportedOperation,
    AuthError, ConnectError, RequestTimeout, BackendError, ResponseFormatError, Cancelled,
)
```

- `Endpoint(kind, base_url=None, api_key=None, api_key_env=None, headers=None)` — `kind` is `"lmstudio"`, `"openrouter"` or `"openai_compat"`. `base_url` is the OpenAI-compatible root **including `/v1`** (defaults: `http://localhost:1234/v1`, `https://openrouter.ai/api/v1`; required for `openai_compat`). OpenRouter's `api_key_env` defaults to `OPENROUTER_API_KEY`.
- `complete(endpoint, model, input, *, reasoning=None, max_tokens=None, temperature=None, top_p=None, top_k=None, min_p=None, repetition_penalty=None, presence_penalty=None, frequency_penalty=None, seed=None, stop=None, provider=None, extra=None, timeout=900.0, on_text=None, on_reasoning=None) -> Result` — `input` is a string or a list of `Message` / `{"role", "content"}` dicts. `None` arguments are **not sent**; zero is a value and is sent. Passing either callback streams the response; the returned `Result` is the same either way. The sampling arguments are described under [Generation settings](#generation-settings).
- `supported_settings(kind, reasoning=None) -> frozenset[str]` and `check_settings(kind, *, reasoning=None, max_tokens=None, …, provider=None) -> None` — pure helpers for the generation settings; see below.
- `list_models(endpoint, *, timeout=15.0) -> list[ModelInfo]` (all kinds) and `list_providers(endpoint, "author/slug", *, timeout=15.0) -> list[ProviderInfo]` (OpenRouter only). No caching.

`complete()` never raises because the answer is empty or truncated: check `Result.text` and `Result.truncated`.

### Route table

| `kind` | `reasoning` | `Result.transport` | URL |
|---|---|---|---|
| `lmstudio` | `None`, `low`, `medium`, `high` | `responses` | `{base_url}/responses` |
| `lmstudio` | `off` | `lmstudio_native` | `{base_url minus /v1}/api/v1/chat` |
| `openrouter` | any | `chat_completions` | `{base_url}/chat/completions` (`off` → `{"effort": "none"}`) |
| `openai_compat` | `None` only | `chat_completions` | `{base_url}/chat/completions` |

One `complete()` call makes exactly one HTTP request: no retries, no route fallback, no streaming→non-streaming fallback. `reasoning` on `openai_compat`, `provider` on non-OpenRouter kinds, and any sampling argument a route does not send raise `UnsupportedSetting`.

Minimum LM Studio versions: **0.3.29** for `/v1/responses`; **0.4.0** for `reasoning="off"` and for `list_models`.

### Generation settings

The *generation settings* are `reasoning`, `max_tokens`, `temperature`, `provider` and eight sampling arguments: `top_p`, `top_k`, `min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty`, `seed` and `stop`. modelSpell sends each one under the field name of the route the request takes. **Supported** below means *modelSpell sends it on that route*, nothing more.

| Argument | OpenRouter | LM Studio native (`reasoning="off"`) | LM Studio Responses (other reasoning) | `openai_compat` |
|---|---|---|---|---|
| `reasoning` | `{"effort": …}` (`off` → `none`) | `"off"` | `{"effort": …}` | — |
| `temperature` | `temperature` (≤ 2) | `temperature` (≤ 1) | `temperature` | `temperature` |
| `max_tokens` | `max_tokens` | `max_output_tokens` | `max_output_tokens` | `max_tokens` |
| `top_p` | `top_p` | `top_p` | `top_p` (0.2.1) | `top_p` |
| `top_k` | `top_k` | `top_k` | — | `top_k` (extension) |
| `min_p` | `min_p` | `min_p` | — | `min_p` (extension) |
| `repetition_penalty` | `repetition_penalty` (≤ 2) | `repeat_penalty` | — | — |
| `presence_penalty` | `presence_penalty` (−2 to 2) | — | — | `presence_penalty` |
| `frequency_penalty` | `frequency_penalty` (−2 to 2) | — | — | `frequency_penalty` |
| `seed` | `seed` | — | — | `seed` |
| `stop` | `stop` (array) | — | — | `stop` (array) |
| `provider` | `provider` (verbatim) | — | — | — |

The table shows what modelSpell **sends**, not what a server applies.

Ranges in brackets are the backend's documented limits for that route and are checked before sending. Every route also checks each setting's meaning: `top_p` and `min_p` in [0, 1]; `top_k` an `int` ≥ 0; `temperature` and `repetition_penalty` ≥ 0; `seed` any `int` (no range: llama-server uses −1 for random); `stop` a non-empty list or tuple of non-empty strings (a bare string is refused); every number finite (no `inf`/`nan`), and `bool` is never a number.

**Verification status.** The table comes from each backend's documentation (checked 27–28 October 2026). The live compatibility check V2 checks only that a backend **accepts** each setting. It was run on 27–28 October 2026 against OpenRouter, LM Studio (both routes) and llama-server, and accepted every supported setting. For 0.2.1's `top_p` on the LM Studio Responses route it is **pending** a run from the 0.2.1 working copy. No route's settings are verified to be *applied*, and modelSpell never claims they are. No model- or provider-specific restrictions have been recorded. Details and history: `tests/fixtures/README.md`.

**Unsupported means refused, not dropped.** A non-`None` setting that the route does not send raises `UnsupportedSetting` before any network I/O, naming the setting and the route. modelSpell never accepts a setting and silently leaves it out.

**LM Studio sampling by route.** On the Responses route (reasoning `None`, `low`, `medium` or `high`), modelSpell sends `temperature`, `max_tokens` and, from 0.2.1, `top_p`. These are the generation settings OpenAI's Responses reference defines, and LM Studio's Responses page refers to that reference for the fields it does not list. Setting `reasoning="off"` (the native route) additionally exposes `top_k`, `min_p` and `repetition_penalty`. Neither LM Studio route sends `seed`, `stop`, `presence_penalty` or `frequency_penalty`.

**The other seven fields on the Responses route** are refused as named arguments. LM Studio accepts them through `extra`, but it also accepts a made-up field, so acceptance shows nothing. OpenAI's Responses reference does not define them. If you send them through `extra`, do not assume they are applied.

**`openai_compat`.** `top_p`, `presence_penalty`, `frequency_penalty`, `seed` and `stop` are part of the standard Chat Completions shape. `top_k` and `min_p` are **extensions** (llama-server and vLLM use these names); a server that does not know them may refuse or ignore them. `repetition_penalty` is **not sent** on `openai_compat`, because servers disagree on its name (llama-server `repeat_penalty`, vLLM `repetition_penalty`); pass the right name through `extra` if you know your server.

**Sent is not applied.** modelSpell guarantees only that a supported setting is *sent* under the right name. Whether it is applied depends on the backend, the model and, on OpenRouter, the provider:
- On OpenRouter, a provider that does not support a parameter ignores it by default, with no error, even with a pinned provider and `allow_fallbacks: false`. Adding `"require_parameters": True` to `provider=` makes OpenRouter route only to providers that *declare* support for every parameter in the request. That establishes declared support, not proof that every sampler changes every model's output. modelSpell passes `provider` verbatim and never adds this flag itself.
- On `openai_compat`, a server that does not know a field may refuse or ignore it.
- On LM Studio's native route, behaviour for a model that cannot use a setting is undocumented.
- On LM Studio's Responses route, LM Studio accepts fields without error whether or not it uses them, so neither acceptance nor modelSpell's support says a setting is applied.

Nothing modelSpell returns claims a setting was applied.

**`extra` still wins.** `extra` is merged last and can override a value the checks approved, or add fields a route does not send. That is unchanged from 0.1.0 and unvalidated.

#### `supported_settings()` and `check_settings()`

Both are pure: no endpoint, no key, no environment, no network. Both raise what `complete()` would raise for the same `kind` and `reasoning` (`ConfigError` for unknown values, `UnsupportedSetting` for `reasoning` on `openai_compat`).

- `supported_settings(kind, reasoning)` returns the names modelSpell sends on the route for that `kind` and `reasoning`, e.g. `supported_settings("lmstudio", "off")` → `{"reasoning", "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty"}`. It says nothing about what a model or provider applies.
- `check_settings(kind, *, reasoning=..., max_tokens=..., ..., provider=...)` runs exactly `complete()`'s generation-setting checks, from the same code, and raises the same exception with the same message. It does not check the endpoint, model, input, `extra`, `timeout` or callbacks, and never raises `AuthError`.

Use `check_settings()` when you must refuse bad settings before committing to something, such as an HTTP status for a streamed response:

```python
from modelspell import ConfigError, UnsupportedSetting, check_settings, complete

def handle(endpoint, model, prompt, settings, respond):
    try:
        check_settings(endpoint.kind, **settings)        # same kwargs you will pass to complete()
    except (ConfigError, UnsupportedSetting) as exc:
        return respond.status(400, str(exc))             # nothing sent upstream, no stream started
    stream = respond.start_stream(200)
    complete(endpoint, model, prompt, on_text=stream.write, **settings)   # will not refuse these settings
```

A pass means `complete()` will not refuse those settings. `complete()` can still raise for anything else: an invalid endpoint, a missing OpenRouter key, `extra`, or any error after sending.

### Per-subtask configuration

There is no registry: pass different arguments per call.

```python
from modelspell import Endpoint, complete

LOCAL = Endpoint(kind="lmstudio")
CLOUD = Endpoint(kind="openrouter", api_key=key_from_ui)   # held in memory only

TASKS = {
    "extract": dict(endpoint=LOCAL, model="<lmstudio-model-key>", reasoning="off", max_tokens=4096,
                    top_k=20),                                  # top_k needs reasoning="off" on LM Studio
    "review":  dict(endpoint=CLOUD, model="<author>/<model>", reasoning="high", max_tokens=16384,
                    provider={"order": ["<provider-slug>"], "allow_fallbacks": False}),
}

def run(task: str, prompt: str):
    cfg = dict(TASKS[task])
    return complete(cfg.pop("endpoint"), cfg.pop("model"), prompt, **cfg)
```

See `examples/basic.py` for discovery, a streamed call with live reasoning, and a repair-style follow-up.

### Changes in 0.2.0 that can affect 0.1.0 callers

Both raise `ConfigError` before sending; nothing else changes for calls that use none of the new arguments (same URLs, equal JSON bodies):

1. **Non-finite numbers.** `temperature=float("inf")` now raises. In 0.1.0 it produced an invalid JSON body (`Infinity`); NaN was already refused.
2. **Documented `temperature` ranges.** Above 2 on OpenRouter, or above 1 on LM Studio's native route (`reasoning="off"`), now raises. Before, the backend decided.

## Behaviour you should know about

**`extra` is a raw escape hatch.** The request bodies modelSpell builds are defaults. `extra` is merged last with a shallow `dict.update` and may override anything except `model` and `stream` (e.g. `max_tokens`, `reasoning`, `store`, `messages`/`input`). Its keys are not validated; overriding `messages`/`input` bypasses message validation and native flattening. Anything sent through `extra` is the caller's responsibility. `extra` never changes the route.

**`reasoning="off"` on LM Studio changes three things.**
1. *Protocol*: LM Studio's native `/api/v1/chat`, not the Responses API.
2. *Conversation representation*: native `input` has no role-tagged turns, so history is **flattened** into one text: system messages go to `system_prompt`; a single user turn is sent as-is; anything else becomes `[USER]\n…\n\n[ASSISTANT]\n…`. The role structure is not preserved.
3. *Storage*: `store: false` is sent by default, so LM Studio keeps no chat and `Result.response_id` is `None`. `extra={"store": True}` opts back in.

**Responses-route retention is backend-dependent and undocumented.** modelSpell never adds `previous_response_id` and sends no `store` on `/v1/responses`. LM Studio supports `previous_response_id` there, so it may retain responses on your server; how long, and whether that can be disabled, is undocumented. `extra={"store": False}` can carry the field, but whether LM Studio honours it on this route is unverified.

**Native-route truncation is a heuristic.** That API reports no stop reason, so `finish_reason` is always `None` there, and `truncated` is `output_tokens >= cap`, where the cap is `max_output_tokens` from the final request body (after `extra`). False positive: a response that ends naturally at exactly the cap reports `truncated=True`. False negative: if the backend reports no usage, `truncated` is `False` even when output was cut.

**Timeouts.** `timeout` (default 900 s) applies to the connection and to each socket read. Streaming makes it an *idle* timeout. A non-streamed request receives nothing until generation finishes, so `timeout` then bounds prefill plus the whole generation (16k tokens at ~20 tok/s ≈ 800 s). **Stream long local generations** — a no-op callback (`on_text=lambda s: None`) is enough.

**Redirects are not followed.** urllib's default handler would forward `Authorization` to the redirect target and turn POST into GET. A 3xx raises `BackendError` naming the target; set `base_url` to the final URL (e.g. `https://` if an `http://` URL redirects). Proxies (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`) are honoured.

**Streaming errors are errors.** Mid-stream error chunks/events raise `BackendError(status=200)`; a stream that ends without its completion marker raises `BackendError("stream ended before completion")`; a malformed `data:` line raises `ResponseFormatError` (one JSON object per `data:` line is the supported format). Responses and native streams stop reading at their terminal event.

**Cancellation.** A callback may raise `Cancelled()`. modelSpell closes the HTTP response, attaches `exc.partial` (what arrived so far, `finish_reason="cancelled"`) and re-raises. Closing the connection locally is the only guarantee; whether the backend stops generating is up to the backend. Any other callback exception propagates unchanged after the response is closed.

### Secrecy: guarantees and limits

Guaranteed:
- No file I/O, logging or printing; the key lives only on the `Endpoint` (resolved at call time: non-empty `api_key`, else the `api_key_env` variable).
- `repr(endpoint)` shows `api_key='***'`.
- Every string modelSpell puts in an exception it raises (message, `BackendError.body`, URLs, reasons, `Location`) has the key replaced by `***`, redacted *before* truncation. HTTP/socket/decoding failures are raised `from None`, so unredacted causes don't appear in formatted tracebacks.
- The key is sent only in the `Authorization` header of the one request to the configured URL.

Not guaranteed:
- Exceptions raised by **your callbacks** propagate unchanged.
- Model output and server data (`text`, `reasoning`, `reasoning_details`, `raw_usage`, discovery `raw`) are returned as received. If a backend echoes the key, it will be there.

### `openai_compat` is a narrow contract

It covers servers that accept the Chat Completions request shape and return standard `choices[0].message` / `choices[0].delta` responses, with no reasoning control (use `extra`, e.g. `{"chat_template_kwargs": {"enable_thinking": False}}`). llama-server, vLLM and Ollama's `/v1` are **untested candidates**, not a compatibility matrix.

### Discovery notes

`ProviderInfo.slug` is OpenRouter's endpoint `tag`, passed verbatim (case and `/` suffix kept) for use in `provider={"order": [slug]}` or `"only"`; it is `None` when absent and never derived from the display name. You can always pass a known slug directly without calling `list_providers()`.

## Deviations from NEL

| # | Deviation |
|---|---|
| 1 | No exception on empty text or truncation |
| 2 | No streaming → non-streaming fallback |
| 3 | No Responses → Chat Completions fallback on LM Studio (version message instead) |
| 4 | `None` arguments are omitted from the body |
| 5 | Reasoning and cached tokens also read from Responses-style `*_tokens_details` (bug fix) |
| 6 | Mid-stream error chunks / `error` events raise |
| 7 | Streams ending without a completion marker raise |
| 8 | Native route: `truncated` inferred against the effective cap; `finish_reason` is `None` |
| 9 | `reasoning` on `openai_compat` raises `UnsupportedSetting` |
| 10 | `provider` on non-OpenRouter kinds raises |
| 11 | Structured discovery; LM Studio native model list; per-model OpenRouter providers |
| 12 | Discovery failures raise |
| 13 | Transport chosen by explicit `kind`, not localhost detection |
| 14 | Native route sends `store: false` by default; `response_id` is then `None` |
| 15 | HTTP redirects are not followed |
| 16 | Malformed SSE `data:` events raise `ResponseFormatError` |
| 17 | `ProviderInfo.slug` comes only from `tag` |
| 18 | Responses and native streams stop reading at their terminal event |

Unchanged on purpose: native multi-turn flattening; the reasoning-alias selection rule; the Responses reasoning-delta matching rule; one per-read timeout defaulting to 900 s; no `previous_response_id` and no `store` on the Responses route; the Chat Completions stream reads to `[DONE]`/EOF; cost from `usage.cost` with no request flag.

## Fixtures

Test fixtures, their provenance and the capture procedure are documented in [`tests/fixtures/README.md`](tests/fixtures/README.md).
