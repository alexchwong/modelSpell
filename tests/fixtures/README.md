# Test fixtures

Two classes (spec §12.2): **captured** (raw bodies from a real server) and **synthetic** (hand-written, `synthetic` in the file name). Both are permanent.

## Status at implementation (20 Oct 2026)

No backend was reachable from the implementation environment (no LM Studio instance, no `OPENROUTER_API_KEY`, and openrouter.ai blocked by the sandbox network allowlist), so **every fixture below is synthetic**, including the success paths. Shapes follow the backends' documented formats; the native-route fixtures are seeded from NEL's `workflows/proforma_v1/tests/test_lmstudio_reasoning_none.py` (commit `7075a47`) and adapted to `store: false` (no `response_id`). Replacing success-path fixtures with captures is optional, not a defect (§12.2).

The S2 pairs (`<stem>.json` + `<stem>.sse`) were generated from one canonical response each, so both encode the same text, reasoning, usage and finish state.

## Capture procedure

Streaming (drop `-N` and write `<case>.json` for non-streaming; for LM Studio use the local URL and omit the auth header):

```sh
curl -sS -N https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" -H "Content-Type: application/json" \
  -d @request.json > tests/fixtures/chat/<case>.sse
grep -cF "$OPENROUTER_API_KEY" tests/fixtures/chat/<case>.sse   # must print 0
```

Check every captured file for the key before committing it. Add a row below with backend, version and date.

## Coverage

| File | Class | Case | Why synthetic |
|---|---|---|---|
| chat/or_text_synthetic.{json,sse} | synthetic | OpenRouter text; S2 pair; `:` comment; final usage chunk (S5) | no OpenRouter key |
| chat/or_reasoning_synthetic.{json,sse} | synthetic | text + `reasoning_details` (text + encrypted) mirrored in `reasoning`; S2 pair | no OpenRouter key |
| chat/or_reasoning_field_synthetic.sse | synthetic | text + `reasoning` field only | no OpenRouter key |
| chat/or_length_usage_synthetic.json | synthetic | `finish_reason=length`; cost, `cost_details`, cached + cache-write + reasoning tokens | no OpenRouter key |
| chat/or_midstream_error_synthetic.sse | synthetic | mid-stream error chunk | not producible on demand |
| chat/or_error_only_synthetic.sse | synthetic | error-only stream | not producible on demand |
| chat/or_malformed_synthetic.sse | synthetic | malformed `data:` event | not producible on demand |
| chat/or_no_completion_synthetic.sse | synthetic | disconnect before `finish_reason`/`[DONE]` | not producible on demand |
| chat/compat_text_synthetic.{json,sse} | synthetic | openai_compat text, no usage; S2 pair | no openai_compat server |
| chat/compat_reasoning_content_synthetic.json | synthetic | `reasoning_content`; `usage.cost` ignored | no openai_compat server |
| responses/resp_text_synthetic.{json,sse} | synthetic | text; S2 pair | no LM Studio |
| responses/resp_reasoning_synthetic.{json,sse} | synthetic | reasoning deltas; `output_tokens_details.reasoning_tokens`; S2 pair | no LM Studio |
| responses/resp_incomplete_synthetic.json | synthetic | `incomplete` / `max_output_tokens` | no LM Studio |
| responses/resp_failed_synthetic.{json,sse} | synthetic | `failed` | not producible on demand |
| responses/resp_final_only_synthetic.sse | synthetic | completion event only, no deltas (S9) | not producible on demand |
| responses/resp_no_completion_synthetic.sse | synthetic | disconnect before `response.completed` | not producible on demand |
| native/native_text_synthetic.{json,sse} | synthetic | text, no `response_id`; progress events interleaved; S2 pair | no LM Studio |
| native/native_reasoning_synthetic.{json,sse} | synthetic | text + reasoning; S2 pair | no LM Studio |
| native/native_error_synthetic.sse | synthetic | `error` event followed by `chat.end` | not producible on demand |
| native/native_no_stats_synthetic.json | synthetic | no `stats` | not producible on demand |
| native/native_cap_synthetic.{json,sse} | synthetic | output exactly at the cap (1500); S2 pair | not producible on demand |
| native/native_no_end_synthetic.sse | synthetic | disconnect before `chat.end` | not producible on demand |
| discovery/openrouter_models_synthetic.json | synthetic | `/models` (trimmed; duplicate id; entry without id) | openrouter.ai unreachable from the build environment |
| discovery/openrouter_endpoints_synthetic.json | synthetic | `/endpoints`: `tag` with `/`, mixed-case `tag`, no `tag`, no name | no OpenRouter key |
| discovery/lmstudio_models_synthetic.json | synthetic | loaded LLM, unloaded LLM, embedding | no LM Studio |
| discovery/compat_models_synthetic.json | synthetic | generic `/models` (`id`/`slug`/string rows, duplicate) | no openai_compat server |

## Verification V1 (§17)

**Not run** — no `OPENROUTER_API_KEY` available at implementation time. Run `pytest -m live -s tests/test_live.py::test_live_v1_discovery_to_routing_round_trip` and record the printed entries, chosen slug, and outcome here.
