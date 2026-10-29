# modelSpell v1 — Implementation Specification (spec v7)

**Status:** Implemented (package 0.2.1). Two verifications run live (§17); no code depends on their outcome at runtime.
**Date:** 28 October 2026
**Package:** modelSpell. Distribution (display) name `modelSpell`; import name `modelspell`. pip normalises distribution names, so `modelSpell` and `modelspell` name the same distribution. Licensed under MIT. Released only as a wheel on GitHub releases (§18); not published to PyPI.
**Version:** spec v7, superseding `modelspell_v1_spec_v6.md` (kept in `docs/` as history). v7 is package 0.2.1. It folds in the *modelSpell 0.2.1 technical specification* (version 6, 28 October 2026): `top_p` on LM Studio Responses (M21-1) by the referenced Responses documentation (M21-2); no release decision depends on model output (M21-3); refusal message guidance (M21-5); the LM Studio probe diagnostic (M21-6). Its decisions are listed in §19. v6 folded in the *modelSpell 0.2.0 technical specification* (version 3, 27 October 2026): the generation settings — eight new `complete()` arguments, the per-route support and range tables, `supported_settings()` and `check_settings()` (§3, §6.1, §6.7–§6.9, §7), their tests (§12.3, §12.8), verification V2 (§17) and the developer UI's request panel (§18.3). v5 added the naming, version file, license, release and developer-UI requirements; v4 is the original library contract. Review decisions and change maps are in `docs/modelspell_design_history.md`. That document is informative and not part of this contract.
**Audience:** a coding agent implementing the package from scratch. Every behaviour needed is stated here. Where this document and your intuition disagree, follow this document.

**0.2.1 addition (29 October 2026):** `complete()` accepts a `ModelInfo` as `model` (M21-7); on the LM Studio native route a model with no reasoning configuration gets no `reasoning` field (M21-8). This additive API change means 0.2.1 is no longer API-identical to 0.2.0.

---

## 0. How to read this document

- **MUST / MUST NOT** are hard requirements, checked by the tests in §12.
- **Normative and guidance sections.** Everything is normative except §11 (package layout and private helper signatures), which is guidance. An implementation may organise private modules differently, as long as the public API (§3) and the specified behaviour are unchanged.
- **Source** means the NEL repository `alexchwong/ngs_evidence_layer`, branch `devel`, commit `7075a47a161195963d20b23d4f738279286dec8c`. Only these files are extraction sources:
  - `workflows/proforma_v1/model_client.py`: all completion transport logic (the "NEL client").
  - `scripts/model_usage.py`: `normalize_provider_usage()` and `generation_id()`.
  - `ui/server.py`: `provider_catalogue()`, `_get_json()`, `_ids()` (discovery), and `SECRETS` (key handling, for reference only).
- The `terraced_v5` and `terraced_v6` model clients are **not** sources and are out of scope.
- **This is a controlled adaptation of NEL, not a verbatim extraction.** Behaviour falls into three categories:
  1. **Ported:** NEL behaviour copied unchanged, marked "port". Copy the NEL logic and keep its behaviour, renaming identifiers to fit this package.
  2. **Supporting dependencies:** NEL helpers needed to preserve ported behaviour (§8.2 usage normalisation, §8.3 reasoning helpers).
  3. **Deviations:** behaviour that intentionally differs from NEL, marked "deviation". Each is listed in §14 with its reason and the test that checks it.

  The golden examples (§7.4) establish request-body parity for the cases they cover. They do not establish complete NEL compatibility.

---

## 1. Purpose and scope

modelSpell is a small Python library with no runtime dependencies. It calls language models on LM Studio, OpenRouter and generic OpenAI-compatible servers. It returns the answer, the reasoning and token usage, plus cost where the backend reports it, in one normalised shape.

### In scope (v1)

1. Text completion from a string or a list of `system` / `user` / `assistant` messages.
2. Three endpoint kinds: `lmstudio`, `openrouter`, `openai_compat`.
3. Optional streaming of answer and reasoning text through callbacks, with cancellation.
4. Token usage, and cost (OpenRouter only).
5. Model discovery on all kinds; provider discovery for a model on OpenRouter.
6. API key held in memory on the `Endpoint` object, with environment-variable fallback.
7. Generation settings (added in v6): `reasoning`, `max_tokens`, `temperature`, `provider` and eight sampling arguments, each mapped to the field name of the route the request takes, sent only on routes that document it, directly or through the reference the route's documentation points to (M21-2, added in v7), and checked before sending (§6.7–§6.9).

**`openai_compat` support is deliberately narrow.** It covers servers that accept the Chat Completions request shape in §7.1 and return responses parsed by §8.4 and §8.5. It offers no reasoning control. Candidate servers include llama-server, vLLM and Ollama's `/v1` endpoint. None of them is covered by the test suite or the live checks (§12.9), so this is a contract, not a tested compatibility matrix. Servers that deviate from that shape are not supported.

### Out of scope (v1)

