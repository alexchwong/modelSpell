# NEWS
## 0.4.0
- New `complete_with_tools()`: offer functions, receive complete calls, return results under the original call IDs.
- New `ToolDefinition`, `ToolCall`, `ToolTurn` and `ToolOutput`; `ToolTurn.replay` shows exactly what continuation sends.
- Calls are returned only from a cleanly finished turn; truncated or unfinished turns have none and cannot be replayed.
- New `check_tool_request()` checks route, settings, tools, provider pin and retry limits without a network call.
- OpenRouter tool requests need a single-provider pin with fallbacks off; `require_parameters` is sent as true.
- LM Studio tool turns use `/v1/responses` with `store: false`; `reasoning="off"` is refused for them.
- `complete()` and `complete_with_tools()` accept `rate_limit_retries`, `max_retry_wait` and `on_retry` to retry HTTP 429.
- Retries are off by default; each attempt resends identical bytes with the full timeout; Stop interrupts the wait.
- New `RetryEvent(attempt, next_attempt, delay, status)`, passed to `on_retry` before each wait.
- `complete()` refuses `ToolTurn` and `ToolOutput` entries with a `ConfigError` naming `complete_with_tools()`.
## 0.3.0
- `complete()` accepts `cancel=CancelHandle()`; `cancel()` from any thread ends the call at any stage, including before the first token.
- `Cancelled` gains `sent`: `False` means no part of the request was written before cancellation took effect.
- Cancellation refuses new streamed fragments; an admitted callback may still run; a `cancel()` after the terminal event keeps the `Result`.

## 0.2.1
- `complete()` accepts a `ModelInfo` from `list_models()`; then `reasoning="off"` works on LM Studio models without reasoning configuration.
- `top_p` is now sent on LM Studio's Responses route (reasoning default, low, medium, high), as OpenAI's Responses reference defines.
- Refusals on that route suggest `reasoning="off"` only where it helps, and mark `extra` as effect-unverified.

## 0.2.0
- `complete()` adds `top_p`, `top_k`, `min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty`, `seed` and `stop`.
- Each setting is sent under the route's field name; LM Studio native sends `repeat_penalty`.
- Settings a route does not support raise `UnsupportedSetting` before sending; nothing is dropped silently.
- New `supported_settings(kind, reasoning)` lists the generation arguments modelSpell sends on a route.
- New `check_settings(kind, ...)` runs `complete()`'s setting checks without a key or network call.
- Setting values are checked before sending, including documented per-route ranges for `temperature`.
- Infinite or NaN numeric settings now raise `ConfigError` instead of sending invalid JSON.
- `temperature` above 1 on LM Studio's native route, or above 2 on OpenRouter, now raises.

## 0.1.0
- First release: one `complete()` call for LM Studio, OpenRouter and OpenAI-compatible servers.
- Returns answer, reasoning, normalised token usage and OpenRouter cost in one `Result`.
- Optional streaming through `on_text` / `on_reasoning` callbacks, with cancellation by raising `Cancelled`.
- LM Studio `reasoning="off"` uses the native chat API, flattens history and sends `store: false`.
- Model discovery on all servers; per-model provider discovery on OpenRouter for provider pinning.
- API keys stay in memory, are redacted from every package error, and never follow redirects.
- No runtime dependencies; Python 3.10 or later.
- Released as a wheel on GitHub releases; pin it by URL and sha256.
