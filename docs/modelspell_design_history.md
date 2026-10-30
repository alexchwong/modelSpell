# modelSpell v1 — design history

**Status:** Informative. This document records how the specification reached its current form. It is not part of the implementation contract; `modelspell_v1_spec_v7.md` is the only authoritative description of behaviour. Section and test references below use the current spec's numbering, which has not changed since spec v2 except by addition (v5 added §18 and K7; v6 added §6.7–§6.9, G10–G12, R10–R16, U6–U10 and V2; v7 added G13 and §19).
**Date:** 20 October 2026; round 5 added 27 October 2026; round 6 added 28 October 2026

## Document lineage

| Version | Date | Reviewed by | Outcome |
|---|---|---|---|
| `modelcall_v1_spec.md` | 19 October 2026 | `ModelSpell_v1_spec_critique.md` | Round 1 changes, applied in spec v2 |
| `modelspell_v1_spec_v2.md` | 20 October 2026 | `ModelSpell_v1_spec_critique_v2.md` | Round 2 changes, applied in spec v3 |
| `modelspell_v1_spec_v3.md` | 20 October 2026 | `ModelSpell_v1_spec_critique_v3.md` | Round 3 changes, applied in spec v4. The reviewer judged v3 ready for implementation; round 3 made two narrow edits and no further review round is planned. |
| `modelspell_v1_spec_v4.md` | 20 October 2026 | — | Implemented as package 0.1.0 |
| `modelspell_v1_spec_v5.md` | 20 October 2026 | — | Round 4 (post-implementation) tooling decisions; library behaviour unchanged. Implemented as package 0.1.0 |
| *modelSpell 0.2.0 technical specification* v1–v3 | 27 October 2026 | *modelSpell 0.2.0 technical specification — review v1* (against v2) | Round 5; v3 implemented as package 0.2.0 and folded into spec v6 |
| `modelspell_v1_spec_v6.md` | 27 October 2026 | — | Generation settings (package 0.2.0) |
| *modelSpell 0.2.1 technical specification* v1–v6 | 28 October 2026 | *review v1* to *review v5*, each against the version before it; *review v3* was not seen by the author of v4 to v6; v4 was based on the implementer's in-conversation review of v3 | Round 6; v6 implemented as package 0.2.1 and folded into spec v7 |
| `modelspell_v1_spec_v7.md` | 28 October 2026 | — | Current. `top_p` on LM Studio Responses (package 0.2.1) |

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

---

## Round 5 — generation settings (spec v6, package 0.2.0)

