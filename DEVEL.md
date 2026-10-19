# Developer guide

This file is for repository maintenance. Library usage is documented in `README.md`; the
behavioural contract is `docs/modelspell_v1_spec_v5.md`; agent operating rules are in
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
| `tests/` | no | Offline suite, fixtures, golden request bodies, opt-in live suite. |
| `ui/` | no | Developer test UI (`python -m ui`). |
| `scripts/build_release.py` | no | Builds and verifies the release wheel. |
| `scripts/smoke_installed.py` | no | Smoke test run against an installed wheel (K3, `--smoke`). |
| `examples/basic.py` | no | Usage example against a local LM Studio. |
| `docs/` | no | Spec v5 (authoritative) and design history (informative). |
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
| OpenRouter | `OPENROUTER_API_KEY`, `MODELSPELL_LIVE_OPENROUTER_MODEL` (`author/slug`) |

Run verification V1 with
`python -m pytest -m live -s tests/test_live.py::test_live_v1_discovery_to_routing_round_trip`
and record the printed evidence in `tests/fixtures/README.md`.

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

**Stop limitation.** Stop closes the browser request; the server notices on its next write, i.e.
when the next token arrives, and then cancels the upstream call. It cannot interrupt prompt
processing before the first token, because v1 cancellation exists only inside callbacks (§8.10).

`tests/test_ui.py` covers the HTTP contract (token, key handling, event stream, discovery,
disconnect → cancel). The HTML/JS is not tested.

## Changing behaviour

The spec is the contract. A behaviour change updates, in the same change:

1. `docs/modelspell_v1_spec_v5.md` (or a new spec version), including §14 if it deviates from NEL;
2. the tests named in the spec for that behaviour;
3. `README.md` where it documents the behaviour;
4. `NEWS.md` under the next version;
5. `docs/modelspell_design_history.md`, if the change records a design decision.

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
modelSpell @ https://github.com/alexchwong/modelSpell/releases/download/v0.1.0/modelspell-0.1.0-py3-none-any.whl#sha256=<sha256>
```

Releases are the supported install channel. Installing from an arbitrary commit
(`git+https://…@<sha>`) is possible on a public repository but unsupported.

## NEWS entries

Add user-visible changes under the intended version heading in `NEWS.md`, newest version first.
Each bullet holds one change in no more than 20 words. Describe what users see, not
implementation detail. Changes to tests, the UI or tooling do not need entries.
