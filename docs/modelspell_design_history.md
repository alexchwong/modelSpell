# modelSpell v1 — design history

**Status:** Informative. This document records how the specification reached its current form. It is not part of the implementation contract; `modelspell_v1_spec_v5.md` is the only authoritative description of behaviour. Section and test references below use the current spec's numbering, which has not changed since spec v2 (v5 only added §18 and test K7).
**Date:** 20 October 2026

## Document lineage

| Version | Date | Reviewed by | Outcome |
|---|---|---|---|
| `modelcall_v1_spec.md` | 19 October 2026 | `ModelSpell_v1_spec_critique.md` | Round 1 changes, applied in spec v2 |
| `modelspell_v1_spec_v2.md` | 20 October 2026 | `ModelSpell_v1_spec_critique_v2.md` | Round 2 changes, applied in spec v3 |
| `modelspell_v1_spec_v3.md` | 20 October 2026 | `ModelSpell_v1_spec_critique_v3.md` | Round 3 changes, applied in spec v4. The reviewer judged v3 ready for implementation; round 3 made two narrow edits and no further review round is planned. |
| `modelspell_v1_spec_v4.md` | 20 October 2026 | — | Implemented as package 0.1.0 |
| `modelspell_v1_spec_v5.md` | 20 October 2026 | — | Current. Round 4 (post-implementation) tooling decisions; library behaviour unchanged |

---

## Round 1 — changes from `modelcall_v1_spec.md`

Rows describe the spec as it stands in v4. Where round 2 changed an outcome, the row says so and points to the round 2 row (B1–B8).

| # | Review item | Change in the spec | Sections |
|---|---|---|---|
| 1 | Provider routing identifier | Name-derived `slug` fallback removed; `slug = tag`, verbatim (case, `/` suffixes), `None` if absent. Documented as OpenRouter's endpoint tag. Explicit identifiers go through `provider=`. Live round trip asserts `Result.provider` for `order=[tag], allow_fallbacks=False`, preferring a provider with several variants. | §7.1, §10.1, §10.3, §12.7 D2, §14 #17, §17 V1 |
| 2 | Native truncation and `extra` conflict | Effective token cap read from the merged body; `finish_reason=None` on the native route; heuristic kept and documented; `usage is None` guarded; G8 override retained. | §6.4, §8.1, §8.8, §11, §12.4 P7, §13, §14 #8 |
| 3 | Secrecy guarantee | Guarantee narrowed to strings the package constructs; callback exceptions and model or server data explicitly excluded. Bodies read up to 64 KB, redacted, then truncated. HTTP-derived exceptions raised `from None`. Sentinel tests updated, including a straddling-key case and a boundary check. | §4.3, §9.2, §9.3, §12.6 E5 |
| 4 | Native flattening and storage; Responses retention | Native sends `store: false` by default (G3, G4 updated; `response_id` `None`). Why flattening is unavoidable is explained, with a README requirement. §7.2 "stateless" wording corrected. Round 2 replaced v2's conditional Responses `store` (V2) with a fixed default: no `store` on Responses (B1). | §7.2, §7.3, §7.4, §8.8, §12.3 R9, §12.4 P9, §13, §14 #14 |
| 5 | Malformed SSE data events | Invalid or non-object `data:` payloads raise `ResponseFormatError(status=200)`; supported format stated (one object per `data:` line); multi-line data unsupported and raises. | §8.9, §9.2, §12.5 S4, §14 #16 |
| 6 | Streaming and cancellation | Kept. Streaming recommended for long generations; non-streaming timeout consequence documented. | §6.2, §9.4 |
| 7 | `ProviderInfo` / `Pricing` | `status`, `uptime_last_30m` and `max_prompt_tokens` removed from `ProviderInfo` (available in `raw`); `Pricing` kept. | §10.1, §12.8 K1 |
| 8 | Tool-calling promise | "Without breaking changes" removed; v1 "should avoid foreclosing"; constraints kept. | §1, §15 |
| 9 | `openai_compat` | Kept, with a narrow compatibility claim. Round 2 reworded the named servers as untested candidates (B4). | §1 |
| 10 | Fixture recorder | `scripts/record_fixtures.py` removed; `curl` capture procedure documented. | §11, §12.2, §16 |
| 11 | Synthetic fixtures | Permanent for edge cases; captured success paths required only where a server is available; coverage table. | §12.2, Definition of done |
| 12 | S2 equivalence | Pairs constructed from one canonical response; live calls never compared. | §12.2, §12.5 S2, §12.9 |
| 13 | Snapshots and layout | K1 kept as is; §11 marked non-normative. | §0, §11, §12.8 K1 |
| 14 | Cancellation and redirect test semantics | Remote stop demoted to a note; S11 asserts local closure only. Redirects disabled rather than counted (see M1). | §8.10, §12.5 S11 |
| 15 | Tuples vs lists; dicts in frozen types | Discovery returns `list`; tuples inside result types; dict fields read-only by convention and `field(hash=False)`; K6 hashability test. | §2, §4, §8.1, §10.1, §12.7 D5, §12.8 K6 |
| 16 | Controlled adaptation | Stated in §0 with its three categories; README requirement. | §0, Definition of done |
| 17 | Per-subtask configuration | README example; no registry. | §1, §6.6 |
| 18 | Rename | `modelcall` → `modelspell`; `ModelCallError` → `ModelSpellError`; `MODELCALL_LIVE_*` → `MODELSPELL_LIVE_*`; PyPI availability note. | Header, §3, §9.1, §11, §12 |
| M1 | Redirect key leak | Per-call `build_opener(_NoRedirectHandler())`; `urlopen` forbidden; 3xx → `BackendError` naming the redacted `Location`; proxy test added. | §2, §4.1, §4.3, §6.3, §9.2, §9.5, §12.6 E1 E8 E9, §12.8 K5, §13, §14 #15 |

