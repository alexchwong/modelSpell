# NEWS
## 0.1.0
- First release: one `complete()` call for LM Studio, OpenRouter and OpenAI-compatible servers.
- Returns answer, reasoning, normalised token usage and OpenRouter cost in one `Result`.
- Optional streaming through `on_text` / `on_reasoning` callbacks, with cancellation by raising `Cancelled`.
- LM Studio `reasoning="off"` uses the native chat API, flattens history and sends `store: false`.
- Model discovery on all servers; per-model provider discovery on OpenRouter for provider pinning.
- API keys stay in memory, are redacted from every package error, and never follow redirects.
- No runtime dependencies; Python 3.10 or later.
- Released as a wheel on GitHub releases; pin it by URL and sha256.
