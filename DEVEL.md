# Developer guide

This file is for repository maintenance. Library usage is documented in `README.md`; the
behavioural contract is `docs/modelspell_v1_spec_v9.md`; agent operating rules are in
`.clinerules`. Nothing in this guide, `.clinerules`, `tests/`, `ui/`, `scripts/`, `examples/`
or `docs/` ships in the release wheel.

## Quick start

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

python -m pytest                 # offline suite (live checks skip without credentials)
python -m ui                     # developer test UI in your browser
python scripts/build_release.py --smoke   # build, verify and smoke-test the release wheel
```

## Contents

- [Repository layout](#repository-layout)
- [Library invariants](#library-invariants)
- [Tests](#tests)
- [Owner checks](#owner-checks)
- [Developer test UI](#developer-test-ui)
- [Changing behaviour](#changing-behaviour)
- [Branches and releases](#branches-and-releases)
- [NEWS entries](#news-entries)

## Repository layout

| Path | Ships in wheel | Purpose |
|---|---|---|
| `modelspell/` | yes | The library. Import name `modelspell`; distribution name `modelSpell`. |
| `modelspell/_version.py` | yes | Single source of the version (`__version__ = "X.Y.Z"`). |
| `pyproject.toml`, `README.md`, `LICENSE` | metadata | Packaging, wheel description, MIT license. |
| `tests/` | no | Offline suite, fixtures (including the test TLS certificate in `tests/fixtures/tls/`), golden request bodies, opt-in live suite, and the LM Studio probe diagnostic (`tests/probe_lmstudio.py`). |
| `ui/` | no | Developer test UI (`python -m ui`) and the owner-check battery (`ui/owner_checks.py`). |
| `scripts/build_release.py` | no | Builds and verifies the release wheel. |
| `scripts/smoke_installed.py` | no | Smoke test run against an installed wheel (K3, `--smoke`). |
| `examples/basic.py` | no | Usage example against a local LM Studio. |
| `docs/` | no | Spec v9 (authoritative), specs v6–v8 (superseded, kept as history) and design history (informative). |
| `.clinerules` | no | Agent operating rules; process detail lives in this guide. |
| `.gitattributes` | no | Line-ending policy (`* text=auto eol=lf`). |
| `.github/workflows/` | no | `tests.yml` (push, PR) and `release.yml` (push to `main`). |

The wheel's contents are fixed by `[tool.hatch.build.targets.wheel] packages = ["modelspell"]`.
The sdist target is restricted to the same files, so a stray `python -m build` cannot ship tests
or the UI. `build_release.py` verifies the wheel contains exactly `modelspell/**`, metadata and
`LICENSE`.

## Library invariants

Changes under `modelspell/` must keep these; tests enforce most of them.

- **Standard library only.** No runtime dependencies (K5).
- **No side effects.** No printing, logging, file I/O, network at import, or module-level mutable
  state (§2, K2). This is why the version lives in a `.py` file rather than a text file read at
  import, and why the UI lives outside the package.
- **One HTTP request per `complete()` or `complete_with_tools()` call by default.** The only
  retry is the opt-in HTTP 429 loop in `_complete._post` (spec v9 §6.11): it retries only an
  error carrying `_http.py`'s private transport-429 marker, resends the bytes encoded once, and
  waits through `_Watch.pause`. No route or streaming fallback, no redirect following; requests
  go through the per-call `_NoRedirectHandler` opener, never `urlopen` (§6.3, §9.5).
- **Tool turns are sealed.** Only the route parsers, through `ToolTurn._make`, create a
  `ToolTurn`; `tool_calls` derives from the frozen `replay`; tool bodies are encoded by
  `_json.dumps` (exact ints and `Decimal`), ordinary bodies by `json.dumps` exactly as in 0.3.0
  (the goldens). Do not add a public constructor or a pickling path (spec v9 §21.2).
- **Every completion request goes through the watched opener**, whose connections register and
  initiate each connection attempt atomically (spec v8 §9.6); discovery uses the plain one. A
  `CancelHandle` never holds its lock while caller code runs.
- **Every exception the package raises is built through `Call.error()` in `_http.py`**, which
  redacts the key before truncating. Low-level failures are raised `from None` (§9.3, E5).
- **The public API is exactly `modelspell.__all__`** (K1). Field changes to public dataclasses
  update the spec and the K1 snapshot together.

### Overloaded `provider` terminology

`provider` has different meanings depending on context:

- In `complete(provider=...)` and `check_settings(provider=...)`, it is an OpenRouter routing
  mapping, such as `{"order": ["deepinfra/turbo"], "allow_fallbacks": False}`.
- `Result.provider` is the provider string reported by the response, not a routing mapping or
  necessarily the endpoint slug requested by the caller.
- `ProviderInfo.name` is a display name; `ProviderInfo.slug` is the endpoint tag used for routing.
  Do not derive a slug from the display name or assume they are interchangeable.
- The UI connection setting `provider` stores a selected endpoint slug and is converted into a
  routing mapping before calling `complete()`.
- In V2 evidence, `provider` records the requested endpoint pin, while `served_by` records
  `Result.provider` from the response.

Keep evidence metadata separate from completion arguments in helper interfaces. Flattening both
into one keyword namespace can collide on `provider`; a metadata parameter named `provider` can
also consume the routing argument instead of forwarding it. Prefer explicit metadata mappings
and internal names such as `provider_slug` for endpoint identifiers. Preserve the public API and
wire-field names; their context and types distinguish their meanings.

## Tests

```bash
python -m pytest                        # everything offline, incl. UI server and wheel install (K3)
python -m pytest tests/test_stream.py   # one area
python -m pytest -m live -s             # live checks; see below
```

Test IDs (R1, P7, S13, …) match the spec's §12 tables; keep them in test names or comments when
adding cases. Tool calling is T1–T11 (`tests/test_tools.py`, spec v9 §12.11) and HTTP 429
recovery L1–L13 (`tests/test_retry.py`, §12.12); both use the fake server's `raw` request bytes,
and retry tests observe backoff by patching `_Watch.pause`, never by sleeping. Cancellation is C1–C9 (`tests/test_cancel.py`, spec v8 §12.10), usage while
streaming S14, the owner-check battery O1–O7 (`tests/test_owner_checks.py`) and the UI's Stop and
owner-check routes U11–U16.

**Cancellation tests** order stages with hooks, never sleeps: events the fake sets when it reaches
a stage (`server.reached`, `server.hold_event(i)`), and events set by patched standard-library
functions (`socket.getaddrinfo`, `socket.socket.connect_ex`, `http.client.HTTPConnection.send`).
The `held_connect` fixture skips (never passes) on a platform that cannot hold a TCP connect.
HTTPS cases use the self-signed certificate in `tests/fixtures/tls/`, trusted through
`SSL_CERT_FILE`. It is test-only; regenerate it in that folder with

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout localhost.key -out localhost.crt -days 36500 \
  -subj /CN=localhost -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

**Live checks** (`tests/test_live.py`) run only when their variables are set, and each call is
checked on its own:

| Backend | Variables |
|---|---|
| LM Studio | `MODELSPELL_LIVE_LMSTUDIO_URL` (e.g. `http://localhost:1234/v1`), `MODELSPELL_LIVE_LMSTUDIO_MODEL` |
| OpenRouter | `OPENROUTER_API_KEY`, `MODELSPELL_LIVE_OPENROUTER_MODEL` (`author/slug`); optional `MODELSPELL_LIVE_OPENROUTER_PROVIDER` (provider slug from `list_providers`; default: first listed provider with a slug) |
| llama-server (V2 and owner checks, as `openai_compat`) | `MODELSPELL_LIVE_LLAMASERVER_URL` (including `/v1`), `MODELSPELL_LIVE_LLAMASERVER_MODEL` |
| Owner checks | `MODELSPELL_LIVE_PRESET` (`Quick`, `Cancellation`, `Usage`, `Full`; default `Full`) |

The LM Studio model may have reasoning configuration or none: native tests pass its `ModelInfo`
from a module-scoped discovery fixture; Responses tests retain the string id. Native V2 records carry
`model_reasoning_options`. When a change touches LM Studio's native request, run
`python -m pytest -m live -s -k "test_live_lmstudio and off"` once with a model that has reasoning
configuration and once with one that has none (two short requests each), and record both in
`tests/fixtures/README.md`. These checks concern HTTP acceptance and transport, not model output.
The probe is a diagnostic and is never part of a release check.
The probe also discovers once, passes the matching entry (or falls back to the id), and reports
only usable answers in its summaries, including each arm's ok count.

**Tool round trips (VT, spec v9 §17)** use the same variables:
`python -m pytest -m live -s -k vt`. Each turn prints a `VT {...}` line; paste them into
`tests/fixtures/README.md` under "Tool round trips". A model that answers without calling skips
(nothing verified). On OpenRouter set `MODELSPELL_LIVE_OPENROUTER_PROVIDER` to a provider that
supports tools; a reported provider that differs from the pin fails. Malformed arguments and
unknown names cannot be injected live (replay accepts only parser-built turns); they are covered
offline by T6 and T7. `tests/test_live_helpers.py` runs the VT harness against the fake.

Run verification V1 with
`python -m pytest -m live -s tests/test_live.py::test_live_v1_discovery_to_routing_round_trip`
and record the printed evidence in `tests/fixtures/README.md`.

**Verification V2** (spec v8 §17) checks the generation-settings tables against real servers
before a release that changes them. Run `python -m pytest -m live -s -k v2`. Each call prints one
`V2 {...}` JSON line with backend, version, route, model, provider, setting, value and outcome.

OpenRouter accounts with privacy settings or guardrails should pin an allowed provider with
`MODELSPELL_LIVE_OPENROUTER_PROVIDER`. Its value must exactly match a slug returned by
`list_providers`; an invalid value fails without falling back to another provider. OpenRouter
records include `pin_source` (`env` or `first_listed`). Discovery does not guarantee that account
guardrails allow a listed provider.

A run in which no supported setting is accepted fails after all probes have printed. Re-run it;
it verifies nothing and must not be recorded as verification evidence. This acceptance check
uses only supported-setting probes, not `extra` probes, which may legitimately be refused.
Partial unrelated failures still need human classification even when the acceptance check passes.

The V2 attempt helper takes evidence in an explicit `meta` mapping, separate from completion
arguments, so the evidence's provider slug cannot collide with or consume request routing.
Offline regression checks in `tests/test_live_helpers.py` cover supported-setting and `extra`
probes on all routes, including provider forwarding and printed evidence. Run them with
`python -m pytest tests/test_live_helpers.py`; they require no live credentials.

- `accepted` means the request succeeded; it never means the setting was applied
  (`effect_unverified`). `refused` is a 4xx attributable to the request; `unrelated` is auth,
  availability, rate limiting or network, and changes nothing (re-run it).
- A refused *supported* setting fails the test. Classify it before release as a route limitation
  (same refusal across models/providers, or documented: move the setting to "—" for that route),
  a model or provider limitation (record it; the README lists it; the table does not change), or
  unrelated. Ranges change only on the evidence rule in spec v8 §6.8.2.
- Paste the records and the classification into `tests/fixtures/README.md`. A backend that was
  not available is recorded as skipped with the reason, and its README rows stay marked
  unverified; never describe a skipped check, or one `openai_compat` server, as verification.

Live checks record acceptance and refusal only, never model behaviour: sampled output is not
reproducible, so no release, table or README claim is decided from it (spec v8 §17, M21-3).

**LM Studio probe (a diagnostic, not a test).** `tests/probe_lmstudio.py` sends one small set of
requests per generation setting to LM Studio and prints what came back, with a plain summary and
reading hints. It gives no verdicts, pytest does not run it, and nothing depends on its output.

```bash
python tests/probe_lmstudio.py --reasoning off 2>&1 | tee probe-off.txt   # native route
python tests/probe_lmstudio.py --reasoning low 2>&1 | tee probe-low.txt   # Responses route
```

It uses the LM Studio live variables, or `--url` and `--model`; `--help` lists the options.
`tests/test_probe_lmstudio.py` checks offline only what it sends and prints.

**Fixtures.** `tests/fixtures/README.md` holds the capture procedure and the coverage table. All
current fixtures are synthetic. When you capture a real response, check it for your key
(`grep -cF "$OPENROUTER_API_KEY" <file>` must print 0) and add a row with backend, version and date.

## Owner checks

The owner checks are a fixed battery of live checks in `ui/owner_checks.py`, built only on
modelSpell's public API. The developer UI and `pytest -m live` run the same code. Prompts, token
caps and timings are constants, so records compare across runs.

| Preset | Checks | Calls | Requests |
|---|---|---|---|
| Quick | `stream_usage`, `cancel_first_token`, `cancel_mid_stream` | 3 | 3 |
| Cancellation | `cancel_pre_send`, `cancel_first_token`, `cancel_mid_stream`, `cancel_non_stream`, `cancel_late`, `cancel_reused` | 6 | 4 |
| Usage | `stream_usage`, `nonstream_usage` | 2 | 2 |
| Full (default) | all eight, each once | 8 | 6 |

- **Route selector.** `default` sends `reasoning=None` (LM Studio: Responses). `native` (LM Studio
  only) sends `reasoning="off"` and passes a `ModelInfo`: the caller's, or one found by a single
  `list_models()` call in a preparation stage (one extra request, shown in the guard).
- **Row states** are `pass`, `fail`, `not_applicable`, `inconclusive` and `blocked`; only `pass`
  counts as passed. Auth, connection, 402, 404, 429 and 5xx failures are `blocked`; an auth or
  connection failure ends the run. Live cancellation rows are classified by what the public API can
  prove: a `Result` after an in-flight `cancel()` is `inconclusive` (the call may have committed
  first) unless two or more callbacks on one channel started after `cancel()` returned.
- **Run verdict:** `fail` if any row fails; `pass` only if every applicable row passes and the run
  was neither stopped nor stuck; otherwise `incomplete`.
- **Running.** In the UI: choose the backend and model in Model settings, click **Run owner
  checks**, then **Copy record**. From pytest: `python -m pytest -m live -s -k owner_checks`, with
  the live variables above and `MODELSPELL_LIVE_PRESET`. pytest prints the record, fails on `fail`,
  skips an `incomplete` run with the rows that did not pass, and passes only on `pass`. A stuck call
  becomes a `fail` row with `cleanup_pending`, never a hung test.
- **Cost.** The record shows a *reported cost subtotal* (the sum of `Result.cost` where reported)
  and an *unknown cost* count: requests that were sent or may have been sent and reported no cost
  (every `Cancelled` with `sent=True`). Nothing is estimated; with any unknown-cost request the
  subtotal is labelled incomplete.
- **Recording.** Paste the copied record into `tests/fixtures/README.md` under "Owner checks", one
  row per backend, with unavailable backends recorded as blocked and the reason. The README may say
  a stage is "verified live" only if the check covering it passed in the recorded run.
- Classifying a `fail` row (modelSpell defect, battery defect, backend limitation) is the
  implementer's job, not part of the routine run. A conclusive `fail` attributable to modelSpell or
  the battery blocks the release until resolved; inconclusive and blocked rows never do.

## Developer test UI

```bash
python -m ui [--port N] [--no-browser]
```

It prints a `http://127.0.0.1:<port>/?t=<token>` address and opens it. The server:

- binds to `127.0.0.1` only and requires the per-launch token on every request, so other web
  pages in your browser cannot drive it or spend API credit;
- holds API keys in memory only, keyed by `(kind, base_url)`, so a key is never sent to a
  different endpoint and is never returned to the page (environment-variable fallback still
  applies, e.g. `OPENROUTER_API_KEY`);
- always streams, and shows the full `Result` as JSON under the output.

**Model settings vs generation settings.** The *Model settings* dialog holds what identifies the
model and how to reach it: model source, base URL, key, model ID and OpenRouter provider. These are
held once per process and shared by every tab. Every generation setting lives in the **request
panel** under the input and is sent with each request: reasoning, temperature and max tokens
always visible; top-p, top-k, min-p, repetition/presence/frequency penalty, seed and stop sequences
under *Advanced* (collapsed, with a count such as "Advanced (3 set, 1 not sent)").

- Generation settings are **per tab and per request, and never saved**: nothing goes to the
  server or browser storage, and a reload clears them. Saving settings is AgentWiz's job.
- A blank field is not sent (backend default); `0` is a value and is sent.
- Fields modelSpell does not send on the current route (from `supported` in `/api/state`, i.e.
  `supported_settings()`) are **disabled and not sent**, but keep their value; they are labelled
  "not sent at this reasoning level" or "not sent with this model source".
- The page refuses only what JavaScript/JSON would change silently: non-numeric text, non-finite
  numbers (`1e400`) and non-safe integers for max tokens, top-k and seed. Everything else, ranges
  included, is left to modelSpell, whose error arrives as an `error` event. The UI does not call
  `check_settings()`: it exists to exercise `complete()` itself.
- **"Settings sent"** in the Result panel (`settings_sent` in the `result` event, including the
  provider pin) records what was sent, not what the model applied.
- **Stop sequences** are one per line; a stop sequence containing a newline cannot be entered.
- **Cross-tab limits.** If another tab changes the model source, a page's `supported` lists are
  stale until it reloads; its next request may carry a setting the new route refuses, which
  arrives as an `error` event (nothing is dropped). No revision checks are made.
- `/api/config` refuses `reasoning` and `max_tokens` with HTTP 400 (they moved to the request
  panel). `/api/complete` refuses a `settings` value that is not an object, or has an unknown key,
  with HTTP 400 before streaming.

**Stop.** The page's Stop posts the request's ID (from the stream's first event,
`{"type": "started", "id": ...}`) to `POST /api/complete/stop`, which cancels that request's
`CancelHandle`. It acts at any stage, including prompt processing before the first token; the
stream then ends with one `{"type": "cancelled", "sent": ..., "partial": ...}` event and the status
line says "stopped — request not sent" or "stopped — the request may have been sent; the backend
may keep generating". If the stop request itself fails, the page closes the connection, which the
server notices at its next write (as before). The status line says "usage not reported" when a
result carries no usage.

**Owner checks section.** One button, **Run owner checks**, runs the Full preset against the
model settings above; **Copy record** copies the record. Other presets are under "Other presets",
and for LM Studio a route selector (Responses or native). `POST /api/checks` streams `plan`,
`stage` (preparation, native route only), one `check` per finished check, and `done`; while a check
runs, the handler writes a `progress` event about once a second, and a failed write of any event
stops the battery, so a closed browser is noticed within about a second. `POST /api/checks/stop`
stops the run. The server holds one battery; its slot stays occupied (409 for a new run) until every
worker thread has exited, which can be later than `done` when a call is stuck. `/api/state`
reports `presets`, `default_preset`, `checks_running` and `checks_stuck`.

`tests/test_ui.py` covers the HTTP contract (U1–U16: token, key handling, event stream, discovery,
disconnect → cancel, per-request settings, `supported`, refusals, zero values, Stop through the
handle, the owner-check routes, Stop, presets and the stuck slot). The HTML/JS is not tested
automatically. After changing the page, run this manual check list once, against a real backend
or a fake one:

1. On LM Studio, switch reasoning between off and low, and watch the advanced fields enable and
   disable.
2. Set top-k, collapse Advanced, and see "1 set". Then switch to low and see "1 set, 1 not sent".
3. Send with temperature and seed set, and find both under "Settings sent" in the Result panel.
4. Enter `1e400` as temperature, and see the page refuse it without sending.
5. Enter temperature 3 on OpenRouter, and see modelSpell's `ConfigError`.
6. Reset, and see every field blank.
7. Against a backend held before its first token (a large prompt on a local model, or the fake),
   Stop shows "stopped — the request may have been sent" within a second.
8. Open "Owner checks": Full is selected; read the guard; click **Run owner checks** against the
   fake; watch the status and table fill; Copy record and paste it somewhere. Then choose Quick
   under "Other presets", run it, and reload: the selector is back on Full.
9. Against a backend that reports no usage (the `openai_compat` fake), the status line says
   "usage not reported".
10. Close the tab during a run; reopen the page: `checks_running` is false within a few seconds,
    and a new run starts.

## Changing behaviour

The spec is the contract. A behaviour change updates, in the same change:

1. `docs/modelspell_v1_spec_v9.md` (or a new spec version), including §14 if it deviates from NEL;
2. the tests named in the spec for that behaviour;
3. `README.md` where it documents the behaviour;
4. `NEWS.md` under the next version;
5. `docs/modelspell_design_history.md`, if the change records a design decision.

A setting is sent on a route only if the route's documentation defines it, directly or through
the reference that documentation points to for fields it does not list (spec v8 §7.2, M21-2).

## Branches and releases

Work happens on `devel`. `main` only receives merges from `devel`. `tests.yml` runs on pushes to
both and on pull requests, and exposes `workflow_call` for the release workflow. Its four-job
matrix runs Python 3.10 and 3.13 on `ubuntu-latest`, Python 3.10 on `macos-15-intel`, and
Python 3.13 on `macos-15` (Apple Silicon). Current `actions/python-versions` builds of Python
3.10 are available for Intel macOS, not ARM64; the split covers both Mac architectures without
pinning an old interpreter patch. Explicit macOS image labels make OS-image migrations deliberate.

Each job has a 20-minute timeout, creates `.venv` with the selected Python, installs
`-e '.[dev]'` using `.venv/bin/python -m pip`, and runs `.venv/bin/python -m pytest`.
Skip reasons remain visible (`-rs` in the pytest configuration). In particular, a skipped
`held_connect` fixture means connect-stage cancellation was not verified on that runner;
do not treat it as a pass or add blanket macOS skips. Live checks skip without credentials.

Each job also runs `.venv/bin/python scripts/build_release.py --smoke` as a required step.
This verifies the committed wheel and its clean-venv installation even if the suite's K3/K7
test skips because wheel building is unavailable; build failures fail the job instead.
The release workflow waits for all four test jobs, so macOS failures block publication too.

### OpenRouter verification and release checklist

1. Update the version, NEWS, spec, documentation and evidence summary on `devel` before building
   the final candidate. Record blocked or failed cases and their diagnoses. Keep previous live captures.
2. From the committed candidate checkout, export the rotated, funded `OPENROUTER_API_KEY` and run
   `python3 scripts/verify_provider_refactor.py`. It uses the repo's `.venv` for the offline suite,
   pins the source commit, builds and smoke-tests the candidate, then runs the committed harness
   from a fresh wheel-only environment. Reports and wheels are retained per run under
   `reports/provider-refactor/`.
3. Check the last line, `release_gate pass|fail|pending`. The named gate requires one Automatic
   Balanced ZDR pin above 50 tokens/s to complete streamed and non-streamed VT on the same
   candidate and effective policy. Exit 0 means the full run passed. Exit 1 means a correctness
   check failed. Exit 2 means the named gate passed with other live cases blocked. Exit 3 means the
   named gate is pending. Review every blocked and failed case. Resolve all named response-format
   incidents before publication.
4. For report-only triage run
   `python3 scripts/verify_provider_refactor.py --triage ORIGINAL_REPORT INCIDENT_ID upstream "Evidence-based reason"`.
   Use `modelspell` for a confirmed modelSpell defect. Each verdict produces a separate JSON file with
   cumulative outcomes. Retain the original and each verdict. If the named case needs another model
   or cost ceiling, use `--release-model`, `--release-max-input-price` or `--release-max-output-price`.
   Price overrides are USD per million tokens and the fixed charge remains zero.
5. Retest AgentWiz against the wheel and SHA-256 from the qualifying report before publication.
   Record the actual endpoint, model, transport, reasoning, pin and scope, provider or served variant,
   quantization and effective policy. Repeat VT under AgentWiz's actual policy when relevant routing
   constraints change or the pin is ambiguous. A defect requires a new candidate and fresh evidence.
6. Stage the exact verified wheel and the qualifying report (or latest qualifying triage record)
   on a GitHub draft release for the version. Name the staged report `provider-gate.json`.
   Target the tested source commit. Merge to `main` with the verified payload. If the merge changes
   the payload, verify a new candidate. The release workflow reruns offline checks, verifies the staged
   gate report, verifies that the tested source has the same payload as `main`, and publishes the
   staged wheel bytes. Run the workflow manually if the push ran before staging.
7. Check the published SHA-256 against the candidate and test the install line in a fresh environment.

For targeted OpenRouter V1, V2, and VT checks, set `MODELSPELL_LIVE_OPENROUTER_MODEL` in
addition to the key. The optional `MODELSPELL_LIVE_OPENROUTER_PROVIDER` is a strict manual
override. Without it the checks use Automatic Balanced. V1 and V2 share a compatible pin.
Streamed and non-streamed VT share their tool pin. Unpinned discovery, streaming and the
owner checks remain available independently.

Downstream projects should depend on the release wheel by URL and hash, e.g.

```
modelSpell @ https://github.com/alexchwong/modelSpell/releases/download/v0.4.0/modelspell-0.4.0-py3-none-any.whl#sha256=<sha256>
```

Releases are the supported install channel. Installing from an arbitrary commit
(`git+https://…@<sha>`) is possible on a public repository but unsupported.

## NEWS entries

Add user-visible changes under the intended version heading in `NEWS.md`, newest version first.
Each bullet holds one change in no more than 20 words. Describe what users see, not
implementation detail. Changes to tests, the UI or tooling do not need entries.