---

## Round 2 — changes from `modelspell_v1_spec_v2.md`

| # | Review item | Change in the spec | Sections |
|---|---|---|---|
| B1 | Responses-route storage default | Fixed default: no `store` on the Responses route, following the rule "send only fields the backend documents for the route". V2 deleted, along with v2's conditional deviation #18, the conditional notes on G1 and G2, the V2 note in R1, the V2 mention in the live suite, and the V2 bullet in the definition of done. The README states that Responses retention is backend-dependent and undocumented, and that `extra` can carry `store` with unverified effect. V1 is kept because no code depends on it. | Header, §7.2, §7.4, §12.3 R1 R9, §12.9, §14, §16, §17, Definition of done |
| B2 | Terminal events end the read | Responses stops on `response.completed` / `response.incomplete` / `response.failed` with a dict `response`; native stops on `chat.end` with a dict `result`. Both then parse and close; data after the terminal event is neither parsed nor validated. Chat Completions is unchanged (`[DONE]` or end of body), because `finish_reason` precedes the usage chunk. The native rule that an `error` event precedes `chat.end` is kept. New deviation #18 (the number v2 used for the removed conditional) and test S13. | §8.5, §8.7, §8.8, §8.10, §11, §12.5 S13, §14 #18, §16 |
| B3 | `extra` override boundary | §6.4 states that §7 bodies are defaults, that raw overrides are unvalidated and the caller's responsibility, and that overriding `messages` / `input` bypasses §5 and §7.3. It explains why only `model` and `stream` are blocked. "`previous_response_id` is never sent" becomes "ModelSpell never adds `previous_response_id`", and the native "always sent" becomes "sent by default". R9 also checks the Responses and `previous_response_id` defaults. The §13 migration note states the native `response_id` behaviour as a default. README requirement added. | §1, §6.4, §7.2, §7.3, §8.1, §12.3 R9, §13, §14 #14 and unchanged list, Definition of done |
| B4 | `openai_compat` named servers | llama-server, vLLM and Ollama are named as candidate servers; none is covered by the test suite or live checks. | §1, Definition of done |
| B5 | Hashability (K6) | Kept as written. Without `field(hash=False)`, hashability would depend on whether optional dict fields are set, which breaks `functools.lru_cache` and dict keys for `Endpoint`. | §2, §12.8 K6 (unchanged) |
| B6 | Field-name snapshot (K1) | Kept as written. An intentional change updates the spec and the snapshot together. | §12.8 K1 (unchanged) |
| B7 | Discovery fields and `Pricing` | No change. | §10.1 (unchanged) |
| B8 | Streaming and callback cancellation | No change; retained. | §1, §8.10 (unchanged) |

