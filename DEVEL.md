# Developer guide

This file is for repository maintenance. Library usage is documented in `README.md`; the
behavioural contract is `docs/modelspell_v1_spec_v7.md`; agent operating rules are in
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
| `tests/` | no | Offline suite, fixtures, golden request bodies, opt-in live suite, and the LM Studio probe diagnostic (`tests/probe_lmstudio.py`). |
| `ui/` | no | Developer test UI (`python -m ui`). |
| `scripts/build_release.py` | no | Builds and verifies the release wheel. |
| `scripts/smoke_installed.py` | no | Smoke test run against an installed wheel (K3, `--smoke`). |
| `examples/basic.py` | no | Usage example against a local LM Studio. |
| `docs/` | no | Spec v7 (authoritative), specs v5 and v6 (superseded, kept as history) and design history (informative). |
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
- **One HTTP request per `complete()` call.** No retries, no route or streaming fallback, no
  redirect following; requests go through the per-call `_NoRedirectHandler` opener, never
  `urlopen` (§6.3, §9.5).
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
adding cases.

**Live checks** (`tests/test_live.py`) run only when their variables are set, and each call is
checked on its own:

| Backend | Variables |
|---|---|
| LM Studio | `MODELSPELL_LIVE_LMSTUDIO_URL` (e.g. `http://localhost:1234/v1`), `MODELSPELL_LIVE_LMSTUDIO_MODEL` |
| OpenRouter | `OPENROUTER_API_KEY`, `MODELSPELL_LIVE_OPENROUTER_MODEL` (`author/slug`); optional `MODELSPELL_LIVE_OPENROUTER_PROVIDER` (provider slug from `list_providers`; default: first listed provider with a slug) |
| llama-server (V2 only, as `openai_compat`) | `MODELSPELL_LIVE_LLAMASERVER_URL` (including `/v1`), `MODELSPELL_LIVE_LLAMASERVER_MODEL` |

The LM Studio model may have reasoning configuration or none: native tests pass its `ModelInfo`
from a module-scoped discovery fixture; Responses tests retain the string id. Native V2 records carry
`model_reasoning_options`. When a change touches LM Studio's native request, run
`python -m pytest -m live -s -k "test_live_lmstudio and off"` once with a model that has reasoning
configuration and once with one that has none (two short requests each), and record both in
`tests/fixtures/README.md`. These checks concern HTTP acceptance and transport, not model output.
The probe is a diagnostic and is never part of a release check.
The probe also discovers once, passes the matching entry (or falls back to the id), and reports
only usable answers in its summaries, including each arm's ok count.

Run verification V1 with
`python -m pytest -m live -s tests/test_live.py::test_live_v1_discovery_to_routing_round_trip`
and record the printed evidence in `tests/fixtures/README.md`.

**Verification V2** (spec v7 §17) checks the generation-settings tables against real servers
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
  unrelated. Ranges change only on the evidence rule in spec v7 §6.8.2.
- Paste the records and the classification into `tests/fixtures/README.md`. A backend that was
  not available is recorded as skipped with the reason, and its README rows stay marked
  unverified; never describe a skipped check, or one `openai_compat` server, as verification.

Live checks record acceptance and refusal only, never model behaviour: sampled output is not
reproducible, so no release, table or README claim is decided from it (spec v7 §17, M21-3).

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

**Stop limitation.** Stop closes the browser request; the server notices on its next write, i.e.
when the next token arrives, and then cancels the upstream call. It cannot interrupt prompt
processing before the first token, because v1 cancellation exists only inside callbacks (§8.10).

`tests/test_ui.py` covers the HTTP contract (U1–U10: token, key handling, event stream, discovery,
disconnect → cancel, per-request settings, `supported`, refusals, zero values). The HTML/JS is not
tested automatically. After changing the page, run this manual check list once, against a real
backend or a fake one:

1. On LM Studio, switch reasoning between off and low, and watch the advanced fields enable and
   disable.
2. Set top-k, collapse Advanced, and see "1 set". Then switch to low and see "1 set, 1 not sent".
3. Send with temperature and seed set, and find both under "Settings sent" in the Result panel.
4. Enter `1e400` as temperature, and see the page refuse it without sending.
5. Enter temperature 3 on OpenRouter, and see modelSpell's `ConfigError`.
6. Reset, and see every field blank.

## Changing behaviour

The spec is the contract. A behaviour change updates, in the same change:

1. `docs/modelspell_v1_spec_v7.md` (or a new spec version), including §14 if it deviates from NEL;
2. the tests named in the spec for that behaviour;
3. `README.md` where it documents the behaviour;
4. `NEWS.md` under the next version;
5. `docs/modelspell_design_history.md`, if the change records a design decision.

A setting is sent on a route only if the route's documentation defines it, directly or through
the reference that documentation points to for fields it does not list (spec v7 §7.2, M21-2).

## Branches and releases

Work happens on `devel`. `main` only receives merges from `devel`. `tests.yml` runs on pushes to
both and on pull requests (Python 3.10 and 3.13).

`release.yml` runs on every push to `main` (and manually). It reruns the tests, then:

- **If `vX.Y.Z` is not yet released:** builds the wheel from `HEAD`, verifies its contents and
  metadata, installs it into a fresh venv and runs the smoke test, then creates release `vX.Y.Z`
  with the wheel attached. The release notes are the NEWS section plus a pinned install line with
  the wheel's sha256.
- **If `vX.Y.Z` already exists:** fails if any shipped file (`modelspell/`, `pyproject.toml`,
  `README.md`, `LICENSE`) changed since the tag, so shipped changes cannot reach `main` without a
  version bump. Changes to tests, the UI, scripts or docs merge without a release.

### Release checklist

1. On `devel`, bump `modelspell/_version.py`.
2. Add `## X.Y.Z` to `NEWS.md` (the build fails without it).
3. Commit, then run `python scripts/build_release.py --smoke` locally. It builds from the
   committed `HEAD`; uncommitted edits are excluded and reported.
4. Merge `devel` into `main`.
5. Check the release page: wheel attached, notes correct, install line works in a fresh venv.

Downstream projects should depend on the release wheel by URL and hash, e.g.

```
modelSpell @ https://github.com/alexchwong/modelSpell/releases/download/v0.2.0/modelspell-0.2.0-py3-none-any.whl#sha256=<sha256>
```

Releases are the supported install channel. Installing from an arbitrary commit
(`git+https://…@<sha>`) is possible on a public repository but unsupported.

## NEWS entries

Add user-visible changes under the intended version heading in `NEWS.md`, newest version first.
Each bullet holds one change in no more than 20 words. Describe what users see, not
implementation detail. Changes to tests, the UI or tooling do not need entries.