Requested by AgentWiz release plan v10.1, decision R2-19 (advanced task settings must not travel through `extra=`), and by the owner (customisable settings in the developer UI's request panel). Specified in the *modelSpell 0.2.0 technical specification*, versions 1–3 (27 October 2026); version 3 answered *review v1*. IDs are prefixed `M2-` to keep them distinct from AgentWiz's.

| ID | Decision | Sections |
|---|---|---|
| M2-1 | Version 0.2.0: an additive API change with two narrow validation tightenings (non-finite numbers; documented `temperature` ranges). Independent of AgentWiz numbering. | Header, §3 |
| M2-2 | Eight named, keyword-only arguments. `None` means not sent; zero is a value. No generic `sampling=` mapping. | §6, §6.1 |
| M2-3 | Support follows documentation per route (§7.2's rule) and means "modelSpell sends it". LM Studio Responses keeps `temperature` and `max_tokens` but takes no sampling arguments; this is modelSpell's contract, not a claim about LM Studio. `openai_compat` takes the standard Chat Completions fields plus the extensions `top_k` and `min_p`, but not `repetition_penalty`, whose name differs between servers. | §6.7 |
| M2-4 | An unsupported setting raises `UnsupportedSetting` before sending; it is never dropped silently. | §6.7.2 |
| M2-5 | Value checks are kind-independent and based on each setting's meaning; route checks enforce documented ranges only; every numeric setting must be finite. | §6.8 |
| M2-6 | `supported_settings(kind, reasoning)` returns a `frozenset` of the generation settings modelSpell sends on that route; pure; raises as `complete()` would; derived from the same table as the checks and body building. | §6.9 |
| M2-7 | `stop` must be a list or tuple of non-empty strings, annotated as such; a bare string is refused. | §6.8.1 |
| M2-8 | `provider` stays verbatim. modelSpell does not add `require_parameters`; it documents it. | §6.7.3 |
| M2-9 | V2 runs before release as an opt-in compatibility check: outcomes `accepted`, `refused`, `effect_unverified`; refusals classified as route, model/provider or unrelated, and only route evidence changes the table; ranges change only under §6.8.2's rule; skipped backends ship marked unverified. | §17 |
| M2-10 | The UI dialog keeps model source, base URL, key, model ID and OpenRouter provider; every generation setting moves to the request panel and is sent per request. | §18.3 |
| M2-11 | The page learns which fields are sent from `supported` in `/api/state` (computed with `supported_settings()`); unsent fields are disabled, keep their values, are labelled with the reason, and are not sent. | §18.3 |
| M2-12 | The page refuses only what JavaScript or JSON would change silently (non-numeric text, non-finite numbers, unsafe integers for max tokens, top-k and seed); everything else is left to modelSpell. | §18.3 |
| M2-13 | Generation settings are per tab and not saved; connection settings stay process-wide; no revision checks. | §18.3 |
| M2-14 | `/api/config` refuses `reasoning` and `max_tokens` with 400; `/api/complete` refuses a malformed `settings` object with 400; modelSpell's refusals arrive as an `error` event; the `result` event carries `settings_sent` (what was sent, not applied). | §18.3 |
| M2-15 | A public, pure `check_settings(kind, **settings)` runs exactly `complete()`'s setting checks, from the same code, without endpoint, key, environment or network, so a caller can refuse invalid settings before committing an HTTP response. | §6.9, R16 |
| M2-16 | No per-model, per-provider or per-server capability knowledge in modelSpell: no discovery of parameter support, no probing, no cache, no server auto-detection, no server-specific mappings within `openai_compat`. Restrictions are documented, not encoded. | §1 |

**Implementation notes (27 October 2026).**

- `settings_sent` includes the provider pin when one is set, because §18.3 defines it as the generation arguments passed to `complete()`, and `provider` is one.
- V2 has **not** been run: no LM Studio, OpenRouter key or llama-server was reachable from the implementation environment. Every support-table row ships as documented and is marked unverified in the README. No table or range has changed from the technical specification.

---

## Round 6 — `top_p` on LM Studio Responses (spec v7, package 0.2.1)

Requested by the owner after an LM Studio Responses probe on 28 October 2026. Specified in the *modelSpell 0.2.1 technical specification*, versions 1–6 (28 October 2026). The decisions are listed in spec v7 §19:

- **M21-1:** `top_p` on the Responses route.
- **M21-2:** documentation by reference.
- **M21-3:** no decision depends on model output.
- **M21-4:** README wording for the other seven fields.
- **M21-5:** refusal messages.
- **M21-6:** the probe diagnostic.

**Withdrawn: effect checks of model output (versions 1–5).** Versions 1 to 5 admitted `top_p` on effect evidence. Their check was E1, a live comparison of sampled answers. It grew a release gate and campaigns, and an evidence rule with "E-checks". Each review then found, correctly, a further gap:

- what `top_p=0.01` proves;
- incomplete answers counted as valid;
- a weak comparison rule;
- retries that undid the false-pass bound;
- interrupted sessions invisible to the gate.

The cause was structural. A test on sampled output is a statistical test, and making it sound kept adding machinery. It also held one field to a bar no other modelSpell setting meets, since modelSpell claims "sent", never "applied", for every route (§6.7.3). Version 6 withdrew the approach. It admitted `top_p` on the same basis `temperature` and `max_output_tokens` already had: LM Studio's Responses page refers to OpenAI's Responses reference, which defines all three. The release gate became deterministic: the offline suite, plus V2 acceptance of `top_p` through the named argument.

**Reversed:** review v2's wording (V2-2, adopted in the specification's version 3), "a reference to another API's documentation alone does not qualify as route documentation". It is replaced by M21-2.

**Not adopted:**

- review v1's per-server comparison table for `openai_compat`, which is unrelated and listed as a follow-up;
- an offline test of disagreement between E1 sessions (version 2), made moot by the withdrawal.

**Evidence.** On 28 October an informal probe was run (`qwen3.8-27b`, reasoning "low"). `top_p=0.01` gave three identical answers where the baseline gave three different ones. No other unsent field showed an effect. That observation prompted the change; it is not its basis (M21-3). It is summarised in `tests/fixtures/README.md`.

**ModelInfo addition (29 October 2026, M21-7–M21-9).** LM Studio refused native requests for
`qwen3-coder-next` with HTTP 400: "does not expose reasoning configuration" (`param: reasoning`).
The owner checked with curl that the same request without `reasoning` succeeded:
`{"model":"qwen3-coder-next","input":"Say ok.","store":false,"top_k":20}` returned a normal answer.
`list_models()` already exposes the missing configuration as `ModelInfo.reasoning_options=None`.
The caller may now pass that entry to `complete()`, which sends its id and omits native `reasoning`
when the options are absent or empty. String models behave as before. The 0.2.1 technical
specification (v6) said "public API unchanged"; this additive API change, decided by the owner,
supersedes that statement. Package 0.2.1 is no longer API-identical to 0.2.0; spec v7 is amended
in place. No library discovery or cache is introduced, and the settings helpers remain unchanged.
Non-empty options lacking `"off"` are left to the server, keeping settings validation consistent.

Alternatives rejected:

- Discovery inside `complete()`: two requests per call, and contrary to M2-16.
- Retry on HTTP 400: contrary to the no-retries contract.
- Always omitting `reasoning`: reasoning models would then reason.
- A new parameter: `ModelInfo` already carries the information.

**Follow-up:** the developer UI still passes the model id string; changing it is outside this work.