- **Tool / function calling.** Deferred to a later version (§15 lists what v1 should avoid foreclosing).
- UI, CLI, HTTP server, proxy, logging, files, persistent key storage, inside the package. (The repository's developer test UI, §18.3, is outside the package and never ships.)
- Agent loops, memory, prompt templates, retrieval, embeddings.
- Task registries, profiles, configuration files or schedulers. Per-subtask settings are expressed by passing different arguments to each call (§6.6).
- Retries of any kind, automatic fallback between transports, fallback from streaming to non-streaming, and following HTTP redirects (§9.5).
- Server-side conversation state. ModelSpell never adds `previous_response_id` and does not manage stored conversations. A caller can still send such fields through `extra` (§6.4), outside the supported behaviour.
- Async API, Ollama's native API, multimodal input, structured-output mode (reachable through `extra`), parsing inline `<think>` tags.
- Cost estimation from price tables.
- Logit bias, log probabilities, `response_format` and structured outputs, tool definitions; single-backend samplers (OpenRouter `top_a`, llama-server DRY and XTC, LM Studio `context_length`). All remain reachable through `extra`.
- **Per-model, per-provider or per-server capability knowledge:** no discovery of which parameters a model accepts, no probing, no capability cache, no server auto-detection, and no server-specific mappings within `openai_compat` (M2-16). Such restrictions are documented, not encoded.

---

## 2. Non-functional requirements

| Requirement | Detail |
|---|---|
| Python | ≥ 3.10 |
| Runtime dependencies | **None.** Standard library only (`urllib.request`, `json`, `dataclasses`, `os`, `urllib.parse`). |
| Dev dependencies | `pytest` only. |
| API style | Synchronous. Safe to call concurrently from multiple threads. |
| Global state | **None.** No module-level mutable state, no caches, no singletons. The HTTP opener is built per call (§9.5). |
| Side effects | No printing, no logging, no file I/O, no network at import time. The package itself reads environment variables only to resolve an API key (§4.2), at call time. urllib's `ProxyHandler` reads the proxy variables when the per-call opener is built. |
| Data types | Standard-library `dataclasses`, `frozen=True`. Sequences held inside the public types (`Result`, `Usage`, `ModelInfo`, `ProviderInfo`, `Pricing`) are `tuple`s. The discovery functions return a fresh `list` on each call. Dict-valued fields (`Endpoint.headers`, `Result.raw_usage`, the items of `Result.reasoning_details`, and every `raw`) are plain `dict`s. Callers must treat them as read-only; they are not deep-frozen. Those fields are declared with `field(hash=False)` so that every public type is hashable (K6). |
| Proxies | MUST honour `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` (either case) through urllib's `ProxyHandler`. Requests are sent through an opener built per call with `urllib.request.build_opener(_NoRedirectHandler())` (§9.5). `build_opener` adds `ProxyHandler` and reads the proxy variables at construction, which is why the opener is built per call. MUST NOT call `urllib.request.urlopen()`: the default opener follows redirects. |
| Packaging | `pyproject.toml` (PEP 621), build backend `hatchling`, `name = "modelSpell"`, `license = "MIT"`. Version single-sourced from `modelspell/_version.py` (§18.1). Include `py.typed`. The wheel contains only the `modelspell/` package plus metadata and `LICENSE` (§18.2). |

---

## 3. Public API (complete)

Everything below is exported from `modelspell/__init__.py` and listed in `__all__`. Nothing else is public, except the version attribute `modelspell.__version__` (a `str`, §18.1), which is deliberately not in `__all__`.

`complete(endpoint, model: str | ModelInfo, input, *, ...) -> Result` has the full signature in §6.

```python
from modelspell import (
    # functions
    complete, list_models, list_providers, supported_settings, check_settings,
    # types
    Endpoint, Message, Result, Usage, ModelInfo, ProviderInfo, Pricing,
    # errors
    ModelSpellError, ConfigError, UnsupportedSetting, UnsupportedOperation,
    AuthError, ConnectError, RequestTimeout, BackendError, ResponseFormatError,
    Cancelled,
)
```

---

## 4. `Endpoint`

```python
@dataclass(frozen=True)
class Endpoint:
    kind: Literal["lmstudio", "openrouter", "openai_compat"]
    base_url: str | None = None
    api_key: str | None = None          # held in memory only; never shown in repr
    api_key_env: str | None = None      # env var consulted when api_key is empty
    headers: Mapping[str, str] | None = field(default=None, hash=False)  # extra HTTP headers on every request
```

### 4.1 Validation and defaults (in `__post_init__`)

| Rule | Behaviour |
|---|---|
| `kind` not one of the three values | `ConfigError` |
| `base_url` is `None` | Default: `lmstudio` → `http://localhost:1234/v1`; `openrouter` → `https://openrouter.ai/api/v1`; `openai_compat` → `ConfigError("openai_compat requires base_url")` |
| `base_url` does not start with `http://` or `https://` | `ConfigError` |
| Trailing `/` on `base_url` | Stripped. Store the normalised value with `object.__setattr__`. |
| `api_key_env` is `None` and `kind == "openrouter"` | Default `"OPENROUTER_API_KEY"` |
| `headers` contains `Authorization` (case-insensitive) | `ConfigError`. The key is set only through `api_key`. |
| `headers` | Copy into a plain `dict`, so later mutation by the caller has no effect. |

**`base_url` convention:** it is the OpenAI-compatible root, including `/v1`. The LM Studio native root is derived from it (§6.3).

**`base_url` is used as given.** Redirects are never followed (§9.5). An `http://` URL whose server redirects to `https://` fails with a message naming the target URL.

### 4.2 Key resolution

Resolution happens at call time, never at construction:

1. `api_key`, if it is a non-empty string after `strip()`.
2. Otherwise `os.environ.get(api_key_env, "").strip()`, if `api_key_env` is set and that value is non-empty.
3. Otherwise no key.

The `Authorization: Bearer <key>` header is sent only when a key resolves.

For `kind == "openrouter"`, `complete()` and `list_providers()` raise `AuthError` **before any network I/O** when no key resolves. The message names the environment variable and never contains a value. `list_models()` on OpenRouter does not require a key: the model list is public, and the key is sent if one resolves.

### 4.3 Secrecy: guarantees and limits

The package guarantees the following:

1. It never writes the key anywhere. It does no file I/O, logging or printing at all.
2. `repr(endpoint)` MUST show `api_key='***'` when a key is set and `api_key=None` otherwise. Implement a custom `__repr__`; do not rely on the dataclass default.
3. Every string the package constructs for an exception it raises is redacted (§9.3) before it is stored. This covers messages, `BackendError.body`, and any URL, reason or `Location` text included in them. It applies whether that text came from the server (error bodies, stream error events, malformed events), from urllib (reasons) or from the caller (`base_url`).
4. Exceptions the package raises for HTTP, socket or decoding failures are raised `from None`, so an unredacted cause never appears in a formatted traceback (§9.3).
5. The key is sent only in the `Authorization` header of the one request made to the configured URL. Redirects are never followed (§9.5), so the header is never re-sent to another URL.

The package does **not** guarantee the following, and the README MUST say so:

- Exceptions raised by caller callbacks (§8.10) propagate unchanged. Their content is the caller's responsibility.
- Model output and server data are returned as received. This covers `Result.text`, `reasoning`, `reasoning_details` and `raw_usage`, and the discovery `raw` fields. If a backend echoes the key into them, it will be there. The package does not scan or redact model output.

---

## 5. `Message` and input normalisation

```python
@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str
```

`complete()`'s `input` argument accepts:

| Input | Normalised to |
|---|---|
| `str` | `[Message("user", s)]` |
| Sequence of `Message` and/or `Mapping` objects | One `Message` per item, order preserved |

Each violation of these rules raises `ConfigError`. `Message.__post_init__` enforces the role and content rules for directly constructed messages too.

- A mapping must have exactly the keys `role` and `content`. Unknown keys are rejected so that future tool fields cannot be silently dropped.
- `role` must be `system`, `user` or `assistant`.
- `content` must be a `str`. An empty string is allowed.
- The list must contain at least one message.

On the wire, messages are serialised as `{"role": ..., "content": ...}` dicts, in input order, except on the native route, which flattens them (§7.3).

---

## 6. `complete()`

```python
def complete(
    endpoint: Endpoint,
    model: str | ModelInfo,
    input: str | Sequence[Message | Mapping[str, str]],
    *,
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,                           # v6
    top_k: int | None = None,                             # v6
    min_p: float | None = None,                           # v6
    repetition_penalty: float | None = None,              # v6
    presence_penalty: float | None = None,                # v6
    frequency_penalty: float | None = None,               # v6
    seed: int | None = None,                              # v6
    stop: list[str] | tuple[str, ...] | None = None,      # v6
    provider: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    timeout: float = 900.0,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
) -> Result
```

All arguments after `input` are keyword-only; their order is not significant. **Generation settings** in this spec means these twelve arguments: `reasoning`, `max_tokens`, `temperature`, `provider`, and the eight sampling arguments `top_p`, `top_k`, `min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`. `supported_settings()` and `check_settings()` (§6.9) deal in exactly these names.

### 6.1 Argument validation (all before any network I/O)

| Argument | Rule | Error |
|---|---|---|
| `model` | non-empty `str`, or `ModelInfo` with a non-empty `str` id; sends that id (M21-7) | `ConfigError`: `model must be a non-empty str or a ModelInfo with a non-empty id` |
| `reasoning` | `None` or one of the four literals | `ConfigError` |
| `reasoning` on `openai_compat` | must be `None` | `UnsupportedSetting` (the message suggests `extra`) |
| `max_tokens` | `None` or `int` ≥ 1 (not `bool`) | `ConfigError` |
| `temperature` | `None` or a finite `int`/`float` ≥ 0 (not `bool`); route ranges in §6.8.2 | `ConfigError` |
| the eight sampling arguments | §6.8.1 value checks, §6.7 route support, §6.8.2 route ranges | `ConfigError` / `UnsupportedSetting` |
| `provider` | `None`, or a mapping, and only when `kind == "openrouter"` | `UnsupportedSetting` on other kinds |
| `extra` | `None` or a mapping; must not contain the keys `model` or `stream` | `ConfigError` |
| `timeout` | number > 0 | `ConfigError` |
| `on_text`, `on_reasoning` | `None` or callable | `ConfigError` |

**Rule: `None` means "send nothing".** For every generation setting, a `None` value means the field is absent from the request body, so the backend's default applies. **Zero is a value, not "unset":** `temperature=0`, `top_k=0`, `seed=0`, `presence_penalty=0` and the like are sent as given. NEL always sent `temperature` and `max_tokens`; callers that want NEL's request bodies pass them explicitly.

The order in which these checks run is fixed in §6.8.4.

### 6.2 Streaming switch

The request is streamed if and only if `on_text` or `on_reasoning` is not `None`. In both modes `complete()` returns the same complete `Result`.

Streaming is the recommended mode for long generations, because it makes `timeout` an idle timeout rather than a limit on total generation time (§9.4). A no-op callback (`on_text=lambda s: None`) is enough to enable it.

### 6.3 Route selection

| `kind` | `reasoning` | Route (`Result.transport`) | URL |
|---|---|---|---|
| `lmstudio` | `None`, `"low"`, `"medium"`, `"high"` | `"responses"` | `{base_url}/responses` |
| `lmstudio` | `"off"` | `"lmstudio_native"` | `{native_root}/api/v1/chat` |
| `openrouter` | any | `"chat_completions"` | `{base_url}/chat/completions` |
| `openai_compat` | `None` only | `"chat_completions"` | `{base_url}/chat/completions` |

`native_root` is `base_url` with one trailing `/v1` removed, if present. Port this from NEL `_endpoint()`.

There is **no fallback** between routes, and no fallback from streaming to non-streaming. Each `complete()` call makes exactly one HTTP request. Redirects are not followed (§9.5), so this holds at the HTTP level too.

### 6.4 `extra` merge and the effective token cap

After the route builds its body, `extra` is applied as a shallow top-level `dict.update`. It may override any key except `model` and `stream`, which §6.1 rejects. That includes keys the route sets from public arguments, such as `max_tokens` / `max_output_tokens`, `temperature`, `reasoning`, `provider` and `store`. This is intended: it is the escape hatch for backend-specific behaviour (G8).

**Defaults and raw overrides.** The request bodies in §7 are ModelSpell's defaults. Statements elsewhere in this spec about what a body contains ("sent by default", "never adds") describe those defaults and hold unless `extra` overrides them. `extra` is a raw escape hatch:

- Keys it sets are not validated, and a request that uses it may fall outside the normalised behaviour this spec describes. That is the caller's responsibility.
- Overriding `messages` (Chat Completions) or `input` (Responses, native) bypasses the §5 message validation and the §7.3 flattening.
- Overriding `store` changes the native-route storage default (§7.3). Adding `previous_response_id` opts into server-side state that ModelSpell does not manage (§1).

**Why only `model` and `stream` are blocked.** Response parsing depends on `stream`: a body whose `stream` disagrees with the callbacks would be parsed in the wrong mode. `model` is the required positional argument, and overriding it would make that argument meaningless. The only other body value that parsing depends on is the output-token cap, and that is read from the merged body (below), so overriding it stays consistent.

`extra` never changes route selection. For example, `extra={"reasoning": ...}` on LM Studio does not switch routes.

**Effective token cap.** Response parsing that depends on the output-token cap reads the cap from the final, merged request body, not from the `max_tokens` argument. In v1 only the native route does this (§8.8), reading `body.get("max_output_tokens")`. A value that is not an `int` ≥ 1, or is a `bool`, counts as no cap.

### 6.5 Never raises for empty or truncated output

`complete()` MUST NOT raise because the answer text is empty or because output was truncated. These are reported through `Result.text == ""` and `Result.truncated`. (Deviation: NEL raised `TruncatedCompletion` and "empty completion" errors. That policy moves to NEL's own wrapper, §13.)

### 6.6 Per-subtask configuration (informative)

Each call takes its own endpoint, model, reasoning, provider and token cap, so an application assigns settings per subtask simply by calling with different arguments. The package adds no registry. The README MUST include an example like this:

```python
from modelspell import Endpoint, complete

LOCAL = Endpoint(kind="lmstudio")
CLOUD = Endpoint(kind="openrouter", api_key=key_from_ui)   # held in memory only

TASKS = {
    "extract": dict(endpoint=LOCAL, model="<lmstudio-model-key>", reasoning="off", max_tokens=4096),
    "review":  dict(endpoint=CLOUD, model="<author>/<model>", reasoning="high", max_tokens=16384,
                    provider={"order": ["<provider-slug>"], "allow_fallbacks": False}),
}

def run(task: str, prompt: str):
    cfg = dict(TASKS[task])
    return complete(cfg.pop("endpoint"), cfg.pop("model"), prompt, **cfg)
```

In v6 the README example also shows one advanced setting (for example `top_k` on the `reasoning="off"` task).

### 6.7 Generation settings: route support and wire names (added in v6)

#### 6.7.1 Support table (normative)

 "Supported" in this table, and throughout this spec, means **modelSpell accepts the argument and sends it under this name on this route**. Whether the backend applies it is a separate question (§6.7.3).

| Named argument | LM Studio Responses (reasoning `None`, `low`, `medium`, `high`) | LM Studio native (`reasoning="off"`) | OpenRouter (Chat Completions) | `openai_compat` (Chat Completions) |
|---|---|---|---|---|
| `reasoning` | `reasoning: {"effort": …}`; `None` omitted | `reasoning: "off"`, except M21-8 (§7.3) | `reasoning: {"effort": …}`; `off` sent as `none`; `None` omitted | refused for any non-`None` value |
| `max_tokens` | `max_output_tokens` | `max_output_tokens` | `max_tokens` | `max_tokens` |
| `temperature` | `temperature` | `temperature` | `temperature` | `temperature` |
| `top_p` | `top_p` (added in v7) | `top_p` | `top_p` | `top_p` |
| `top_k` | — | `top_k` | `top_k` | `top_k` (extension) |
| `min_p` | — | `min_p` | `min_p` | `min_p` (extension) |
| `repetition_penalty` | — | `repeat_penalty` | `repetition_penalty` | — (server names differ) |
| `presence_penalty` | — | — | `presence_penalty` | `presence_penalty` |
| `frequency_penalty` | — | — | `frequency_penalty` | `frequency_penalty` |
| `seed` | — | — | `seed` | `seed` |
| `stop` | — | — | `stop` (array) | `stop` (array) |
| `provider` | — | — | `provider` (mapping, verbatim) | — |

"—" means refused: a valid non-`None` value raises `UnsupportedSetting` before any network I/O (§6.7.2). An invalid value can raise `ConfigError` first, under the order of checks in §6.8.4. The table is the library's send-and-validate contract. It is not a claim about what any server applies (§6.7.3). `extra` remains an unvalidated escape hatch that can add or override any field except `model` and `stream`. It never changes the route and establishes nothing about server support.

Why each column is what it is:

- **OpenRouter.** Every field is documented on OpenRouter's parameters page, under these names.
- **LM Studio native.** LM Studio documents `temperature`, `top_p`, `top_k`, `min_p` and `repeat_penalty` for `POST /api/v1/chat`, and not `seed`, `stop`, `presence_penalty` or `frequency_penalty`. §7.2's rule applies: send only fields the backend documents for the route.
- **LM Studio Responses** (changed in v7). `temperature`, `max_tokens` and `top_p` are sent: LM Studio's Responses page refers to OpenAI's Responses reference for the fields it does not list, and that reference defines them (M21-2). The other seven are not sent, because neither LM Studio's Responses page nor OpenAI's reference defines them. **Consequence:** on this route a caller gets `temperature`, `max_tokens` and `top_p`. Setting `reasoning="off"` additionally exposes `top_k`, `min_p` and `repetition_penalty` through native chat. Neither LM Studio route sends `seed`, `stop`, `presence_penalty` or `frequency_penalty`. Changing routing to lift this is out of scope. A caller can still send fields through `extra`, with unverified effect.
- **`openai_compat`.** `top_p`, `presence_penalty`, `frequency_penalty`, `seed` and `stop` are part of the standard Chat Completions shape. `top_k` and `min_p` are **extensions**, included because llama-server and vLLM both use these names; a server that does not know them may refuse or ignore them. `repetition_penalty` is **excluded**: llama-server calls it `repeat_penalty` and vLLM `repetition_penalty`, and picking one name would silently do nothing on the other. A caller who knows its server uses `extra`.

**One table, three readers.** The support table is held in one place, the route modules, as a mapping from argument to wire name for each route. The setting checks (§6.8, shared by `check_settings()` and `complete()`), body building (§7) and `supported_settings()` (§6.9) all read that same mapping, so they cannot disagree (R14, R16). Where the mapping lives is guidance (§11); that there is only one is normative.

#### 6.7.2 Unsupported means refused, not dropped

A non-`None` argument that the route does not support raises `UnsupportedSetting` before any network I/O, so the server receives zero requests. The message names the argument and the route, for example: `top_k is not supported on the LM Studio Responses route (reasoning other than "off"); use reasoning="off", or pass it through extra (effect unverified)`. On the Responses route (M21-5, v7), the message suggests `reasoning="off"` only for a setting the native route sends (`top_k`, `min_p`, `repetition_penalty`), taken from the native route's table so it cannot drift. For a setting neither LM Studio route sends (`seed`, `stop`, `presence_penalty`, `frequency_penalty`) it says so, for example `seed is not supported on the LM Studio Responses route (reasoning other than "off") or the LM Studio native route; pass it through extra (effect unverified)`. Both forms say an `extra` field's effect is unverified: the route's documentation does not define these fields, and LM Studio accepts fields without error whether or not it uses them. Messages on other routes end "; pass it through extra". modelSpell never drops a setting silently: accepting it and leaving it out of the body would tell the caller the setting was sent when it was not. The existing `UnsupportedSetting` uses for `reasoning` on `openai_compat` and `provider` off OpenRouter keep their behaviour and messages.

#### 6.7.3 Sent is not the same as applied (documented limitation)

modelSpell guarantees only that a supported setting is **sent** under the right name. Whether the model applies it is up to the backend, the model and, on OpenRouter, the provider:

- **OpenRouter.** By default, a provider that does not support a parameter ignores it, with no error; pinning a provider and disabling fallbacks does not change this. `"require_parameters": true` in `provider=` makes OpenRouter route only to providers that declare support for every parameter in the request. That establishes declared support, not proof that every sampler changes every model's output. modelSpell passes `provider` verbatim and adds nothing to it (M2-8).
- **`openai_compat`.** A server that does not know a field may refuse it or ignore it.
- **LM Studio native.** Behaviour for a model that cannot use a setting is undocumented.
- **LM Studio Responses** (v7). LM Studio accepts fields without error whether or not it uses them, so neither acceptance nor modelSpell's support says a setting is applied.

Nothing modelSpell returns claims a setting was applied. The README states this.

### 6.8 Setting checks (added in v6)

All checks run before key resolution and before any network I/O, through the same path as other validation errors, so redaction (§9.3) is unaffected. They are implemented once: `check_settings()` exposes them, and `complete()` calls the same code.

#### 6.8.1 Value checks (every route)

"Number" means `int` or `float`, not `bool`, and finite (§6.8.3).

| Argument | Rule | Error |
|---|---|---|
| `top_p` | number, 0 ≤ value ≤ 1 | `ConfigError` |
| `top_k` | `int` (not `bool`), ≥ 0 | `ConfigError` |
| `min_p` | number, 0 ≤ value ≤ 1 | `ConfigError` |
| `repetition_penalty` | number, ≥ 0 | `ConfigError` |
| `presence_penalty`, `frequency_penalty` | number | `ConfigError` |
| `seed` | `int` (not `bool`); no range | `ConfigError` |
| `stop` | a non-empty `list` or `tuple` of non-empty `str`. A bare `str` is refused, so `"END"` is never sent as `["E", "N", "D"]` or silently reinterpreted. | `ConfigError` |
| `temperature` | number, ≥ 0 | `ConfigError` |
| `max_tokens` | `int` ≥ 1 (not `bool`), as in §6.1 | `ConfigError` |

The ranges are each setting's meaning, not any backend's limit: `top_p` and `min_p` are probabilities; a top-k count cannot be negative and 0 conventionally disables it; a repetition penalty is a multiplier. `seed` has no range in modelSpell (llama-server uses −1 for a random seed); callers impose their own limits. `stop` is sent as a JSON array in the given order; modelSpell sets no limit on the number of sequences, because no backend documents one.

#### 6.8.2 Route range checks

Ranges a backend documents for its route, checked only on that route. A value outside them raises `ConfigError`, naming the argument, the range and the route.

| Argument | OpenRouter | LM Studio native | LM Studio Responses | `openai_compat` |
|---|---|---|---|---|
| `temperature` | ≤ 2 | ≤ 1 | — | — |
| `repetition_penalty` | ≤ 2 | — | n/a | n/a |
| `presence_penalty` | −2 to 2 | n/a | n/a | — |
| `frequency_penalty` | −2 to 2 | n/a | n/a | — |

"—" means no documented range, so only §6.8.1 applies. "n/a" means the setting is not supported on that route, so §6.7.2 refuses it before any range check.

**When a documented range changes** (one rule, also used by V2, §17). A range is kept as long as the backend documents it. It changes only on authoritative evidence (the backend's own documentation changed) or reproducible implementation evidence that the documented range is wrong (the server's source, or a reproducible test showing that values outside it are accepted *and* change the output). A backend merely accepting an out-of-range value (HTTP 200) is not enough, because it may clamp or ignore it. Every change is recorded in the design history with its evidence.

#### 6.8.3 Finiteness

Every numeric setting must be finite: `temperature` and the new numeric arguments. Infinity and NaN raise `ConfigError` before sending. (0.1.0 accepted `temperature=inf`, because `inf >= 0`, and `json.dumps` then wrote `Infinity`, which is not valid JSON.) `max_tokens`, `top_k` and `seed` are integers, which are always finite.

#### 6.8.4 Order of checks

**The setting checks**, shared by `check_settings()` and `complete()`:

1. `kind` is one of the three kinds (`ConfigError`).
2. `reasoning` is `None` or one of the four literals (`ConfigError`), and is `None` for `openai_compat` (`UnsupportedSetting`).
3. Route selection, using §6.3's rule.
4. For each generation setting that is not `None`, in the order `max_tokens`, `temperature`, `top_p`, `top_k`, `min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`:
   1. the value check (§6.8.1);
   2. if the route does not support it, `UnsupportedSetting` (§6.7.2);
   3. the route range check (§6.8.2).
5. `provider` is given only for `openrouter` (`UnsupportedSetting`), and is a mapping (`ConfigError`).

So for each argument the value check comes before support: a malformed value is reported as malformed even on a route that would refuse the argument anyway, which keeps the error stable when the caller changes route.

**`complete()`'s validation** runs, in order: the `endpoint` and `model` checks; the setting checks above with `kind = endpoint.kind`; then the `extra`, `timeout` and callback checks. Then it resolves the key (§4.2, which may raise `AuthError`) and sends.

### 6.9 `supported_settings()` and `check_settings()` (added in v6)

```python
def supported_settings(
    kind: Literal["lmstudio", "openrouter", "openai_compat"],
    reasoning: Literal["off", "low", "medium", "high"] | None = None,
) -> frozenset[str]

def check_settings(
    kind: Literal["lmstudio", "openrouter", "openai_compat"],
    *,
    reasoning=None, max_tokens=None, temperature=None, top_p=None, top_k=None, min_p=None,
    repetition_penalty=None, presence_penalty=None, frequency_penalty=None, seed=None, stop=None,
    provider=None,
) -> None   # parameter types as in complete()
```

Both are **pure**: no endpoint, no network, no environment, no state. Both take a `kind` string, so a caller can ask before building an endpoint or resolving a key.

**`supported_settings()`** returns the names of the generation settings modelSpell accepts and sends on the route chosen by this `kind` and `reasoning`. A call that passes any other generation setting as non-`None` raises `UnsupportedSetting`. It never contains `endpoint`, `model`, `input`, `extra`, `timeout`, `on_text` or `on_reasoning`, which are always accepted. It says nothing about whether a model, provider or server applies a setting. It raises exactly what `complete()` would for the same `kind` and `reasoning`: `ConfigError` for an unknown value, `UnsupportedSetting` for `openai_compat` with a reasoning value.

| `kind` | `reasoning` | Route | Result |
|---|---|---|---|
| `lmstudio` | `None`, `low`, `medium`, `high` | `responses` | `{reasoning, max_tokens, temperature, top_p}` |
| `lmstudio` | `off` | `lmstudio_native` | `{reasoning, max_tokens, temperature, top_p, top_k, min_p, repetition_penalty}` |
| `openrouter` | any | `chat_completions` | `{reasoning, max_tokens, temperature, provider, top_p, top_k, min_p, repetition_penalty, presence_penalty, frequency_penalty, seed, stop}` |
| `openai_compat` | `None` | `chat_completions` | `{max_tokens, temperature, top_p, top_k, min_p, presence_penalty, frequency_penalty, seed, stop}` |

**`check_settings()`** runs exactly the setting checks of §6.8.4, from the same code `complete()` uses, and raises the same exception with the same message; it returns `None` when every check passes. It does not build or validate an `Endpoint` (so the base URL is not checked), does not check `model`, `input`, `extra`, `timeout` or the callbacks, and never resolves a key, so it never raises `AuthError`. A pass means `complete()` will not refuse those settings; `complete()` can still raise for anything outside them (an invalid endpoint, a missing OpenRouter key, `extra`, or any error after sending).

*Why it exists.* A caller that streams `complete()`'s output over HTTP has usually committed its response status before `complete()` runs. `supported_settings()` gives only names and cannot see `temperature=3` on OpenRouter; `check_settings()` lets the caller run every setting check first and answer HTTP 400 before streaming.

---

## 7. Wire formats: request bodies

All requests are `POST`, with these headers:

```
Content-Type: application/json
Authorization: Bearer <key>        # only if a key resolves (§4.2)
<endpoint.headers...>
```

The body is `json.dumps(body).encode("utf-8")`. Key order in the body is irrelevant; tests compare parsed dicts.

### 7.1 Chat Completions (`openrouter`, `openai_compat`)

This ports NEL `_payload()`'s `chat` branch and `_reasoning_request()`.

```python
body = {"model": model, "messages": [...], "stream": streaming}
# every supported, non-None generation setting from §6.7, under its wire name:
for arg, wire in FIELDS[kind].items():   # openrouter: 10 fields; openai_compat: 9 (no repetition_penalty)
    if settings[arg] is not None:
        body[wire] = list(settings[arg]) if arg == "stop" else settings[arg]
# openrouter only:
if provider is not None:    body["provider"] = dict(provider)      # verbatim
if reasoning is not None:   body["reasoning"] = {"effort": EFFORT[reasoning]}
```

`EFFORT = {"off": "none", "low": "low", "medium": "medium", "high": "high"}`

OpenRouter accepts `none` as an effort value and treats it as reasoning disabled. NEL's `"none"` setting produced exactly `{"effort": "none"}`.

`provider` is passed through verbatim. Callers who already know an exact provider or endpoint slug pass it directly, for example `provider={"order": ["deepinfra/turbo"]}`, without calling `list_providers()`.

### 7.2 LM Studio Responses (`lmstudio`, reasoning `None`/`low`/`medium`/`high`)

This ports NEL `_payload()`'s `responses` branch.

```python
body = {"model": model, "input": [...messages as dicts...], "stream": streaming}
if temperature is not None: body["temperature"] = temperature
if max_tokens is not None:  body["max_output_tokens"] = max_tokens
if top_p is not None:       body["top_p"] = top_p                       # v7 (M21-1)
if reasoning is not None:   body["reasoning"] = {"effort": reasoning}   # low|medium|high
# nothing else: every other sampling argument is refused before this point (§6.7)
```

System messages stay inside `input` with `role: "system"`, as in NEL. ModelSpell never adds `previous_response_id`, and does not send `store` on this route.

**Rule (reworded in v7, M21-2): send the generation settings the backend documents for the selected route.** Where the route's page refers to another API's reference for fields it does not list, the generation settings that reference defines count as documented for the route. Nothing else is sent: a setting neither documented nor defined by that reference is refused (§6.7.2). The rule covers the named generation settings only; `store` is a retention policy, decided as follows. LM Studio documents `store` for native chat (§7.3), so the native route sends `store: false` by default. LM Studio's Responses documentation describes `previous_response_id` but does not document `store`, so the Responses route sends no `store` field.

**Server-side retention.** Not adding `previous_response_id` does not make the server stateless. LM Studio's `/v1/responses` supports `previous_response_id`, so it can retain responses server-side; how long, and whether that can be disabled, is undocumented. Any retention happens on the user's own LM Studio server, not a third party's. The README MUST state that:

- Responses-route retention is backend-dependent and undocumented.
- `extra={"store": False}` can carry the field (§6.4), but whether LM Studio honours it on this route is unverified.

No live check decides this default. An HTTP 200 would show only that the server accepted the field, not that storage was disabled.

### 7.3 LM Studio native chat (`lmstudio`, reasoning `"off"`)

This ports NEL `_payload()`'s `lmstudio-native` branch and `_lmstudio_native_prompt()`, with `store: false` by default (#14) and the conditional reasoning field (M21-8).

```python
system_prompt, user_input = flatten(messages)
body = {"model": model_id, "input": user_input, "store": False, "stream": streaming}
if body_reasoning is not None: body["reasoning"] = body_reasoning
if temperature is not None:  body["temperature"] = temperature
if max_tokens is not None:   body["max_output_tokens"] = max_tokens
# v6: top_p -> top_p, top_k -> top_k, min_p -> min_p, repetition_penalty -> repeat_penalty (if not None)
if system_prompt is not None: body["system_prompt"] = system_prompt
```

**M21-8:** route selection still uses the caller's `reasoning="off"`. Set `body_reasoning` to that
value, except when the caller passed a `ModelInfo` whose `reasoning_options` is `None` or empty:
then omit the field, because the model has nothing to turn off and LM Studio refuses it. String
models behave as before. Non-empty options, including `("on",)`, still send `"reasoning": "off"`;
there is no client-side refusal for a missing `"off"` option. `supported_settings()` and
`check_settings()` are unchanged (M21-9). The library never discovers or caches capabilities itself;
it uses only the entry passed by the caller (M2-16). `extra` still merges last (§6.4).

`flatten(messages)` is ported **exactly**:

1. `system_parts` is the contents of all `system` messages, in order. `system_prompt = "\n\n".join(p for p in system_parts if p)`, or `None` if that join is empty.
2. `conversation` is all non-system messages, in order.
3. If `conversation` has exactly one message and its role is `user`, then `user_input` is that message's content.
4. Otherwise `user_input = "\n\n".join(f"[{m.role.upper()}]\n{m.content}" for m in conversation)`.

**Why this route flattens.** LM Studio's native `input` accepts a string, or an array of text and image items. It does not accept role-tagged turns. True multi-turn conversation on this route requires `previous_response_id`, which needs `store: true` and server-side state, and that is incompatible with a stateless library. Flattening plus `store: false` is the consistent pair.

**Documented limitation.** On this route, multi-turn history is flattened into one text input, and the original role structure is not preserved. NEL's repair loop depends on this behaviour (`automatic_marking.py` sends system, user, assistant, user), and NEL's test `test_native_retry_history_is_preserved_as_text_input` asserts it. `Result.transport == "lmstudio_native"` tells the caller this route ran. The README MUST state prominently that `reasoning="off"` on LM Studio changes three things: the protocol, the conversation representation, and storage.

**Storage (deviation #14).** `store: false` is sent by default, so LM Studio does not keep the chat. As a consequence, by default the response carries no `response_id`, and `Result.response_id` is `None` on this route. A caller who wants the server to keep the chat can pass `extra={"store": True}`, which overrides the default (§6.4).

### 7.4 Golden request examples (MUST match exactly as parsed JSON)

G1, G2, G5, G7 and G9 were generated by running NEL's `_endpoint()` and `_payload()` at the pinned commit, so they are NEL parity checks. G3 and G4 are NEL's output plus `"store": false` (deviation #14). For G7, NEL itself needs a non-localhost host to reach its generic route (§14 #13). G6 and G8 cover new behaviour (§6.1, §6.4).

Each example uses `model="m"` and these messages unless it says otherwise:

`MSGS = [{"role":"system","content":"sys"},{"role":"user","content":"task"}]`

**G1** — `lmstudio`, `reasoning=None`, `temperature=0.0`, `max_tokens=8192`, non-streaming:

```
POST http://localhost:1234/v1/responses
{"model":"m","input":MSGS,"temperature":0.0,"max_output_tokens":8192,"stream":false}
```

**G2** — `lmstudio`, `reasoning="high"`, `temperature=0.0`, `max_tokens=8192`, streaming:

```
POST http://localhost:1234/v1/responses
{"model":"m","input":MSGS,"temperature":0.0,"max_output_tokens":8192,"stream":true,"reasoning":{"effort":"high"}}
```

**G3** — `lmstudio`, `reasoning="off"`, `temperature=0.0`, `max_tokens=16384`, non-streaming. Input is `sys`, then `user:original`, `assistant:bad output`, `user:repair feedback`:

```
POST http://localhost:1234/api/v1/chat
{"model":"m",
 "input":"[USER]\noriginal\n\n[ASSISTANT]\nbad output\n\n[USER]\nrepair feedback",
 "temperature":0.0,"max_output_tokens":16384,"reasoning":"off","store":false,"stream":false,
 "system_prompt":"sys"}
```

**G4** — `lmstudio`, `reasoning="off"`, input = `MSGS`: the body has `"input":"task"`, `"system_prompt":"sys"` and `"store":false`.

**G5** — `openrouter`, `reasoning="off"`, `temperature=0.2`, `max_tokens=4096`, `provider={"order":["deepinfra"],"allow_fallbacks":False}`, streaming:

```
POST https://openrouter.ai/api/v1/chat/completions
{"model":"m","messages":MSGS,"temperature":0.2,"max_tokens":4096,"stream":true,
 "provider":{"order":["deepinfra"],"allow_fallbacks":false},"reasoning":{"effort":"none"}}
```

**G6** — `openrouter`, all optional arguments `None`, non-streaming:

```
{"model":"m","messages":MSGS,"stream":false}
```

**G7** — `openai_compat`, `base_url="http://localhost:8080/v1"`, `temperature=0.0`, `max_tokens=512`:

```
POST http://localhost:8080/v1/chat/completions
{"model":"m","messages":MSGS,"temperature":0.0,"max_tokens":512,"stream":false}
```

**G8** — G7 with `extra={"chat_template_kwargs":{"enable_thinking":False},"max_tokens":256}`: the body gains `chat_template_kwargs`, and `max_tokens` becomes `256`.

**G9** — a `lmstudio` endpoint with `base_url="http://host:1234/v1/"` and `reasoning="off"`: the URL is `http://host:1234/api/v1/chat`.

Added in v6 (the exact bodies are in `tests/golden/requests.json`):

**G10** — `openrouter`, streaming, with `temperature`, `max_tokens` and all eight sampling arguments (`stop` given as a list): every one appears under its own name; `stop` is a JSON array.

**G11** — `lmstudio`, `reasoning="off"`, with `top_p`, `top_k`, `min_p` and `repetition_penalty`: the native body carries `top_p`, `top_k`, `min_p` and `repeat_penalty`.

**G12** — `openai_compat` with its seven sampling arguments (`seed=-1`): each under its own name; no `repetition_penalty`.

Added in v7:

**G13** — `lmstudio`, `reasoning="low"`, streaming, with `temperature=1.0`, `top_p=0.9` and `max_tokens=256`: the Responses body carries `temperature`, `max_output_tokens`, `top_p` and `reasoning: {"effort": "low"}`.

**Compatibility.** Calls that pass none of the v6 arguments, with a finite in-range `temperature`, send requests to the same URLs with equal JSON bodies as 0.1.0: G1–G9 are unchanged. `extra` is still merged last (§6.4) and may override or add any of the v6 fields; it can override a value the setting checks approved, which is deliberate backward compatibility. `supported_settings()` and `check_settings()` describe the named arguments only and say nothing about what `extra` may carry.

---

## 8. Wire formats: responses

### 8.1 `Result` and `Usage`

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None

@dataclass(frozen=True)
class Result:
    text: str                                   # "" allowed
    reasoning: str | None                       # None when no reasoning text was produced
    usage: Usage | None                         # None when the backend reported none
    cost: float | None                          # USD; OpenRouter only; never 0.0 as a placeholder
    finish_reason: str | None                   # backend-reported; always None on lmstudio_native (§8.8)
    truncated: bool
    model: str | None                           # as reported by the backend
    provider: str | None                        # OpenRouter's upstream provider name, if reported
    response_id: str | None                     # None on lmstudio_native by default (store: false, §7.3)
    transport: Literal["chat_completions", "responses", "lmstudio_native"]
    reasoning_details: tuple[dict, ...] | None = field(hash=False)  # raw structured reasoning items, opaque
    raw_usage: dict | None = field(hash=False)  # backend's usage object (or native `stats`), verbatim
```

`reasoning_details` and `raw_usage` have no default; `field(hash=False)` only excludes them from `__hash__`. `repr(Result)` is the dataclass default. It contains no secret the package supplied; data a backend echoes is outside the guarantee (§4.3).

### 8.2 Usage normalisation

Usage parsing ports NEL's `normalize_provider_usage()`, with one fix (marked **fix**). A field is set only when its value is an `int` ≥ 0 and not a `bool`; otherwise it is `None`. If every field is `None`, `Result.usage` is `None`.

**Chat Completions and Responses** (`document["usage"]` must be a dict):

| `Usage` field | Source keys, first present wins |
|---|---|
| `input_tokens` | `prompt_tokens`, `input_tokens` |
| `output_tokens` | `completion_tokens`, `output_tokens` |
| `total_tokens` | `total_tokens`; else `input + output` when both are set |
| `reasoning_tokens` | `completion_tokens_details.reasoning_tokens`, then **fix:** `output_tokens_details.reasoning_tokens` |
| `cached_input_tokens` | `prompt_tokens_details.cached_tokens`, then **fix:** `input_tokens_details.cached_tokens` |
| `cache_write_tokens` | `prompt_tokens_details.cache_write_tokens` |

**Cost** (OpenRouter only): `usage.cost`, when it is an `int`/`float` ≥ 0 and not a `bool`, becomes `Result.cost` as a `float`. For all other kinds, `Result.cost` is always `None`. No request flag is needed: OpenRouter returns cost in `usage` by default, and NEL sends none.

**LM Studio native** (`document["stats"]` must be a dict; ports `_lmstudio_native_usage()`):

| `Usage` field | Source |
|---|---|
| `input_tokens` | `stats.input_tokens` |
| `output_tokens` | `stats.total_output_tokens` |
| `total_tokens` | `input + output` when both are set |
| `reasoning_tokens` | `stats.reasoning_output_tokens` |

`raw_usage` is the `usage` dict (or `stats` dict) copied as received.

### 8.3 Reasoning text helpers (port verbatim)

Port these NEL functions unchanged, as private helpers:

- `_text_fragment(value)`: if `value` is a string, return it. If it is a list, concatenate its string items and its dict items' `text` or `content` strings. Otherwise return `""`.
- `_reasoning_details_fragment(details)`: if `details` is not a list, return `""`. Otherwise prefer the `text` of items whose type is not `reasoning.encrypted`. If no item has text, fall back to concatenating `summary` strings.
- `_reasoning_fragment(delta)`: return the structured `reasoning_details` text if there is any. Otherwise return the first non-empty value of `reasoning`, `reasoning_content`, `thinking` (via `_text_fragment`). Never concatenate these aliases, because some providers mirror the same text in several of them.

### 8.4 Chat Completions — non-streaming

This ports `_complete_chat_nonstreaming()`. The changes are that no exception is raised for empty text or `length`, and the new fields.

- `choice = document["choices"][0]`; `message = choice["message"]`. A missing key or wrong type raises `ResponseFormatError`.
- `text` = `message["content"]` if it is a `str`; `""` if it is `None` or absent. Any other type raises `ResponseFormatError`.
- `reasoning` = `_reasoning_fragment(message)`, or `None`.
- `reasoning_details` = `tuple(message["reasoning_details"])` if it is a list, else `None`.
- `finish_reason` = `choice.get("finish_reason")` as `str`, or `None`.
- `truncated` = `finish_reason == "length"`.
- `model` = `document.get("model")` if it is a `str`.
- `provider` = `document.get("provider")` if it is a `str`.
- `response_id` = `document.get("id")` if it is a non-empty `str`. This ports `model_usage.generation_id()`.
- `usage` and `cost` per §8.2.

### 8.5 Chat Completions — streaming

This ports `_complete_chat_streaming()`, with the changes marked.

SSE line handling is shared by all routes (§8.9). For each JSON `document`:

1. **Change:** if `document.get("error")` is truthy, or any choice has `finish_reason == "error"`, close the connection and raise `BackendError(status=200, body=<redacted error JSON>)`. OpenRouter reports mid-stream errors this way, including in streams that consist of only the error chunk. NEL ignored these and returned partial text as success.
2. If `usage` is present (§8.2), keep it. A later value overwrites an earlier one.
3. If `id` is a non-empty `str`, keep it as `response_id`. Likewise keep `model` and `provider` when they are strings.
4. `choices = document.get("choices")`. If it is not a non-empty list, continue. `choice = choices[0]`; `delta = choice.get("delta") or {}`.
5. `r = _reasoning_fragment(delta)`. If non-empty, append it to reasoning and call `on_reasoning(r)`.
6. If `delta["reasoning_details"]` is a list, extend the `reasoning_details` accumulator with it.
7. `t = _text_fragment(delta.get("content"))`. If non-empty, append it to text and call `on_text(t)`.
8. If `choice.get("finish_reason")` is not `None`, set `finish_reason = str(...)`.
9. `"[DONE]"` ends reading.

**This route does not stop at `finish_reason`.** On OpenRouter, the final usage chunk arrives after the choice that carries `finish_reason` (S5). The route therefore reads until `[DONE]` or the end of the body, unlike the Responses and native routes, which stop at their terminal event (§8.7, §8.8).

After the stream:

- If no JSON event was received, raise `BackendError("provider returned no SSE events")`. **Change:** NEL fell back to non-streaming here.
- **Change:** if neither `[DONE]` nor any `finish_reason` was seen, raise `BackendError("stream ended before completion")`. A dropped connection must not look like success.
- `truncated = finish_reason == "length"`.

### 8.6 Responses — non-streaming

This ports `_responses_output()`, `_responses_reasoning()`, `_responses_reasoning_details()`, `_responses_finish_reason()` and `_responses_is_truncated()` verbatim.

- `text`: for each `output` item with `type == "message"`, for each `content` part with `type` in `{"output_text", "text"}`, concatenate its `text`.
- `reasoning`: for each `output` item with `type == "reasoning"`, take the first non-empty `_text_fragment` of its `summary`, `content` or `text` keys, and concatenate across items. `None` if empty.
- `reasoning_details`: tuple of the `output` items with `type == "reasoning"`, or `None`.
- `finish_reason`: if `status == "incomplete"`, then `incomplete_details.reason`, or `"incomplete"` if that is absent. If `status == "completed"`, then `"stop"`. Otherwise `status`, or `None` if `status` is absent.
- `truncated`: `status == "incomplete"` and `incomplete_details.reason` (lower-cased) is in `{"max_output_tokens", "max_tokens", "length"}`.
- If `status` is `"failed"` or `"cancelled"`, raise `BackendError`, with `document.get("error")` redacted in the body.
- `model` = `document.get("model")`; `response_id` = `document.get("id")`; `usage` per §8.2; `cost = None`; `provider = None`.

### 8.7 Responses — streaming

This ports `_complete_responses_streaming()` and `_responses_event_reasoning()`. For each JSON `document`, let `etype = document.get("type")`:

1. If `etype == "response.output_text.delta"`: `t = _text_fragment(document["delta"])`. If non-empty, append it to text and call `on_text(t)`.
2. Otherwise, if `etype` lower-cased contains `"reasoning"` and ends with `".delta"`: `r = _text_fragment(document["delta"])`. If non-empty, append it to reasoning and call `on_reasoning(r)`. (This is NEL's rule. It covers both reasoning-text and reasoning-summary deltas.)
3. **Change:** if `etype == "error"`, close the connection and raise `BackendError` with the redacted event.
4. If `document["response"]` is a dict (`nested`), update `response_id`, `model` and `usage` from it. If `etype` is in `{"response.completed", "response.incomplete", "response.failed"}`, set `final = nested` and **stop reading** (deviation #18). Leave the loop without waiting for the end of the HTTP body, then apply the after-stream rules below. A terminal event without a dict `response` does not set `final` and does not stop reading.
5. `"[DONE]"` is skipped, not treated as terminal (NEL behaviour).

**Why stop at the terminal event.** LM Studio's Responses documentation lists `response.completed` but does not say what follows it. Stopping on the event means a finished generation does not depend on how promptly the server, or a proxy, closes the body. Otherwise a slow close could delay the result or turn it into a `RequestTimeout`.

After the stream:

- If no JSON event was received, raise `BackendError("provider returned no SSE response events")`.
- If no text was streamed and `final` is set, take `text = _responses_output(final)`. If it is non-empty, call `on_text(text)` once, so the callbacks always reproduce `Result.text`.
- If no reasoning was streamed and `final` is set, take `reasoning = _responses_reasoning(final)`. If it is non-empty, call `on_reasoning(reasoning)` once.
- `finish_reason`, `truncated` and `reasoning_details` come from `final` (or `{}`), as in §8.6.
- If `final["status"]` is `"failed"`, raise `BackendError`.
- **Change:** if `final` is `None` (no completion event), raise `BackendError("stream ended before completion")`.

### 8.8 LM Studio native — non-streaming and streaming

This ports `_lmstudio_native_output()`, `_lmstudio_native_usage()`, `_lmstudio_native_generation_id()` and `_complete_lmstudio_native_streaming()`.

**Output parsing.** This applies to the response body, or to `chat.end.result` when streaming. For each `output` item:

- `type == "message"` with a string `content` → text.
- `type == "reasoning"` with a string `content` → reasoning, and the item is appended to `reasoning_details`.
- All other item types are ignored.

**Metadata:**

- `response_id` = `response_id` if it is a non-empty `str`, else `None`. With `store: false` (§7.3), LM Studio omits it, so this is normally `None`.
- `model` = `model_instance_id` if it is a `str`.
- `usage` per §8.2 (native); `cost = None`; `provider = None`.

**Truncation is a heuristic (deviation #8).** This API reports no stop reason. Let `cap` be the effective token cap (§6.4). Then:

```python
truncated = (cap is not None and usage is not None
             and usage.output_tokens is not None and usage.output_tokens >= cap)
finish_reason = None   # always, on this route
```

The package does not synthesise a `finish_reason` on this route. `truncated` is an inference, not a backend-reported fact, and the README MUST document both kinds of error:

- **False positive:** a response that ends naturally at exactly the cap reports `truncated=True`.
- **False negative:** when the backend reports no usage, `truncated` is `False` even if the output was cut.

NEL always reported `"stop"` and never detected truncation on this route.

**Streaming events.** The SSE `event:` lines are ignored; dispatch on the JSON `type` field:

| `type` | Action |
|---|---|
| `message.delta` | `content` string → text, `on_text` |
| `reasoning.delta` | `content` string → reasoning, `on_reasoning` |
| `error` | Record `error.message`, or `error.type`, or a default message. **Do not stop reading**, because `chat.end` still follows with whatever was generated. |
| `chat.end` | If `document["result"]` is a dict: set `final` to it and **stop reading** (deviation #18), without waiting for the end of the HTTP body. A `chat.end` without a dict `result` does not set `final` and does not stop reading. |
| anything else (`chat.start`, `model_load.*`, `prompt_processing.*`, `*.start`/`*.end`) | Ignore |

After the stream:

- If no JSON event was received, raise `BackendError("provider returned no SSE native chat events")`.
- If `final` is set: fill text from it if no text was streamed, and likewise reasoning. Call each callback once in that case. Take usage, `response_id` and `model` from `final`.
- If an error was recorded, raise `BackendError(<redacted message>)`. This happens after `final` has been processed, as in NEL. Stopping at `chat.end` does not change this rule: LM Studio documents that an `error` event is still followed by `chat.end` carrying whatever was generated, and that the stream concludes with `chat.end`.
- **Change:** if `final` is `None`, raise `BackendError("stream ended before completion")`.

### 8.9 SSE line handling (shared)

This ports NEL's line loop, with one deviation (#16). Iterate the HTTP response line by line (`for raw_line in response`). Then:

1. Decode with `raw_line.decode("utf-8", errors="replace").strip()`. Line-wise decoding is safe because a multi-byte UTF-8 sequence never contains the byte `\n`.
2. Skip the line if it is empty or starts with `:` (comment or keep-alive, e.g. `: OPENROUTER PROCESSING`). Skip lines that do not start with `data:`, such as `event:`, `id:` and `retry:`.
3. `data = line[5:].strip()`. Skip it if it is empty.
4. `"[DONE]"` is passed through to the route handler.
5. Otherwise `json.loads(data)`. **Deviation #16:** if decoding fails, or the result is not a dict, raise `ResponseFormatError(status=200)`. Its message is `"malformed SSE data event"`, and its `body` is the data, redacted and then truncated to 600 characters (§9.3). The caller's `try/finally` closes the connection (§8.10). NEL skipped such lines, which can return success with text missing.

**Supported streaming format:** one complete JSON object per `data:` line. This is what the supported routes emit. SSE also allows one event's data to span several `data:` lines; that form is not supported. Line-wise parsing splits it into invalid fragments, so it raises `ResponseFormatError` under step 5 rather than being silently dropped.

Implement this as a generator that yields either the string `"[DONE]"` or a dict.

### 8.10 Callbacks and cancellation

- Callbacks are invoked synchronously, in the calling thread, in arrival order, and only with non-empty strings.
- No callback is invoked after `complete()` returns or raises.
- Invariant: `"".join(all on_text args) == Result.text`, and `"".join(all on_reasoning args) == (Result.reasoning or "")`.
- **Cancellation:** a callback may raise `Cancelled()`. The package then closes the HTTP response immediately, sets `exc.partial = Result(...)` from what has been received so far (`finish_reason="cancelled"`, `truncated=False`, `usage=None`, `cost=None`), and re-raises the same exception object.
- Any other exception raised by a callback: close the response and re-raise it unchanged. Do not wrap it and do not redact it (§4.3).
- Any exception the package raises while reading a stream (§8.5, §8.7–§8.9, §9.2) also closes the response before it propagates.
- When a route stops reading at its terminal event (§8.7, §8.8), the response is closed the same way, without reading the rest of the body. Anything after the terminal event is neither parsed nor validated, so malformed data there does not raise. §8.9's strictness (S4) applies to data read before the terminal event.
- **What closing guarantees.** Closing the HTTP response locally is the package's only cancellation mechanism, and the only thing it guarantees. Whether the backend then stops generating is backend behaviour. It is expected on LM Studio, and on OpenRouter for providers that support cancellation, but it is neither guaranteed nor tested (S11 checks only the local disconnect).

---

## 9. Errors

### 9.1 Hierarchy

```python
class ModelSpellError(Exception): ...
class ConfigError(ModelSpellError): ...            # invalid arguments / endpoint
class UnsupportedSetting(ModelSpellError): ...     # valid setting the chosen kind cannot express
class UnsupportedOperation(ModelSpellError): ...   # e.g. list_providers on lmstudio
class AuthError(ModelSpellError):                  # missing key, HTTP 401/403
    status: int | None
class ConnectError(ModelSpellError): ...           # endpoint unreachable
class RequestTimeout(ModelSpellError): ...         # socket timeout (connect or read)
class BackendError(ModelSpellError):               # server-reported failure, including redirects
    status: int | None                             # HTTP status; 200 for in-stream errors; None if n/a
    body: str                                      # redacted, then truncated to ≤ 1000 chars
class ResponseFormatError(BackendError): ...       # body or SSE event not parseable / unexpected shape
class Cancelled(ModelSpellError):
    partial: Result | None                         # set by the package
```

`Cancelled.__init__` takes an optional message and sets `partial = None`.

### 9.2 Mapping (ports NEL `_read_json_response()`, `_open_stream()`, `_http_error()`)

| Condition | Raised |
|---|---|
| `HTTPError` 300–399 | `BackendError(status=code)`; redirect message (§9.5) |
| `HTTPError` 401 or 403 | `AuthError(status=…)`; message `"authentication failed (HTTP {code}) for {url}: {body}"` |
| `HTTPError` 404/405 on the `responses` route | `BackendError`; the message says LM Studio 0.3.29+ is required for `/v1/responses` |
| `HTTPError` 404/405 on the `lmstudio_native` route, or on LM Studio `list_models` | `BackendError`; the message says LM Studio 0.4.0+ is required for `/api/v1/...` |
| Any other `HTTPError` | `BackendError(status=code)`; message `"provider returned HTTP {code} for {model!r} at {url}: {body}"` |
| `URLError` (not `HTTPError`) | `ConnectError`; message `"endpoint unreachable at {url}: {reason}"` |
| `TimeoutError` / `socket.timeout`, including when it is a `URLError`'s `reason` | `RequestTimeout`; message `"request timed out after {timeout}s at {url}"` |
| `OSError` while reading a stream | `BackendError`; message `"stream failed for {model!r}: {exc}"` |
| Non-JSON or non-dict body (non-streaming) | `ResponseFormatError`, with the body redacted and then truncated to 600 characters |
| Malformed SSE data event | `ResponseFormatError(status=200)` (§8.9) |

**Reading error bodies.** Read at most 65,536 bytes: `exc.read(65536).decode("utf-8", errors="replace")`. Redact the text (§9.3), then truncate it to 1000 characters, then close `exc`. Check whether `URLError.reason` is itself a `TimeoutError` before mapping the error to `ConnectError`.

### 9.3 Redaction

- **Redact.** When a key resolved for the call, every string the package places in an exception it raises has every occurrence of the key value replaced with `***`. This covers messages, `body`, and any URL, reason or `Location` text.
- **Order: redact first, truncate second.** Never truncate text that still contains the unredacted key: truncation can cut through the key and leave a prefix that replacement no longer matches. This applies to the 1000-character error body, the 600-character malformed-body excerpt, and the malformed SSE excerpt.
- Never include request headers in any message.
- **Exception chaining.** Exceptions raised in response to urllib, socket or JSON-decoding errors are raised `from None`. Put the needed details (status, URL, reason, redacted body) in the message instead. `from None` sets `__suppress_context__`, so the original exception does not appear in formatted tracebacks. It remains reachable as `__context__` for a debugger, outside the guarantee.
- **Scope.** This covers exceptions the package constructs (§4.3). Exceptions raised by callbacks pass through unchanged.
- Tests: E5.

### 9.4 Timeout semantics

`timeout` is passed to `opener.open(request, timeout=timeout)`. It applies to the connection attempt and to each individual socket read, so during streaming it acts as an idle timeout. The NEL default of 900 s is kept, because local models can spend minutes on prefill before the first token. There is no total-duration cap.

**Consequence for non-streamed calls.** A non-streamed request receives no bytes until generation has finished. So `timeout` is effectively a limit on prefill plus the whole generation. For example, a 16,384-token generation at about 20 tokens/s takes about 800 s before prefill. Long local generations should be streamed (§6.2). The README MUST state this.

### 9.5 Redirects are not followed (deviation #15)

Each call builds its opener with `urllib.request.build_opener(_NoRedirectHandler())`. `_NoRedirectHandler` subclasses `urllib.request.HTTPRedirectHandler` and overrides `redirect_request(self, req, fp, code, msg, headers, newurl)` to return `None`. urllib then raises `HTTPError` with the 3xx status, and the package maps it as below. This applies to `complete()`, `list_models()` and `list_providers()`.

**Why redirects are not followed.** urllib's default redirect handler does two harmful things, both verified on Python 3.13:

- It re-sends every request header except `Content-Length` and `Content-Type`, including `Authorization`, to the redirect target, whatever its host.
- It turns a POST answered with 301, 302 or 303 into a GET with no body.

Following a redirect would therefore leak the key and silently replace the completion request with an unrelated one.

**Mapping:** an `HTTPError` with a status from 300 to 399 raises `BackendError(status=code)` with this message:

`"endpoint redirected (HTTP {code}) from {url} to {location}; redirects are not followed — set base_url to the final URL"`

`location` is the redacted `Location` header, or `"<no Location header>"` if there is none. The body is read as in §9.2.

---

## 10. Discovery

### 10.1 Types

```python
@dataclass(frozen=True)
class Pricing:
    prompt: float | None          # USD per input token
    completion: float | None      # USD per output token
    raw: dict = field(hash=False)

@dataclass(frozen=True)
class ModelInfo:
    id: str                                   # pass this to complete()
    name: str | None
    context_length: int | None
    loaded: bool | None                       # lmstudio only
    supports_tools: bool | None
    supports_reasoning: bool | None
    reasoning_options: tuple[str, ...] | None # lmstudio only
    pricing: Pricing | None                   # openrouter only
    raw: dict = field(hash=False)             # the server's entry, verbatim

@dataclass(frozen=True)
class ProviderInfo:
    name: str                                 # display name (provider_name)
    slug: str | None                          # OpenRouter's endpoint `tag`, verbatim; None if absent
    context_length: int | None
    max_completion_tokens: int | None
    quantization: str | None
    pricing: Pricing | None
    supported_parameters: tuple[str, ...]
    raw: dict = field(hash=False)             # the endpoint entry, verbatim
```

`raw` has no default in any of these types; `field(hash=False)` only excludes it from `__hash__`.

A `ModelInfo` returned by `list_models()` for the same endpoint may be passed as
`complete(model=...)`; passing one from another endpoint is the caller's error and is not detected.

`ProviderInfo` normalises only the fields a provider selector needs. Other endpoint metadata, including `status`, the uptime and latency figures, `max_prompt_tokens` and `service_tier`, is available in `raw` and is not part of the public contract.

`Pricing` values arrive as decimal strings from OpenRouter. Parse them with `float()`; on failure or a negative value, use `None`.

### 10.2 `list_models(endpoint, *, timeout: float = 15.0) -> list[ModelInfo]`

This is a GET with `Accept: application/json`, plus `Authorization` if a key resolves, plus `endpoint.headers`. It is sent through the same no-redirect opener (§9.5). The 15 s default is NEL's `CATALOGUE_TIMEOUT`. The result is a new `list`, sorted by `id`. Entries with a duplicate `id` are dropped; the first wins.

| `kind` | URL | Parsing |
|---|---|---|
| `openrouter` | `{base_url}/models` | For each entry in `data`: `id`, `name`, `context_length`, `pricing` (`prompt`, `completion`). `supports_tools = "tools" in supported_parameters` and `supports_reasoning = "reasoning" in supported_parameters`, both `None` if `supported_parameters` is absent. `loaded = None`; `reasoning_options = None`. |
| `lmstudio` | `{native_root}/api/v1/models` | For each entry in `models` with `type == "llm"`: `id = key`, `name = display_name`, `context_length = max_context_length`, `loaded = len(loaded_instances) > 0`, `supports_tools = capabilities.trained_for_tool_use`. `reasoning_options = tuple(capabilities.reasoning.allowed_options)` if present. `supports_reasoning = reasoning_options is not None and any(o != "off" for o in reasoning_options)`. `pricing = None`. Needs LM Studio 0.4.0+. |
| `openai_compat` | `{base_url}/models` | Ports NEL `_ids()`. Rows are `document["data"]` if the document is a dict, else the document itself if it is a list. `id = row["id"] or row["slug"] or row["name"]`, or the row itself if it is a string. All other fields are `None`. |

Failures raise the §9 errors, and nothing is cached. If a mandatory field such as `id` or `key` is missing, that entry is skipped. A document of the wrong top-level shape raises `ResponseFormatError`. (Deviation: NEL turned discovery failures into "notes" for the UI. The package raises, and callers can still pass any model ID to `complete()` by hand.)

### 10.3 `list_providers(endpoint, model_id, *, timeout: float = 15.0) -> list[ProviderInfo]`

- For any `kind` other than `openrouter`, raise `UnsupportedOperation`, with no I/O.
- If no key resolves, raise `AuthError`, with no I/O.
- `model_id` must have the form `author/slug`, split on the first `/` into two non-empty parts. Otherwise raise `ConfigError`. Quote each part with `urllib.parse.quote(part, safe=":-._~")`.
- GET `{base_url}/models/{author}/{slug}/endpoints`, through the no-redirect opener (§9.5).
- Parse `document["data"]["endpoints"]`. Each entry becomes a `ProviderInfo`:
  - `name = provider_name or name`, whichever is a non-empty string. If neither is, skip the entry.
  - `slug = tag` if `tag` is a non-empty `str`, passed through **verbatim**: no case change, no trimming, no splitting on `/`. Otherwise `slug = None`.
  - **No value is ever derived from `provider_name` or `name` to fill `slug`.** A display name is not evidence of a routing identifier.
  - `context_length`, `max_completion_tokens`, `quantization` and `supported_parameters` use the same key names. `pricing` follows §10.1.
- Order is kept as returned. The function returns a new `list`.
- A 404 response maps to `BackendError` as usual.

**What `slug` means.** OpenRouter's routing documentation distinguishes two kinds of slug. A base provider slug, such as `deepinfra`, matches all of that provider's endpoint variants. An endpoint slug, such as `deepinfra/turbo`, matches exactly one endpoint. The endpoints API reference marks `tag` as required, but shows only one example value (`openai`). It does not say which kind of slug `tag` is, or whether matching is case-sensitive. The package therefore passes `tag` through unchanged and documents `slug` as "OpenRouter's endpoint tag, passed verbatim to `provider={"order": [...]}` or `"only"`". Verification V1 (§17) checks the discovery-to-routing round trip.

**`slug is None` never blocks routing.** Callers can always pass an identifier they know directly in `provider=` (§7.1).

(Deviation: NEL listed OpenRouter's global `/providers` catalogue, which does not answer "which providers serve this model".)

---

## 11. Package layout (guidance, non-normative)

This section is guidance. Tests check the public API (K1) and the behaviour; they do not check private module names or helper signatures.

```
modelspell/
  __init__.py          # re-exports + __all__ only
  py.typed
  _types.py            # Endpoint, Message, Result, Usage, ModelInfo, ProviderInfo, Pricing
  errors.py            # §9.1
  _http.py             # per-call no-redirect opener, build Request, post (json / stream), get_json,
                       # error mapping, redaction
  _sse.py              # iter_sse(response) -> Iterator[dict | "[DONE]"]
  _usage.py            # §8.2
  _reasoning.py        # §8.3 helpers
  _complete.py         # complete(): validate, normalise input, select route, merge extra,
                       # compute the effective token cap, dispatch
  _routes/
    __init__.py
    chat.py            # build_body, parse_response, parse_stream  (§7.1, §8.4, §8.5)
    responses.py       # §7.2, §8.6, §8.7
    lmstudio_native.py # §7.3, §8.8
  _discovery.py        # list_models, list_providers
tests/
  conftest.py          # local HTTP server fixture (§12.1)
  fixtures/            # captured and synthetic responses (§12.2)
    README.md          # capture procedure + coverage table
  golden/              # golden request bodies (§7.4)
  test_*.py
examples/
  basic.py             # §12.9
scripts/
  smoke_installed.py   # dev-only; K3 smoke test against an inline stub server
  build_release.py     # dev-only; §18.2
ui/                    # dev-only test UI, never shipped (§18.3)
  __main__.py, server.py, index.html
docs/                  # this spec, design history
.github/workflows/     # tests.yml, release.yml (§18.2)
modelspell/_version.py # __version__ only (§18.1)
pyproject.toml
README.md
NEWS.md
DEVEL.md
LICENSE
```

A suggested shape for the route modules. In v6 each route module also exposes `FIELDS` (argument → wire name; the chat module keys it by `kind`, because `repetition_penalty` is OpenRouter-only) and the range table `RANGES`; `_complete.py` holds one route-selection helper and one setting-check function used by `complete()`, `supported_settings()` and `check_settings()`. `build_body()` takes a `settings` mapping holding only the non-`None` sampling arguments and tests `is not None`, never truthiness, so zero values are sent. Avoid a general parameter-schema engine, backend auto-detection, new endpoint kinds, dynamic capability discovery or persisted settings.

```python
def build_body(messages, model, *, reasoning, provider, settings, streaming, kind) -> dict
def parse_response(document: dict, *, kind, token_cap) -> Result
def parse_stream(events: Iterator[dict | str], *, kind, token_cap, on_text, on_reasoning) -> Result
```

`token_cap` is the effective token cap, read from the merged body (§6.4). One module (here `_complete.py`) should own opening and closing the HTTP response, with a single `try/finally` around stream parsing. The behaviour that results — the connection is closed on every exit path, including an early stop at a terminal event (§8.10) — is normative and tested by S4, S6, S11, S12 and S13.

---

## 12. Tests (required)

All unit tests run offline against a local HTTP server. Live tests are opt-in.

### 12.1 Test server fixture

`tests/conftest.py` provides a `server` fixture: a `http.server.ThreadingHTTPServer` on `127.0.0.1:0`, started in a background thread. It:

- records every request (method, path, headers, parsed JSON body);
- replies with a scripted response: status, headers, and a body given either as bytes or as a list of byte chunks sent with `flush()` and optional delays;
- can close the connection mid-body;
- can delay before the first byte (for timeout tests);
- exposes `server.url` and `server.requests`;
- can be instantiated more than once in a test (redirect tests need two servers);
- can **hold the body open** after a scripted prefix until a release `threading.Event` is set. While holding, the handler polls its socket (`select` with a short interval, then `recv(1, socket.MSG_PEEK)`; an empty read means the client closed). It sets a `client_closed` event if the client disconnects before release. Teardown sets the release event for every held response. On Python 3.13 this works for both chunked and connection-close bodies: the client receives a flushed line while the body stays open, and the handler detects the client's close.

An autouse fixture removes `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`, in both upper and lower case, for every test except E9. This keeps local test traffic away from any proxy configured in the CI environment.

Tests build `Endpoint(kind=..., base_url=server.url + "/v1")`.

### 12.2 Fixtures

**Capture procedure.** There is no recording script in v1. `tests/fixtures/README.md` documents capture with `curl`. For a streaming capture:

```sh
curl -sS -N https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" -H "Content-Type: application/json" \
  -d @request.json > tests/fixtures/chat/<case>.sse
grep -cF "$OPENROUTER_API_KEY" tests/fixtures/chat/<case>.sse   # must print 0
```

For a non-streaming capture, drop `-N` and write to `<case>.json`. For LM Studio, use the local URL and omit the header. Check every captured file for the key before committing it.

**Two classes of fixture, both permanent:**

- **Captured:** raw response bodies from a real server. A captured success-path fixture is required for each route and each discovery endpoint *where a server is available during implementation*. That covers chat (OpenRouter), chat (`openai_compat`), Responses, native, and each discovery URL.
- **Synthetic:** hand-written, with `synthetic` in the file name. These are permitted permanently for cases that cannot be produced on demand: mid-stream errors, error-only streams, disconnects, malformed events and bodies, `failed` / `cancelled` statuses, exact-cap native output, entries without `tag`, and the paired fixtures for S2. A synthetic fixture is also permitted for a success path when no server was available. It is not a defect to be replaced later.

`tests/fixtures/README.md` holds a coverage table that labels each fixture "captured" or "synthetic". Captured rows give the backend, its version where known, and the capture date. Synthetic success-path rows give the reason no capture was possible.

Seed the native-route fixtures from NEL's test `workflows/proforma_v1/tests/test_lmstudio_reasoning_none.py`, which contains real-shaped non-streaming and streaming payloads. Adapt them to `store: false` (no `response_id`).

**Paired fixtures (S2)** are constructed, not recorded. Two live calls are two separate generations, even at temperature 0. For each route, at least one pair (`<stem>.json` and `<stem>.sse`) is written from one canonical response, so both encode the same text, reasoning, usage and finish state.

Minimum fixture set per route:

| Route | Cases |
|---|---|
| chat (OpenRouter) | text only; text + reasoning (`reasoning` field); text + `reasoning_details` with both text and an encrypted item; `finish_reason="length"`; usage with cost, `cost_details`, cached tokens; mid-stream error chunk; error-only stream; malformed `data:` event; one S2 pair |
| chat (openai_compat) | text only; `reasoning_content` field; no usage |
| responses | text only; text + reasoning (streamed via reasoning delta events); `incomplete` / `max_output_tokens`; `failed`; usage with `output_tokens_details.reasoning_tokens`; one S2 pair |
| lmstudio_native | text only (no `response_id`); text + reasoning; `error` event followed by `chat.end`; progress events interleaved; output exactly at the cap; no `stats`; one S2 pair |
| discovery | OpenRouter `/models` (trimmed to ~3 entries); OpenRouter `/models/{a}/{s}/endpoints` (captured, plus a synthetic variant with a mixed-case `tag`, a `tag` containing `/`, and an entry with no `tag`); LM Studio `/api/v1/models` with one LLM (loaded), one LLM (not loaded), one embedding; generic `/models` |

### 12.3 Request construction

| ID | Test |
|---|---|
| R1 | Golden examples G1–G13 (§7.4): exact URL, method and parsed-JSON body equality. G1–G9 are unchanged from v5; G13 added in v7. |
| R2 | For every optional argument, `None` produces no key in the body. Cover all three routes. Zero values (`temperature=0`, `top_k=0`, `min_p=0`, `presence_penalty=0`, `seed=0`) are sent as `0`, not omitted. |
| R3 | The `reasoning` mapping (§6.3, §7) for every value × every kind. `openai_compat` with any non-`None` value raises `UnsupportedSetting`. |
| R4 | `extra` overrides apply last, including overriding `max_tokens` (G8), `max_output_tokens` and `store`. `extra` containing `model` or `stream` raises `ConfigError`. `extra` never changes the route. `extra` overrides a field set from a v6 argument (`extra={"top_k": 5}` with `top_k=40` sends 5), and may add a field the route does not support (`repeat_penalty` on `openai_compat`) without raising. |
| R5 | Each `ConfigError` / `UnsupportedSetting` / before-I/O `AuthError` case in §4, §6.1, §6.7.2 and §6.8: the server receives **zero** requests. |
| R6 | Native flattening: single user; system + user; multiple system messages (including empty ones); full repair history (G3); history with no system message. |
| R7 | Headers: `Authorization` is present if and only if a key resolves; `endpoint.headers` are sent; `Content-Type` is correct. |
| R8 | Input normalisation: `str`; `Message` list; dict list; mixed; a bad role, an extra key, non-str content, and an empty list each raise `ConfigError`. |
| R9 | Native body always contains `"store": false`, in both streaming and non-streaming modes, unless `extra` overrides it. Responses body never contains `store` unless `extra` sets it. No route body contains `previous_response_id` unless `extra` sets it. |
| R10 | Unsupported settings: every sampling argument × every route where it is "—" in §6.7 raises `UnsupportedSetting`, naming the argument and the route. Zero requests. On the Responses route (v7, M21-5) the message contains `reasoning="off"` exactly for `top_k`, `min_p` and `repetition_penalty` (a list the test keeps itself), and contains "(effect unverified)" for every setting; on other routes it contains neither. |
| R11 | Value checks (§6.8.1), each `ConfigError` with zero requests: `bool` for each numeric argument and `seed`; `float` for `top_k` and `seed`; `top_p`, `min_p` = −0.01 and 1.01; `top_k` = −1; `repetition_penalty` = −0.1; `stop` as `"END"`, `[]`, `[""]`, `["a", 1]`. Boundaries 0 and 1 for `top_p` and `min_p`, and 0 for `top_k`, are accepted. |
| R12 | Finiteness: `inf`, `-inf` and `nan` for `temperature` and every new numeric argument raise `ConfigError`, with zero requests. |
| R13 | Route ranges (§6.8.2): OpenRouter `temperature` and `repetition_penalty` 2 accepted, 2.01 refused; `presence_penalty`/`frequency_penalty` ±2 accepted, ±2.01 refused; LM Studio native `temperature` 1 accepted, 1.01 refused; LM Studio Responses and `openai_compat` `temperature` 5 accepted. |
| R14 | `supported_settings()` equals the §6.9 table for every `kind` × `reasoning`; for each combination every name in the set is accepted by `complete()` and reaches the body, and every generation setting not in it raises `UnsupportedSetting`; an unknown `kind`, an unknown `reasoning`, and `openai_compat` with a reasoning value raise as `complete()` does; no network call, no environment read. |
| R15 | `stop` given as a `tuple` is sent as a JSON array in the same order. |
| R16 | `check_settings()` agrees with `complete()`: every refusing case of R10–R13 raises the same exception class and message from both; every accepting case of R10–R14 returns `None`; no network or environment: an OpenRouter case with no key passes `check_settings()` while `complete()` raises `AuthError`. |
| R17 | `ModelInfo` as model, streaming and non-streaming: (a) native `reasoning="off"` with options `None` or `()` omits only `reasoning`, retaining id and `store: false` (MSGS and G3 parity); (b) options `("off", "on")` or `("on",)` retain `reasoning: "off"`, matching string-model bodies; (c) `top_k=20`, `min_p=0.05`, `repetition_penalty=1.1` send native fields including `repeat_penalty`, without reasoning for absent options; (d) LM Studio reasoning `None`/`"low"`, OpenRouter and `openai_compat` reasoning `None` have identical URL/body to string `"m"`; (e) empty/non-string ModelInfo ids, `123` and `None` raise `ConfigError` with §6.1's model message and zero requests. Native HTTP 500 errors name the id, not the ModelInfo repr; Responses `top_k=1` refusal matches the string-model message. |

### 12.4 Response parsing

| ID | Test |
|---|---|
| P1 | Every non-streaming fixture → the expected `Result`, compared field by field: `text`, `reasoning`, `usage`, `cost`, `finish_reason`, `truncated`, `model`, `provider`, `response_id`, `transport`, `reasoning_details`. |
| P2 | Empty answer text returns `Result(text="")`, with no exception. A truncated response returns `truncated=True`, with no exception. |
| P3 | Cost: OpenRouter with cost → float; OpenRouter without cost → `None`; `lmstudio` and `openai_compat` responses that contain a `usage.cost` field → still `None`. |
| P4 | Usage: chat-style keys; Responses-style keys, including the reasoning-token and cached-token **fix**; native `stats`; invalid values (negative numbers, bools, strings) → `None`; no usage → `Result.usage is None`. |
| P5 | Reasoning alias selection: when `reasoning_details` and `reasoning` carry the same text, the text appears once. Encrypted details are ignored. The summary fallback works. |
| P6 | Responses `failed` / `cancelled` → `BackendError`. |
| P7 | **Native truncation heuristic**, with effective cap C: `output_tokens ≥ C` → `truncated=True`; `output_tokens < C` → `False`; no `stats` → `False`, with no exception; no cap → `False`. `finish_reason is None` in every case. **The effective cap comes from the body:** with `max_tokens=1000, extra={"max_output_tokens": 2000}` and 1500 output tokens → `False`; with `max_tokens=None, extra={"max_output_tokens": 1500}` and 1500 output tokens → `True`. Run both streaming and non-streaming. |
| P8 | Malformed bodies (not JSON, JSON array, missing `choices`, non-string `content`) → `ResponseFormatError`. |
| P9 | Native response without `response_id` → `Result.response_id is None`, with no exception. |

### 12.5 Streaming

| ID | Test |
|---|---|
| S1 | **Chunk-boundary invariance.** Serve every streaming fixture (a) in one chunk, (b) one byte per chunk, and (c) split at 20 seeded-random offsets, including inside multi-byte UTF-8 characters (put `é` and an emoji in the fixtures). The `Result` must be identical in every case. |
| S2 | **Stream vs non-stream equivalence.** For each constructed pair (§12.2), the streamed and non-streamed `Result`s match on `text`, `reasoning`, `usage`, `finish_reason`, `truncated`. Live calls are never compared with each other; each is checked independently (§12.9). |
| S3 | Callback invariants (§8.10): the concatenations equal `text` and `reasoning`; no empty strings are passed; the order matches arrival; nothing is called after return. |
| S4 | SSE handling: `:` comment lines, `event:` / `id:` / `retry:` lines and blank lines are ignored; `[DONE]` ends the chat route. A `data:` line holding invalid JSON, or JSON that is not an object, raises `ResponseFormatError(status=200)` on every route. It is raised even when a valid completion marker follows, and the connection is closed. A single JSON object split across two consecutive `data:` lines raises `ResponseFormatError`. |
| S5 | OpenRouter's final usage chunk (a single choice with an empty delta) is parsed into usage. |
| S6 | Mid-stream error chunk → `BackendError(status=200)`, raised without returning partial text, and the connection is closed. An error-only stream → `BackendError`. |
| S7 | Stream closed early (no `[DONE]` or `finish_reason`, no `response.completed`, no `chat.end`) → `BackendError("stream ended before completion")`. |
| S8 | A stream that produces no events → `BackendError`, and exactly **one** request was made (no fallback). |
| S9 | Responses fallback: final-only text and reasoning, with no deltas, still reach `on_text` / `on_reasoning` exactly once. |
| S10 | Native: an `error` event followed by `chat.end` → `BackendError`, raised after the stream is consumed. |
| S11 | **Cancellation:** `on_text` raises `Cancelled()` after the second chunk. The exception propagates; `exc.partial.text` equals the first two chunks; `exc.partial.finish_reason == "cancelled"`; the test server observes the client disconnect. The test asserts only local closure, not that any backend stopped generating (§8.10). |
| S12 | A callback raising `ValueError` → the same `ValueError` object propagates unwrapped, and the connection is closed. |
| S13 | **Terminal events end the read.** This test does not measure elapsed time. Serve each stream below with the §12.1 hold-open mode, so the body stays open until teardown sets the release event. The streams are: Responses ending in `response.completed`; Responses ending in `response.incomplete`; native ending in `chat.end`; native with an `error` event before `chat.end`; and chat ending in `[DONE]`. Call `complete()` with `timeout=2` in a worker thread, joined with a 30 s watchdog; a watchdog expiry fails the test as a hang. Assert two things. First, each call returns its normal `Result`, or for the native `error` case raises the §8.8 `BackendError`, and never raises `RequestTimeout`. Second, the server's `client_closed` event is set (wait on it, bounded by the watchdog). A correct implementation never reads after the terminal event, so its outcome does not depend on scheduling. `timeout=2` only bounds how long a broken implementation takes to fail with `RequestTimeout`. **Negative case:** a terminal event without its dict payload (`response` or `result`) does not end the read, so that call raises `RequestTimeout`. |

### 12.6 Errors, transport and secrets

| ID | Test |
|---|---|
| E1 | Status mapping: 401 and 403 → `AuthError`; 400, 404, 422, 429, 500 and 502 → `BackendError` with the matching `status`; 404/405 on the Responses and native routes → the version messages; 301, 302, 303, 307 and 308 → `BackendError` with the redirect message (§9.5). |
| E2 | Connection refused (port with no listener) → `ConnectError`, whose message names the URL. |
| E3 | Server delays the first byte past `timeout` → `RequestTimeout`. Server stalls mid-stream past `timeout` → `RequestTimeout`. A stream with gaps shorter than `timeout`, but a total longer than `timeout`, completes. |
| E4 | Exactly one request per `complete()` call, in every test that reaches the server. Assert this in the fixture's teardown. |
| E5 | **Sentinel secrecy**, with key `"sk-SENTINEL-1234567890"`. For each package error path, assert the sentinel is absent from `str(e)`, `repr(e)`, `"".join(traceback.format_exception(e))` and, where present, `e.body`. The paths are: 401; 500 whose body echoes the key; 500 whose body is 995 × `"x"` + key + 50 × `"y"`, so the key straddles the 1000-character limit, where additionally `"sk-S"` must not appear in `e.body`; connection refused; timeout; a malformed non-streaming body echoing the key; a malformed SSE event echoing the key; a mid-stream error echoing the key; a 302 whose `Location` contains the key; a `base_url` whose query string contains the key. Also assert that `repr(endpoint)` contains `***` and not the sentinel, and that `repr(result)` for a normal (non-echoing) fixture does not contain it. **Boundary check:** a callback raising `ValueError("sk-SENTINEL-1234567890")` propagates unchanged with the sentinel intact, documenting the §4.3 limit. |
| E6 | Key resolution: an explicit key wins over the environment; the env var is used when the key is empty; whitespace-only counts as empty; OpenRouter with no key → `AuthError` with no request; `lmstudio` with no key → no `Authorization` header. |
| E7 | `Endpoint` validation: bad kind; missing `base_url` for `openai_compat`; non-http URL; trailing-slash normalisation; `Authorization` in `headers` → `ConfigError`. |
| E8 | **Redirects are not followed.** Server A answers `POST /v1/chat/completions` with 302 and `Location` pointing at server B. `complete()` raises `BackendError(status=302)` whose message names B's URL. Server B receives **zero** requests, and A receives exactly one. Repeat with 307, and with 301 on `list_models()` (GET). |
| E9 | **Proxy still honoured.** Set `HTTP_PROXY` to a test server acting as a proxy, and call `complete()` against an `http://` URL on an unreachable host. The proxy server receives the request with the absolute URL as its path, which shows that the per-call opener includes `ProxyHandler`. |

### 12.7 Discovery

| ID | Test |
|---|---|
| D1 | Each discovery fixture → the expected `ModelInfo` list (a `list`, sorted by id, de-duplicated, with embedding models excluded on LM Studio). |
| D2 | `list_providers` fixtures → the expected `ProviderInfo` list. `slug` equals `tag` exactly, keeping case and any `/` suffix. An entry with no `tag` → `slug is None`; no `slug` is ever derived from `provider_name`. A `model_id` without `/` → `ConfigError` with no I/O. `lmstudio` / `openai_compat` → `UnsupportedOperation` with no I/O. |
| D3 | `list_models` on OpenRouter with no key: the request is sent without `Authorization` and succeeds. |
| D4 | Failure: HTTP 500 → `BackendError`; wrong top-level shape → `ResponseFormatError`; LM Studio 404 → the 0.4.0 message. |
| D5 | No caching: two calls → two requests, and two distinct `list` objects. |

### 12.8 Packaging and concurrency

| ID | Test |
|---|---|
| K1 | Public API snapshot: `modelspell.__all__` equals the exact list in §3 (in v6, `supported_settings` and `check_settings` follow `list_providers`), and the dataclass field names of every public type equal the lists in this spec. Private module names and signatures (§11) are not snapshotted. |
| K2 | `import modelspell` under a patched `socket.socket` that raises on use: the import succeeds, so it makes no network calls. |
| K3 | Build the wheel and `pip install` it into a clean venv with no other packages. Then run `python -c "import modelspell; print(modelspell.__all__)"` and `scripts/smoke_installed.py`, which starts an inline `http.server` stub and makes one `complete()` call against it. Before that call, the script removes the proxy variables from its own environment (as in §12.1), so a CI proxy cannot intercept the localhost request. Runs in CI. |
| K4 | Concurrency: 16 threads call `complete()` against the test server, each with a distinct prompt echoed back in the response. Every result matches its own prompt. |
| K5 | A `grep`-style test: there are no imports outside the standard library in the `modelspell/` sources, and `urlopen` is not called. |
| K6 | `hash()` succeeds for an instance of every public dataclass type, with its dict-valued fields populated. Equality still compares those fields. |
| K7 | The built wheel contains exactly `modelspell/**`, `METADATA`, `WHEEL`, `RECORD` and `licenses/LICENSE`, and its metadata reports `Name: modelSpell`, the `_version.py` version and `License-Expression: MIT` (§18.2). |

### 12.9 Example and live tests

- `examples/basic.py` (≤ 60 lines) shows: building endpoints; `list_models`; a streamed call that prints reasoning live, then the final text, usage and cost; and a three-message repair-style follow-up.
- `tests/test_live.py`, marked `@pytest.mark.live` and skipped unless its environment variables are set. Each live call is checked independently for valid behaviour; live results are never compared with each other.
  - `MODELSPELL_LIVE_LMSTUDIO_URL` and `MODELSPELL_LIVE_LMSTUDIO_MODEL`: one call each with `reasoning=None`, `"high"` and `"off"`, streamed and non-streamed. A module-scoped fixture discovers the matching `ModelInfo` and asserts it is present. The `"off"` calls and native V2 use that entry; `None` and `"high"` retain the string id. The `"off"` calls assert `Result.response_id is None`, which shows that `store: false` is honoured on the native route. Also `list_models`. Offline live-helper coverage checks ModelInfo object identity, id recording and reasoning-options metadata for supported and extra probes.
  - `OPENROUTER_API_KEY` and `MODELSPELL_LIVE_OPENROUTER_MODEL`: one streamed call with reasoning, asserting `cost is not None`; `list_models`; `list_providers`; and verification V1 (§17).
  - `MODELSPELL_LIVE_LLAMASERVER_URL` and `MODELSPELL_LIVE_LLAMASERVER_MODEL` (added in v6, used only by V2): llama-server as an `openai_compat` server.
  - Verification V2 (§17) for each backend whose variables are set.
  - (v7) No live test asserts anything about model output (M21-3).
- `tests/probe_lmstudio.py` (added in v7, M21-6): a diagnostic script, not a test and not run by pytest. It discovers once, passes the matching ModelInfo or falls back to the string id, and records `model_arg: "ModelInfo"` or `"id"` in P-CONFIG. Discovery errors are recorded without preventing requests. It sends a fixed catalogue of requests to LM Studio, one per generation setting, and prints each response with a plain summary for a person to read. Only `outcome == "ok"` answers enter distinct counts, equality or stop tails; each arm reports "k ok of n". If any arm has no ok records, the probe reports only "no usable answers: <first error, 120 chars>". Nothing depends on its output. `tests/test_probe_lmstudio.py` checks offline, with a stand-in for `complete()`, only what it sends and prints: request shapes on both LM Studio routes, order, output format, error and routing-regression handling, argument errors, discovery object identity and fallback, summaries excluding errors, and that its record helper never raises on a malformed record.
  - Print an explicit summary of skipped checks.

---

## 13. NEL migration (separate change, after v1 ships)

This is not part of the package work. It is recorded so the API is checked against its first consumer. It applies only to `workflows/proforma_v1/model_client.py`.

| NEL concept | Package equivalent |
|---|---|
| `Binding.base_url`, `api_key`, `api_key_env` | `Endpoint(kind, base_url, api_key, api_key_env)` |
| `_is_lmstudio(binding)` (localhost host, or pipeline name starting `lmstudio`) | NEL chooses `kind="lmstudio"` under the same rule; an OpenRouter host → `"openrouter"`; otherwise `"openai_compat"` |
| `reasoning`: `default` / `none` / `low` / `medium` / `high` | `None` / `"off"` / `"low"` / `"medium"` / `"high"` |
| `temperature`, `max_tokens` (always sent) | Pass them explicitly on every call |
| `provider_routing` | `provider=` |
| `timeout_s` | `timeout=` |
| `NEL_MODEL_STREAM` env switch | NEL passes callbacks when streaming is on |
| `_ActivityWriter.reasoning/output` | `on_reasoning` / `on_text` callbacks |
| Activity `finish` event (`finish_reason`, `streamed`, `transport`) | From `Result.finish_reason`, `Result.transport`, and whether callbacks were passed. On `lmstudio_native`, `finish_reason` is now `None` (NEL logged `"stop"`). |
| `Completion(content, usage, generation_id, reasoning, reasoning_details)` | `Result.text`, usage dict (below), `Result.response_id`, `Result.reasoning`, `Result.reasoning_details`. `response_id` is `None` on `lmstudio_native` (`store: false` by default); the ledger records no generation id for those calls. |
| `TruncatedCompletion` | NEL raises it when `Result.truncated` |
| "provider returned an empty completion" | NEL raises it when `not Result.text.strip()` |
| Streaming → non-streaming fallback, Responses → Chat fallback | Dropped. If NEL needs them, it implements them on top of the package. |
| Implicit redirect following | Dropped. A redirecting `base_url` fails with the §9.5 message; configure the final URL. |

The usage dict for NEL's ledger (`scripts/model_usage.record_call`) is rebuilt from the `Result`:

```python
{"prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens,
 "total_tokens": u.total_tokens, "reasoning_tokens": u.reasoning_tokens,
 "cached_tokens": u.cached_input_tokens, "cache_write_tokens": u.cache_write_tokens,
 "cost_usd": result.cost,
 "cost_details": {"upstream_inference_cost": raw_usage["cost_details"]["upstream_inference_cost"]}}
```

Omit keys whose value is `None`, as NEL's normaliser did. Include `cost_details` only when it is present in `raw_usage`.

NEL's `SECRETS` store, and its injection of keys into child processes, stay in `ui/server.py`. NEL constructs `Endpoint(api_key=SECRETS.get(env) or None, api_key_env=env)`.

---

## 14. Deviations from NEL (complete list)

| # | Deviation | Reason | Test |
|---|---|---|---|
| 1 | No exception on empty text or truncation | Policy belongs to the caller | P2 |
| 2 | No streaming → non-streaming fallback | NEL treated HTTP 400/422 as "streaming unsupported", which re-sends genuinely bad requests and hides their errors. Both target backends stream. | S8 |
| 3 | No Responses → Chat Completions fallback on LM Studio | It was a third calling method, needed only for LM Studio versions before 0.3.29. Raise with a version message instead. | E1 |
| 4 | `None` arguments are omitted from the body | Lets backend defaults apply; NEL parity is achieved by passing values explicitly | R2 |
| 5 | Reasoning tokens and cached tokens are read from Responses-style `*_tokens_details` | Bug fix: NEL missed reasoning tokens on the Responses route | P4 |
| 6 | Mid-stream error chunks and `error` events raise | NEL returned partial text as success | S6 |
| 7 | Streams ending without a completion marker raise | A dropped connection must not look like success | S7 |
| 8 | Native route: `truncated` inferred from output tokens against the effective cap (§6.4); `finish_reason` is `None` | That API reports no stop reason. NEL never detected truncation there and always reported `"stop"`, presenting an inference as a backend fact. | P7 |
| 9 | `reasoning` on `openai_compat` raises `UnsupportedSetting` | NEL silently ignored it, which can cost minutes per call on local models | R3 |
| 10 | `provider` on non-OpenRouter kinds raises | NEL sent it to any Chat Completions server | R5 |
| 11 | Discovery returns structured `ModelInfo`, uses LM Studio's native model list, and lists per-model OpenRouter providers | Needed to find models and per-model providers; NEL returned IDs only and the global provider catalogue | D1, D2 |
| 12 | Discovery failures raise | No UI "notes" in a library | D4 |
| 13 | Transport chosen by explicit `kind`, not by detecting a localhost host | NEL treats any localhost server (e.g. llama-server) as LM Studio | R1 (G7) |
| 14 | Native route sends `store: false` by default; `Result.response_id` is then `None` there | LM Studio stores native chats by default. A stateless library should not leave server-side history, and the route already flattens history, so stored state is never used. | R9, P9 |
| 15 | HTTP redirects are not followed | urllib's default handler forwards `Authorization` to the redirect target and turns POST into GET | E8 |
| 16 | Malformed SSE `data:` events raise `ResponseFormatError` | NEL skipped them, which can return success with text missing | S4 |
| 17 | `ProviderInfo.slug` comes only from `tag`; there is no name-derived fallback | A display name is not a routing identifier | D2 |
| 18 | Responses and native streams stop reading at their terminal event (`response.completed` / `response.incomplete` / `response.failed` with a dict `response`; `chat.end` with a dict `result`), not at the end of the HTTP body | NEL read until the server closed the body. A slow close by the server or a proxy could delay a finished generation or turn it into a timeout. | S13 |

Unchanged from NEL, on purpose:

- multi-turn flattening on the native route;
- the reasoning-alias selection rule;
- the Responses reasoning-delta matching rule;
- a single per-read timeout with a 900 s default;
- ModelSpell never adds `previous_response_id`, and sends no `store` field on the Responses route;
- the Chat Completions stream reads until `[DONE]` or the end of the body;
- cost taken from `usage.cost` with no request flag.

---

## 15. Later tool-calling support

v1 should avoid foreclosing tool calling in a later version. It does **not** promise that tool calling can be added without any API change. For example, assistant turns that carry only tool calls may need `Message.content` to accept `None`, and a later version would introduce that deliberately.

These v1 constraints keep that path open at little cost:

- Do not raise or drop data when `message.content` is `None` and `tool_calls` is present: return `text=""` and ignore the `tool_calls` field. Responses `function_call` items and native `tool_call` items are likewise ignored without error.
- Keep `Result` fields keyword-constructible, so new fields with defaults (for example `tool_calls=()`) can be appended.
- Keep body building and response parsing per route in one place each (§11 suggests the route modules).
- Keep `Message` rejecting unknown keys (§5), so tool fields such as `tool_calls` and `tool_call_id` are added deliberately.
- `reasoning_details` is already kept verbatim. v1 builds nothing on it (no replay, no reconstruction).

---

## 16. Implementation order

1. `errors.py`, `_types.py` with validation (including `field(hash=False)` fields), and the K1 and K6 tests. This fixes the public API first.
2. `_http.py`: the per-call no-redirect opener, request building, error mapping, and redact-then-truncate. Then the test-server fixture (including proxy-variable clearing and the hold-open mode) and tests E1–E9.
3. `_sse.py` (strict about malformed data), `_reasoning.py`, `_usage.py`, with unit tests (S4, P4, P5).
4. `_routes/chat.py`, with R1–R3 for chat, and the P and S tests for the chat fixtures.
5. `_routes/responses.py`, then `_routes/lmstudio_native.py` (`store: false` by default, effective-cap truncation, stopping at the terminal event), each with their R, P and S tests, including R9, P7, P9 and S13.
6. `_complete.py` (validation, dispatch, `extra` merge, effective cap, callbacks, cancellation): R4–R8, S1–S3, S11–S12, K4.
7. `_discovery.py`, with D1–D5.
8. Capture fixtures with the §12.2 procedure where servers are available, and write the fixture coverage table.
9. `examples/basic.py`, packaging, K2, K3, K5, README.
10. Run the full suite. Then run the live suite where servers are available, including V1. Report every skipped live check and verification explicitly.

**v6 (package 0.2.0):** route `FIELDS` and `RANGES`, then `build_body()` taking `settings` (G10–G12); the shared setting checks (R2, R4, R5, R10–R13, R15); `supported_settings()`, `check_settings()` and exports (R14, R16, K1); the developer UI (§18.3, U3, U6–U10, then the manual check list); README, DEVEL, NEWS, design history; V2 against every available backend, classified and applied; full suite and `build_release.py --smoke`.

**v7 (package 0.2.1):** `top_p` in the Responses `FIELDS` and the refusal messages (G13, R10, R14, R16); ModelInfo argument normalisation and conditional native reasoning (M21-7–M21-9, R17); live fixture, V2 metadata and probe discovery/usable-answer summaries with offline tests (§12.9); README, DEVEL, NEWS, design history; LM Studio live checks on models with and without reasoning configuration, including V2 Responses `top_p` through the named argument `accepted`; full suite and `build_release.py --smoke` at release.

---

## 17. Implementation-time verification

There are two verifications, V1 and V2. Both run in the live suite (§12.9). Record their outcomes and evidence in `tests/fixtures/README.md`. No verification decides any request default: every default in this spec is fixed.

For 0.2.1, `python -m pytest -m live -s -k lmstudio` MUST pass once on a model with reasoning
configuration and once on a model without it. These are request/response checks (HTTP status,
transport), not checks of model output (M21-3). Native V2 records carry `model_reasoning_options`:
the discovery tuple as a JSON list, or `null` when absent or empty. Their `model` is the id, while
the request receives the same ModelInfo object.

**V1 — OpenRouter discovery-to-routing round trip.**

1. Call `list_providers()` for `MODELSPELL_LIVE_OPENROUTER_MODEL`. Capture the response as a fixture, and record whether every entry has `tag` and `provider_name`.
2. Choose an entry with a non-`None` `slug`. If the model has a provider with two or more endpoints, prefer one of those.
3. Call `complete()` with `provider={"order": [slug], "allow_fallbacks": False}`. Assert that the call succeeds and that `Result.provider` equals that entry's `name`.
4. If a provider has two or more endpoints, record their tags. Distinct tags (for example `x` and `x/turbo`) indicate that `tag` is endpoint-level; identical tags indicate that it is provider-level. `Result.provider` reports only the provider name, so it cannot distinguish variants itself.

No code depends on the outcome. `slug` stays `tag`, passed verbatim, and `None` when `tag` is absent. If the round trip fails, report it; do not add a fallback.

**V2 — backend compatibility of the generation-settings tables (added in v6, before release).** The support and range tables (§6.7, §6.8.2) come from backend documentation checked on 27 October 2026; the llama-server facts come from a secondary source. V2 is a proportionate, opt-in compatibility check against real servers, run in the live suite, with its evidence recorded in `tests/fixtures/README.md`. No code path depends on it at runtime, and no runtime probing or capability cache is added to handle its outcomes.

*What it can establish.* A successful request shows that the backend **accepted** the request, not that a setting was applied. V2 does not try to prove that a sampler changes output. Each check records one outcome: `accepted` (the request succeeded), `refused` (the backend rejected the request with an error attributable to the setting) or, in the summary, `effect_unverified` for every accepted setting whose effect was not observed. Each record carries backend and server version where available, route, model, OpenRouter provider where relevant, the setting and value, the outcome, and the HTTP status or error class.

*Checks*, for each available backend (OpenRouter with a pinned provider, LM Studio native, LM Studio Responses, llama-server as `openai_compat`):

1. Each supported setting once, at a non-default valid value, streaming.
2. For each range in §6.8.2, the boundary value and a value just outside it, through `extra` (bypassing validation). An `accepted` out-of-range value does not by itself change the range.
3. Each unsupported setting on LM Studio Responses and on llama-server, through `extra`. An accepted field is `effect_unverified`, never "ignored" or "applied".
4. On llama-server, `repetition_penalty` and, separately, `repeat_penalty`, through `extra`.

*Classifying a refusal* before it changes anything: (1) **route or schema limitation** — documented, or the same refusal across models (and providers) naming the field; (2) **model or provider limitation** — the refusal names the model/provider or does not recur with another; (3) **unrelated failure** — authentication, availability, rate limits, network.

*What outcomes change:* a route limitation moves the setting to "—" for that route, with the evidence recorded; a model or provider limitation changes nothing in the table and is listed in the README as a known restriction; an unrelated failure is re-run or reported as not completed; ranges change only under §6.8.2's rule; if llama-server accepts `repetition_penalty`, adding it to the `openai_compat` column is the owner's decision, recorded in the design history.

*What V2 never does* (v7, M21-3): decide anything from model output. Sampled text is not reproducible, so a check built on it is a statistical test with a false-pass risk. Live checks record only whether a request was accepted or refused. For 0.2.1, V2's Responses record for `top_p` sent through the named argument must be `accepted` before release.

*Skipped backends* are reported as skipped, with the reason. Their rows ship as documented and are marked **unverified** in the README. A skipped check is never described as verification, and a check against one `openai_compat` server never as verifying others.

### Definition of done

- All tests in §12.3–§12.8 pass.
- A captured success-path fixture exists for each route and discovery URL where a server was available. Every synthetic fixture is labelled in the coverage table, with a reason for synthetic success-path fixtures.
- The wheel installs into a clean venv and imports with no other packages present.
- V1 has been run, or is reported as skipped with the reason.
- V2 has run for each available backend, or is reported as skipped with the reason; outcomes classified, applied and recorded; nothing skipped described as verified.
- The §18 release machinery and developer UI exist and their tests (K7, U1–U10) pass; the UI manual check list (§18.3) has been followed once.
- `README.md` documents:
  - the public API and the route table (§6.3);
  - the per-subtask configuration example (§6.6);
  - that §7 bodies are defaults and that `extra` overrides are raw, unvalidated and the caller's responsibility (§6.4);
  - native-route flattening and `store: false` by default, and what `reasoning="off"` changes (§7.3);
  - that Responses-route retention is backend-dependent and undocumented, and that `extra` can carry `store` but its effect on that route is unverified (§7.2);
  - the native truncation heuristic, its false positives and negatives, and `finish_reason=None` (§8.8);
  - the non-streaming timeout consequence (§9.4);
  - the redirect policy (§9.5);
  - the secrecy guarantees and limits (§4.3);
  - the narrow `openai_compat` claim, naming llama-server, vLLM and Ollama only as untested candidates (§1);
  - minimum LM Studio versions (0.3.29 for Responses; 0.4.0 for `reasoning="off"` and `list_models`);
  - a statement that ModelSpell is a controlled adaptation of NEL (§0), and the deviations list (§14);
  - a link to the fixture capture procedure (§12.2);
  - installation from the release wheel pinned by URL and sha256, the naming (`modelSpell` / `modelspell`) and the MIT license (§18);
  - (v6) the support table (§6.7) with rows V2 left unverified marked as such, and any model or provider restrictions V2 found; `supported_settings()` and `check_settings()` with an example of checking settings before starting a streamed response; "sent is not applied" (§6.7.3) including `require_parameters` and what it does and does not establish; that `top_k` and `min_p` on `openai_compat` are extensions and `repetition_penalty` is not sent there; that LM Studio's Responses route takes `temperature`, `max_tokens` and `top_p` (v7), on OpenAI's Responses reference which LM Studio's page refers to, and that the other seven are not sent and their effect through `extra` is unverified; the two behaviour changes for 0.1.0 callers (non-finite numbers, `temperature` route ranges); the per-subtask example with one advanced setting.

---

## 18. Version, release and developer tooling (added in spec v5)

This section governs repository tooling around the package. None of it changes library behaviour, and none of the tooling ships in the wheel.

### 18.1 Version

- The version is single-sourced in `modelspell/_version.py`, which contains exactly one line: `__version__ = "X.Y.Z"`.
- `pyproject.toml` declares `dynamic = ["version"]` and reads it with `[tool.hatch.version] path = "modelspell/_version.py"`.
- `modelspell/__init__.py` imports `__version__` from `_version.py`. It MUST NOT read a text file or package metadata at import, which would break §2.

### 18.2 Release artifact and workflow

- **The only release artifact is the wheel** `modelspell-X.Y.Z-py3-none-any.whl`, attached to GitHub release `vX.Y.Z`. No curated source zip is produced. GitHub's automatic source archives exist but are not a supported install channel.
- **Wheel contents** are fixed by `packages = ["modelspell"]`: exactly `modelspell/**`, `METADATA`, `WHEEL`, `RECORD` and `licenses/LICENSE` (K7). The sdist target is restricted to `modelspell/`, `pyproject.toml`, `README.md` and `LICENSE`, so tests, the UI, scripts and docs never ship in either form.
- **`scripts/build_release.py`** builds from `git archive HEAD` (uncommitted edits are excluded and reported), requires a `## X.Y.Z` heading in `NEWS.md`, verifies the wheel's contents and metadata exactly, and with `--smoke` installs the wheel into a fresh venv and runs `scripts/smoke_installed.py`. It prints the wheel path and sha256. `--changed-since TAG` exits 1 when any shipped file (`modelspell/`, `pyproject.toml`, `README.md`, `LICENSE`) changed since `TAG`.
- **Branches:** development on `devel`; `main` receives merges from `devel`.
- **`.github/workflows/tests.yml`** runs the offline suite on Python 3.10 and 3.13 on pushes to `devel` and `main`, on pull requests, and when called by the release workflow.
- **`.github/workflows/release.yml`** runs on pushes to `main` and by manual dispatch. It runs `tests.yml` first. If release `vX.Y.Z` does not exist, it runs `build_release.py --smoke` and creates the release with the wheel attached; the notes are the `NEWS.md` section plus a pinned install line with the sha256. If the release exists, it fails when `build_release.py --changed-since vX.Y.Z` reports shipped-file changes (a version bump is required), and otherwise does nothing.

### 18.3 Developer test UI

The UI exists to exercise the library by hand. It lives in top-level `ui/`, runs from a checkout with `python -m ui`, and uses only the public API (`complete`, `list_models`, `list_providers`, `supported_settings`, `Endpoint`, the errors). It is exempt from §2's library rules (it prints its address and reads its HTML file), because it is not part of the package.

- **Server:** stdlib `ThreadingHTTPServer` bound to `127.0.0.1` only. A per-launch random token is required on every request: as the `t` query parameter for the page, as the `X-Session-Token` header for API calls. Requests without it get 403.
- **Keys:** write-only, held in the server process's memory, keyed by `(kind, base_url)` after `Endpoint` normalisation, so a key set for one endpoint is never sent to another. Responses report only `key_set`. The `api_key_env` fallback still applies.
- **Page:** input box, optional system prompt, a **request panel** of generation settings, reasoning stream box, output box, Send and Stop, a status line (time, transport, finish reason, truncation, usage, cost, provider) and, in the Result panel, the settings sent followed by the full `Result` as JSON.
- **Settings dialog** (connection only): model source (`kind`), base URL, API key, model ID (with `list_models`), and provider for OpenRouter (with `list_providers`; "Any" sends no `provider`, a chosen endpoint sends `{"order": [slug], "allow_fallbacks": false}`, entries with `slug is None` are disabled). Held once per process, shared by every tab. The header summary shows model source, base URL, model, provider (OpenRouter) and key status.
- **Request panel** (v6): between the input box and Send/Stop. A basic row always visible — reasoning (Backend default, off, low, medium, high; hidden for `openai_compat`), temperature, max tokens — and an *Advanced* `<details>`, collapsed by default, with top-p, top-k, min-p, repetition penalty, presence penalty, frequency penalty, seed and stop sequences (a textarea, one per line). A Reset button clears every field. A one-line note says settings are sent as shown and whether the model applies them is up to the backend.
  - **Blank means not sent; zero is a value.**
  - **What has been set is never hidden:** while collapsed, the summary reads "Advanced (2 set)" or "Advanced (3 set, 1 not sent)".
  - **Which fields are sent:** the page enables only fields in `supported[level]` from `/api/state`, for the saved model source and the reasoning level selected in the panel. Other fields stay visible, disabled and keep their value, labelled "not sent at this reasoning level" (sent at another level of the same source) or "not sent with this model source". Fields re-evaluate when reasoning changes and after the dialog is saved.
  - **Parsing:** blank or whitespace is not sent; number fields use `Number()`, and the page refuses (status-line message, nothing sent) only non-numeric text, non-finite values (e.g. `1e400`, which `JSON.stringify` would turn into `null`), and, for max tokens, top-k and seed, values that are not safe integers. Ranges and integer-versus-decimal for other fields are left to modelSpell. Stop sequences are split on newlines, a trailing `\r` removed, empty lines dropped; none left means not sent. A stop sequence containing a newline cannot be entered.
  - **Per tab, not saved:** generation settings live in the page only; nothing is written to the server or browser storage.
- **Routes:**
  - `GET /api/state` returns the connection settings, `key_set`, `version` and `supported`: for each reasoning level the saved `kind` allows, the sorted names from `supported_settings(kind, level)`; the key `""` stands for `None`. For `openai_compat` only `""` is present. It no longer contains `reasoning` or `max_tokens`.
  - `POST /api/config` takes the connection settings and the write-only key as before; a body containing `reasoning` or `max_tokens` is refused with HTTP 400 ("moved to the request panel; send it with /api/complete") and changes nothing.
  - `POST /api/complete` takes `prompt`, `system` and optional `settings`, an object whose keys are a subset of the eleven page-set generation arguments (`provider` comes from the dialog). A `settings` value that is not an object, or has a key outside the eleven, gets HTTP 400 before streaming and nothing is sent upstream. `null` values and absent keys are not passed, and `reasoning: ""` is treated as `null`; every other value passes to `complete()` unchanged (never converted with `value or None`, which would drop zeros). The server does no value checks; modelSpell's refusals arrive as one `error` event, and nothing is sent upstream. The UI deliberately does not call `check_settings()` first: it exists to exercise `complete()` itself.
- **Calls:** single-turn, always streamed. Events are newline-delimited JSON: `reasoning` and `text` fragments, then one `result` or one `error`. The `result` event carries `settings_sent`: the generation arguments passed to `complete()` (including the provider pin), non-`null` only. They are what was sent, not a statement of what the model applied.
- **Cross-tab limits.** If another tab changes the model source, a page's `supported` lists are stale until it reloads; its next request may carry a setting the new route refuses, which arrives as an `error` event. No revision checks are made.
- **Stop:** the browser aborts its request; the server's next write fails and the callback raises `Cancelled`, which closes the upstream request (§8.10). Stop cannot take effect before the first token, because v1 cancellation exists only inside callbacks. A cancellation mechanism usable during prefill is a candidate for a later version. The Ctrl/Cmd+Enter handler can still start a second request while one is active.

| ID | Test (`tests/test_ui.py`) |
|---|---|
| U1 | Missing or wrong token → 403 for the page and every API route; the correct token serves the page. |
| U2 | A stored key is reported only as `key_set`, never returned; it is not sent after switching to another endpoint, is sent again after switching back, and `api_key: ""` clears it. |
| U3 | `/api/complete` events concatenate to `Result.text` and `Result.reasoning`; the request carries the system message and the provider pin from the dialog settings, and reasoning and max tokens from the request's `settings`. |
| U4 | A backend error becomes one `error` event; an invalid configuration → 400. |
| U5 | Closing the browser connection mid-stream cancels the call and closes the upstream connection. |
| U6 | Settings reach the backend under the route's names: OpenRouter with all eleven page-set arguments, each under its wire name; LM Studio with reasoning `off` goes to `/api/v1/chat` with `top_k` and `repeat_penalty`; `null` and absent keys never appear; `settings_sent` equals exactly the non-`null` arguments passed. |
| U7 | `/api/state`'s `supported` equals `supported_settings()` for every allowed level of the saved `kind`; after switching to `openai_compat`, only `""` remains, without `reasoning`. |
| U8 | `settings` not an object or with an unknown key → 400; `top_k` on LM Studio with reasoning `low` → one `error` event naming `UnsupportedSetting`; `temperature: 3` on OpenRouter → one `error` event naming `ConfigError`. The fake backend receives zero requests in each case. |
| U9 | `/api/config` with `reasoning` or `max_tokens` → 400, and `/api/state` is unchanged afterwards. |
| U10 | Zero values survive: `temperature: 0`, `top_k: 0`, `seed: 0` on OpenRouter arrive in the body as `0` and appear in `settings_sent`. |

The HTML and JavaScript are not tested automatically, and no browser-automation dependency is added. The manual check list in `DEVEL.md` ("Developer test UI") is followed once after page changes.

---

## 19. Decisions of package 0.2.1 (added in v7)

From the *modelSpell 0.2.1 technical specification*, version 6. IDs are prefixed `M21-`. The specification's versions 1 to 5 used M21-2, M21-3 and M21-6 for an approach based on effect checks of model output. Version 6 withdrew that approach, and the design history records why.

| ID | Decision | Sections |
|---|---|---|
| M21-1 | `top_p` is sent on the LM Studio Responses route as `top_p`, with the existing value check only. The other seven sampling arguments stay refused there. | §6.7.1, §6.9, §7.2 |
| M21-2 | A route's documentation includes the reference it points to for fields it does not list. LM Studio's Responses page refers to OpenAI's Responses reference, so the generation settings that reference defines (`temperature`, `max_output_tokens`, `top_p`) count as documented. This covers only the named generation settings, not `store`. It replaces the 0.2.1 specification's earlier wording, which excluded references. | §1, §6.7.1, §7.2 |
| M21-3 | No release or support decision depends on model output. Live checks record acceptance and refusal only. Whether a server applies a setting stays unverified and documented as such. | §6.7.3, §12.9, §17 |
| M21-4 | The README says the seven unsent Responses fields are not sent, that LM Studio accepts them (and a made-up field) through `extra`, so acceptance shows nothing, and that OpenAI's reference does not define them. It does not say they are ignored. | README |
| M21-5 | Responses refusal messages suggest `reasoning="off"` only for settings the native route sends, and mark `extra` as effect-unverified. | §6.7.2 |
| M21-6 | `tests/probe_lmstudio.py` is a committed diagnostic with no verdicts. Its offline tests cover only what it sends and prints. | §12.9 |
| M21-7 | `complete()` accepts a `ModelInfo` as `model` and sends its `id`. An additive API change, so 0.2.1 is not API-identical to 0.2.0. | §3, §6.1, §10.1, R17 |
| M21-8 | On the LM Studio native route, a `ModelInfo` with no reasoning options means the body omits `reasoning`. A string model behaves as before. | §7.3, R17 |
| M21-9 | modelSpell still never discovers or caches capabilities itself (M2-16 unchanged). It uses only what the caller passes. `check_settings()` and `supported_settings()` are unchanged, and there is no client-side refusal for options lacking `"off"`. | §6.7, §6.9, §7.3 |
