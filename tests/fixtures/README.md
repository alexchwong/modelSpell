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

## Verification V2 (spec v7 §17)

V2 records whether a backend **accepts** each setting, never whether it applies it (spec v7 §17, M21-3).

Run with `python -m pytest -m live -s -k v2` (variables in `DEVEL.md`). Paste each printed `V2 {...}` line, then classify every `refused` record as a route limitation, a model or provider limitation, or an unrelated failure, and record what (if anything) changed.

| Backend | Version | Route | Model / provider | Status |
|---|---|---|---|---|
| OpenRouter | n/a (hosted) | chat_completions | qwen/qwen3-coder-next, pinned `parasail/bf16` (first listed), no fallbacks, no `require_parameters` | **run 27 Oct 2026** |
| LM Studio | 0.4.21 (Build 2) | lmstudio_native | qwen3.8-27b | **run 28 Oct 2026** |
| LM Studio | 0.4.21 (Build 2) | responses | qwen3.8-27b | **run 28 Oct 2026** |
| LM Studio | *to record* | responses (0.2.1 working copy) | *to record* | **pending**: required before releasing 0.2.1 |
| llama-server (`openai_compat`) | b9910-f5525f7e7 | chat_completions | qwen3-coder-next | **run 28 Oct 2026** |

OpenRouter, 27 Oct 2026: all 10 supported settings `accepted` (served by Parasail), effect unverified.
Range probes through `extra`: boundary values accepted (temperature 2, repetition_penalty 2,
presence_penalty and frequency_penalty -2 and 2); values just outside refused with HTTP 400 by
OpenRouter itself (`provider_name: null`), naming the parameter and limit. Classification: expected
refusals of out-of-range values, not route limitations. No change to the support or range tables.
An earlier run the same day (qwen/qwen3.8-27b, `darkbloom/fp4`) was unrelated throughout: HTTP 404,
every provider removed by the account's zero-data-retention guardrails. It verifies nothing.

LM Studio native (reasoning "off"), 28 Oct 2026: all 6 supported settings `accepted`, effect
unverified. Range probe through `extra`: temperature 1 accepted; 1.01 refused with HTTP 400 by
LM Studio's request validation (`param: temperature`, `code: too_big`). Classification: expected
refusal of an out-of-range value. No table change.

LM Studio Responses (reasoning "low"), 28 Oct 2026: `max_tokens` and `temperature` `accepted`.
The 8 settings modelSpell does not send on this route were all `accepted` when forced through
`extra`; whether LM Studio applies or silently drops them is not established. No table change
(spec v6 §6.8.2: acceptance alone is not evidence). Package 0.2.1 adds `top_p` to this route on
documentation (spec v7 §7.2, M21-2), not on this acceptance; see the 0.2.1 run below.

llama-server, 28 Oct 2026: all 9 supported settings `accepted`, effect unverified. Probes through
`extra`: `repetition_penalty` and `repeat_penalty` both `accepted`. Source check at f5525f7
(tools/server/server-schema.cpp): every name modelSpell sends is a schema field (`max_tokens` is an
alias of `n_predict`); `repeat_penalty` is a field with no aliases; fields outside the schema are
ignored without error. So `repetition_penalty` is accepted and silently ignored. Classification: no
route limitation; M2-3's exclusion of `repetition_penalty` on `openai_compat` confirmed. No table
change. This verifies llama-server only, not other `openai_compat` servers.

### 0.2.1: `top_p` on LM Studio Responses (pending)

Before releasing 0.2.1, run `python -m pytest -m live -s -k v2_lmstudio_responses` from the 0.2.1
working copy and paste the `V2` lines here with the date and LM Studio version. The release needs
the record `setting: "top_p", via: "argument"` to be `accepted`. An `unrelated` record means re-run.
A 4xx naming `top_p` is classified under spec v7 §17 before anything else is decided.

### History

**Attempted, not verified (27 Oct 2026).** An OpenRouter run with `qwen/qwen3.8-27b` pinned to
`darkbloom/fp4` returned HTTP 404 for all 22 requests. Account guardrails (zero data retention)
removed every provider. All records are `unrelated`, and the run verifies nothing. OpenRouter
therefore stays **not verified**. The skipped rows below retain the original implementation
environment's status; this unsuccessful attempt does not replace them with verification.

**Not run** (27 Oct 2026, package 0.2.0) — no LM Studio instance, no `OPENROUTER_API_KEY` and no llama-server were available in the implementation environment. Every backend is therefore **skipped**, every row of the support and range tables (spec v6 §6.7, §6.8.2) ships as documented, and the README marks them unverified. Nothing here may be described as verification until the checks below are run.

## Probe observations (informal)

Observations of model output are information only. Model output is not reproducible, so nothing in
the support tables, a release or the README is decided from them (spec v7 §17, M21-3).

**28 Oct 2026**, uncommitted script, LM Studio Responses route, reasoning "low", `qwen3.8-27b`,
non-streaming, token cap 1500. The LM Studio version, engine, quantisation and presets were not
recorded. Fields under test went through `extra`.

- A made-up field (`zz_not_a_field`) was accepted.
- `top_p=0.01`: three identical answers; the baseline gave three different answers.
- `top_k=1`, `min_p=0.99` and `seed=42`: three different answers each.
- `stop=["17"]`: counting continued to 30.
- The penalty probes had a blank baseline at temperature 0, so nothing could be compared.

Full answers are in Appendix A of the *modelSpell 0.2.1 technical specification* (v6). Later output
of `tests/probe_lmstudio.py` may be kept under `tests/fixtures/evidence/` with the date, LM Studio
version, engine and model; it is informal and nothing cites it.