---

## Round 3 — changes from `modelspell_v1_spec_v3.md`

| # | Review item | Change in the spec | Sections |
|---|---|---|---|
| C1 | S13 timing | S13 no longer measures elapsed time. §12.1 adds a hold-open mode: the body stays open until a release event, and the handler polls its socket and records `client_closed`. S13 asserts the expected `Result` (or, for the native `error` case, the `BackendError`), never `RequestTimeout`, plus `client_closed`. A 30 s watchdog catches hangs. The 10 s hold and the under-1 s requirement are removed. `timeout=2` only bounds how long a broken implementation takes to fail. The mechanism was checked on Python 3.13 for chunked and connection-close bodies. | §12.1, §12.5 S13, §16 |
| C2 | Review history inside the contract | The round 1 and round 2 change maps (spec v3's Appendices A–B) and §17's note on the removed V2 check moved to this document. The spec header keeps a one-line pointer here, and §14 stays in the spec. Rationale sentences that guard current decisions, such as §7.2's "No live check decides this default", stay in the spec because they describe the current contract rather than its history. | Header, §17; spec v3 Appendices A–B removed |

Sections the review found no reason to change in round 3: functional scope (streaming and cancellation, `Pricing`, `openai_compat`, raw usage and reasoning details, K1, K6), and the documented limits, which the README list in the definition of done already covers.

---

## Round 4 — post-implementation tooling decisions (spec v5)

Decided in discussion after package 0.1.0 was implemented. None changes library behaviour.

| # | Decision | Reason | Sections |
|---|---|---|---|
| D1 | Display and distribution name `modelSpell`; import name stays `modelspell`. | pip normalises distribution names, so the mixed-case name costs nothing there. A mixed-case import name breaks PEP 8, and Python imports are case-sensitive on every OS. | Header, §11 |
| D2 | Version single-sourced in `modelspell/_version.py`, not a text `VERSION` file. | Reading a text file or package metadata at import would break §2 (no file I/O at import). Hatch reads the `.py` file natively. | §2, §3, §18.1 |
| D3 | MIT license. | Owner's choice; needed before publishing release assets. | Header, §2 |
| D4 | Release artifact is the wheel only, attached to a GitHub release; no curated source zip. NEL's curated zip was considered and rejected. | The wheel already excludes tests, UI and docs, installs without a build step (works offline) and can be pinned by URL and sha256. A curated zip would duplicate the wheel's job with its own manifest, and pip would need to fetch the build backend to install it. Installing from arbitrary commits cannot be prevented on a public repo; releases are the supported channel. | §18.2 |
| D5 | Release workflow modelled on NEL's, with two changes: it calls `scripts/build_release.py` instead of duplicating its logic inline, and it runs the test suite (Python 3.10 and 3.13) before releasing. | NEL's workflow re-implements its build script in bash, so the two can drift; it also releases without running tests. | §18.2 |
| D6 | Developer test UI in top-level `ui/`, outside the package and never shipped; session token and per-endpoint key storage kept even though it is dev-only; reasoning, max-tokens and full-`Result` controls added to the requested fields. | A UI inside the package would break §2. The token prevents other browser pages from driving the loopback server; per-endpoint keys prevent sending a key to the wrong server. The extra controls are needed to exercise every route. | §1, §18.3 |
| D7 | Stop in the UI takes effect only at the next token. | v1 cancellation exists only inside callbacks (§8.10), so nothing can interrupt a call waiting on prefill. Recorded as a v2 candidate: a cancellation handle accepted by `complete()`. | §18.3 |
| D8 | `NEWS.md` and `DEVEL.md` at the repository root; spec and design history in `docs/`. | Owner's choice for NEWS/DEVEL; tests cite spec section numbers, so the spec lives with the code. | §11 |
