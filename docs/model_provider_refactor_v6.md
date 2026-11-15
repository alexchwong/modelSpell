# Model provider refactor v6

**Target release** — modelSpell 0.4.0  
**Status** — Revised specification for implementation  
**Scope** — OpenRouter provider selection, tool routing, and one-command verification

## 1. Release and routing choices

Ship one public OpenRouter selector shared by the modelSpell library, developer UI, and live harness, with a public contract for AgentWiz. Hold 0.4.0 until the final candidate passes the named live acceptance case in §7.

| Choice | Behaviour |
|---|---|
| **OpenRouter-managed** | Ordinary completions use OpenRouter routing. Existing callers may pass native routing preferences through the completion API. |
| **Manual pin** | Use the requested slug verbatim and disable fallbacks. The selector and harness validate it against discovery and report available slugs if it is invalid. A failed pin remains a failure for that pin. Existing direct calls retain their current validation and routing semantics. |
| **Automatic** | Rank and confirm a route before the run starts. Return one pin and its effective routing policy to the caller. Default to Balanced when Automatic is selected. |

The developer UI exposes all three choices and shows the optional Balanced floor when applicable. OpenRouter tool workflows require Automatic or Manual pin. If an existing tool workflow is configured as Managed, the UI or downstream caller explains the pin requirement and stops before sending a request. A new developer-UI tool workflow is outside this refactor.

## 2. Public API, lifetime, and pin scope

- Add `choose_provider()`, an immutable selection type, and a selection error carrying sanitized evidence. Specify and export all three, update the public API snapshot (K1), and cover the selector in the installed-wheel smoke. Bad local inputs use the existing configuration-error path. Redact upstream errors before attaching structured evidence.
- The selector takes the endpoint, model, selection mode, any explicit pin, caller routing policy, and intended request requirements. It returns the chosen slug, the immutable effective routing policy, and evidence. Pass the selector's immutable policy directly to either completion API and its preflight. The selector emits only standard JSON scalar types, with finite numbers. For ordinary encoding, thaw the selector's policy recursively into ordinary dictionaries and lists. Leave the existing plain-dictionary encoding path intact. Test both preflights, both completion entry points, and wire-byte equality with the equivalent plain policy, including nested `max_price`. Existing tool-specific Decimal encoding continues to apply to tool callers. The selector performs I/O when called explicitly. In Automatic, budget one model endpoint discovery request, one optional ZDR catalogue request, at most one optional model-metadata request to resolve a missing permaslug, and up to four completion attempts including retries.
- The caller owns the selection for one conversation or compatible live-test scope. Discard it when connection, credential, model, policy, or request requirements change. The value contains no credential, credential fingerprint, endpoint object carrying a key, or raw diagnostic payload. Keep the library free of a selection cache. Reuse the effective routing policy through tool replay. Surface failures after the run starts. A caller may make a fresh selection for a new run.
- A strict pin uses one slug in `provider.order` with `allow_fallbacks: false`. Automatic ranks endpoint-specific prices or speeds only for tags that identify one published route. Reject duplicate tags. For tag `T`, treat any published descendant beginning with `T/` as an overlap unless documented routing rules and available metadata establish that the descendant cannot match this request. A service-tier marker alone is insufficient because some tier endpoints are already in the routing pool. Treat unknown tier eligibility conservatively. Record excluded tags as ambiguous and label a qualifying pin **specific as published**. Undisclosed routes are outside discovery's visibility. Keep explicit base pins valid and report their provider-level scope where applicable. Evidence of the route requested and evidence of the variant served are separate fields.

## 3. Eligibility, modes, and routing policy

| Automatic mode | Eligibility | Ranking |
|---|---|---|
| **Cheapest** | p50 output throughput **greater than 20 tokens/s**. | Lowest estimated request cost. |
| **Balanced** | p50 output throughput **greater than 50 tokens/s**. A finite positive `balanced_min_tps` replaces 50 for this mode. For example, 100 requires **greater than 100 tokens/s**. | Lowest estimated request cost. Speed breaks equal-cost ties. |
| **Fastest** | Sufficient latency and throughput telemetry. | Lowest estimated finish time, using p50 time to first token plus expected output tokens divided by p50 output throughput. |

The Cheapest floor is fixed for a predictable low-cost preset. Balanced is the configurable speed floor. An endpoint whose speed equals the applicable threshold is ineligible. A missing speed cannot satisfy a throughput floor. Missing pricing or timing data cannot satisfy a ranking estimate that needs it. Keep missing values distinct from zero. Record candidate exclusions. If no candidate qualifies, report the threshold and the best observed speed among otherwise eligible candidates when available. The selector never relaxes a threshold automatically.

Estimate cost from published input and output prices and representative token counts. Default to 600 input and 200 output tokens, with caller overrides. Include a fixed request charge where known and apply a separately identified discount only once. Explicitly selected free models and zero-cost routes remain eligible if they meet the requirements. Break cost ties by finish time, time ties by cost, and remaining ties by slug. A known tie-break estimate precedes a missing one.

### Automatic policy composition

- Support the documented `only`, `ignore`, `quantizations`, `max_price`, `zdr`, `data_collection`, and `require_parameters` constraints. Validate the fields before discovery where possible. Apply locally verifiable filters, including the original allowlist and exclusions, published quantization, advertised capabilities, and price where comparable. Apply native price caps in their documented units and retain them on the request. OpenRouter remains authoritative for account, access, privacy, and request routing decisions.
- Apply explicit `quantizations` as an allowlist. A row with unknown quantization cannot establish a match to an explicit allowlist. Without an allowlist, keep FP4, INT4, and unknown precision eligible. Record the selected quantization if known. For tool selection, require advertised support for tools and preserve the tool preflight rule for `require_parameters: true`.
- Reject `sort` (including partitioned sort), `order`, `allow_fallbacks: true`, and unsupported policy fields in Automatic. Name the offending field and list the supported Automatic fields in the error. An explicit order belongs to the caller-controlled route. Existing Managed and Manual completion paths retain their native policy behaviour.
- Filter against the original `only` and `ignore` using routing-slug semantics. After filtering, replace an existing `only` with the chosen slug as its sole entry. Retain the applicable exclusions and server-enforced constraints. Set `order: [slug]` and `allow_fallbacks: false`. Use the same effective policy in the probe and subsequent requests. Every returned tool policy must pass `check_tool_request()`. Keep the caller's input mapping intact.

## 4. Confirmation, ZDR, and failures

**Match the intended request.** Use a short synthetic completion for ordinary requests. For a tool run, call `complete_with_tools()` with a synthetic prompt, one inert tool definition, and the exact effective routing policy. Include the known generation settings that affect routing. A parsed terminal response without an embedded error confirms route acceptance. Accept a `length` finish, including a reasoning-only reply. Record policy blocks and response-format rejections separately. Preserve response-format incidents in selection evidence even if a later candidate confirms successfully. Tool confirmation executes no tool. The later live round trip supplies tool-behaviour evidence.

**Shortlist under explicit ZDR.** When the caller requests `zdr: true`, try the ZDR catalogue with the normal inference credential. Apply the following rules:

- Match the catalogue's `(model_id, tag)` to the exact pair on each discovered endpoint record. Here `model_id` is the canonical model identifier (permaslug), which is available in `ProviderInfo.raw` when published. When an endpoint record omits it and the catalogue is usable, look up the exact requested model once in model metadata and use its published `canonical_slug` for the missing value. Preserve the requested model ID on all probes and completions. Never strip variant suffixes, guess the canonical ID, or match by provider tag alone.
- Probe eligible catalogue matches first. Fall back to remaining eligible candidates if the catalogue is unavailable or invalid, or if eligible matches are absent or exhausted. Record unresolved canonical identity separately from a verified absence of catalogue matches. Report any matched permaslug, the catalogue outcome, and the pool that supplied the pin.
- An optional catalogue HTTP failure, including 401, 403, 404 or 5xx, a socket timeout with time remaining, or an unparsable response makes the catalogue unavailable. Record its status or reason and continue while budget remains. An authentication rejection on required model discovery or an inference probe still stops selection. Caller cancellation and deadline expiry stop the selection at any stage. Every fallback probe keeps `zdr: true`. Optional metadata lookups and all probes share the same overall deadline.

Record Safety Retention as **yes**, **no**, or **unknown** from published metadata where available. Catalogue membership does not establish a route's Safety Retention status or an account's settings. Attribute rejection to Safety Retention only when structured evidence supports it. Document that callers whose accounts already enforce ZDR can request it explicitly to activate the shortlist.

**Deadline and cancellation.** Start one monotonic deadline per selection, default 30 seconds and configurable. Accept caller `cancel=` and use the watched transport for the selector's model discovery, catalogue lookup, and probes. Interrupt socket I/O and retry waits on caller cancellation or deadline expiry. Use selection-local deadline cancellation so expiry does not cancel unrelated operations sharing the caller's handle. Bound socket timeouts and retry waits by time remaining. Check cancellation and the deadline between stages and before dispatch, and return no successful selection after expiry. Document that operating-system DNS can delay return even after expiry. Report caller cancellation and deadline expiry distinctly and preserve `Cancelled.sent` when supplied.

**Completion attempts and errors.** The status rules in this paragraph apply to inference probes. Limit completion attempts to four, including retries, with an option for a lower cap. Reserve one attempt before each dispatch. Distinguish reservations, scheduled retries, and actual sends where observable. A scheduled retry alone is not a send. Stop on 401, 402, and 403 by default. For 403, use a structured reason when available, such as moderation, guardrail, budget, or access. Otherwise report **forbidden — reason unavailable**. Preserve existing public exception types. For ambiguous 404, report no route for that probe and allow the next candidate within budget. Stop if structured evidence establishes a model-wide or account-wide failure. Respect `Retry-After` on 429 within both budgets. Rotate on 429 only when structured evidence identifies an upstream provider limit. Use bounded, sanitized diagnostics and keep error-message wording out of control flow.

**Record the decision.** Include the model, mode, threshold, routing scope, requested slug, provider or variant reported, quantization, ranking inputs, catalogue outcome, and probe attempts. Capture the pin source and distinguish configuration from confirmed observations. Exclude credentials, real prompts and raw upstream diagnostics.

## 5. One-command verification

Provide one repository command:

```bash
python3 scripts/verify_provider_refactor.py
```

The developer exports a funded `OPENROUTER_API_KEY` before running the command. This is the only required user-supplied test setting. The bootstrap may use system Python to locate or create the repository's `.venv` and re-execute under its Python. Use that environment for bootstrap and offline checks. Install the checkout with its development extra if needed. Run release-qualifying live checks against the installed candidate wheel as described below. Exclude the API key from dependency-install and offline-test subprocesses. The runner chooses the models, pins, and routing settings and opts into its own live cases. Keep existing pytest entry points for targeted checks.

### Candidate wheel and build identity

The default command builds the candidate wheel with the existing release builder from the committed source. Pin the full source commit at the start and verify that the builder used it. Run live verification in a fresh isolated environment with that wheel installed. Use the committed verification harness and fixtures for release evidence. Run outside the checkout or otherwise isolate imports, and verify that modelSpell resolves to the installed wheel and that its installed version agrees with the committed source version and wheel metadata. An editable-install run is diagnostic only. Retain the tested wheel and report its path and SHA-256 digest.

Record the tested build as the full source commit, committed modelSpell version, and exact wheel SHA-256. Also record the payload-clean flag and verification-harness provenance. Check payload paths (`modelspell/`, `pyproject.toml`, `README.md`, and `LICENSE`) for staged, unstaged, and untracked changes at the start and end. Unrelated scratch files are outside that check. The archived commit is the source of truth for the wheel. Verify the source commit and wheel identity again before accepting the run as release evidence. A dirty payload, changed source commit, unverifiable wheel or checkout import makes the release gate pending even if the live cases pass. Keep live observations visible. A confirmed correctness failure still takes precedence with exit 1. A rerun from the eligible candidate can establish release evidence.

### Default live models

Use the ordered, bounded list below. Treat published prices and provider counts as a snapshot. Recheck actual endpoint capabilities, price caps, throughput, and pin specificity at run time. Public discovery cannot determine all restrictions on the supplied key.

| Order | OpenRouter model ID | Published providers | Published input / output USD per million tokens | Role |
|---|---|---:|---:|---|
| 1 | `z-ai/glm-5.3-flash` | 36 | 0.0399 / 0.0784 | Primary. Broad provider pool, low listed prices, and #4 on OpenRouter's 10 November daily ranking. |
| 2 | `openai/gpt-oss-120b` | 21 | 0.03 / 0.15 | Low-cost alternate with published tool support at 18 providers. |
| 3 | `minimax/minimax-m3.1-flash` | 10 | 0.1125 / 0.45 | Alternate model family. #10 on the same daily ranking. |
| 4 | `qwen/qwen3.8-27b` | 24 | 0.018 / 0.997 | Alternate with a wide pool and prior relevance to the owner's OpenRouter testing. |

The table below defines the complete default matrix. Use the first suitable model for each case. Move past a delisted model or a model unsuitable because of discovery, account eligibility, or availability. The named VT case also moves past a model whose behaviour prevents verification, as defined below. Preserve any correctness failure already observed. Code stores only the ordered model IDs from the dated shortlist. Discover and check current endpoint data at run time, and report the model and provider actually used.

Cap the default run at **64 inference POSTs**, including selector probes and retries. Keep prompts short. Allow up to **2,048 output tokens for VT turns** and 512 or less elsewhere. Preserve and report the intended runtime reasoning setting. A turn that exhausts its cap before calling a tool supplies truncation evidence. Default native price caps are USD 0.50/M input and USD 1.50/M output with zero fixed request charge. Apply comparable local checks and keep the caps on live requests. Carry the remaining run budget into each selector call. Print per-case POST counts and reported charges where available. Track reserved attempts separately where a send cannot be confirmed. Stop on a global credential or billing failure. Distinguish budget exhaustion from endpoint ineligibility.

After offline checks, run the named release case **first**. Reserve six VT POSTs for the current model’s two three-turn sequences before spending on its candidate probes. Charge dispatched attempts against the run budget. Release any unused reservation before considering the next model and reserve six again for that model. Start another model only if the remaining budget can cover at least one probe and a full VT. If necessary, lower that model's probe allowance to fit the remaining budget. Run the other paid cases after it, with V2 last. Reuse the compatible Balanced probe as the V2 baseline when its route and policy match.

### Coverage and reporting

Run the full offline suite before spending live requests. It covers ranking, price and speed boundaries, the Balanced override, missing stats, ambiguous tags, effective routing policy, credential redaction, timing and error handling, and the UI's use of the shared selector. Exercise policy and outage cases deterministically against test fixtures. Include a catalogue 403 followed by a successful ZDR probe, a catalogue 401 followed by a successful ZDR probe, and a terminal inference 401. Cover permaslug matching where the requested ID differs, including a dated permaslug and a public `:free` ID, missing endpoint identity resolved by exact model metadata, and an unresolved identity that cannot match on tag alone. Test named-case fallback after each model-behaviour sub-cause, preservation of correctness failures, and insufficient budget for another full VT attempt. Include independently specified ranking expectations, deadline and cancellation tests for slow discovery and inference, retry-budget accounting, and report redaction with a synthetic key.

| Live case | Required evidence |
|---|---|
| **OpenRouter-managed** | Ordinary completion succeeds with OpenRouter choosing the route. |
| **Manual / V1** | Reuse a slug confirmed by an Automatic ordinary-completion check with compatible requirements and policy. Pass it through the actual manual path and check provider-level attribution using the discovery snapshot rule below. Record the pin source as manual and its origin as automatic. Verify verbatim tag forwarding offline. Report multi-variant availability and any confirmed served variant separately. Exercise invalid-pin rejection before a completion in the offline suite. |
| **Automatic modes** | Exercise Cheapest, Balanced, and Fastest. Use the recorded discovery snapshot for a live consistency check. Check the applicable floor and ranking inputs, and confirm that the winner is the first accepted candidate in the recorded attempt order for the applicable pool. Independent expected rankings are tested offline. Exercise a non-default Balanced threshold live. Prefer 100 when a suitable route exists. The runner may select another explicit test threshold from discovered speeds to exercise the override, and reports the value used. Exact boundaries and no silent relaxation are checked offline. |
| **V2** | Run the OpenRouter settings matrix after a compatible minimal baseline. Reuse a selector probe only when its route and policy match. Stop the matrix if the baseline is blocked. Report each setting as accepted, specifically refused, or unrelated and unverified. A successful response establishes request acceptance. The setting's effect remains unverified. |
| **Automatic ZDR tool round trip / VT** | Request Balanced and `zdr: true`. Record catalogue access, the pin, and the tool-shaped probe. On the selected configuration, exercise streaming and non-streaming VT, including a real synthetic tool call, a clean continuation that uses the returned tool result, and acceptance of a final request with the narrower tool set. See the named-case outcome rules below. Classify provider attribution on every turn using the discovery snapshot rule below, including the narrower-tool request. Reuse the pin and effective policy throughout. |

Run V1, V2, and VT through the shared selector. An independently invoked pytest live check honours `MODELSPELL_LIVE_OPENROUTER_PROVIDER` as a strict override. With a model and key and without that override, it resolves automatically. Share V1 and V2's automatic selection where requirements match. VT may need a different pin because tools change the eligibility rules. Share the VT pin across streaming and non-streaming when it satisfies both. Keep the existing unpinned discovery, streaming, and owner checks available independently.

### Provider attribution

Use the expected `provider_name` from the recorded discovery row for the exact pinned tag. For a provider-scope pin, use a single provider name only if the matching rows establish it unambiguously. Compare names after trimming surrounding whitespace and ignoring case. Never derive the expected display name from a routing slug.

- **Confirmed.** The reported name matches the expected discovery name. This confirms provider-level attribution. Report served-variant attribution separately when independently available.
- **Conflicting.** The reported name unambiguously identifies a different provider in the same snapshot, and the expected provider is also unambiguous. Treat this as a demonstrated pinned-route violation and fail verification.
- **Unverified.** The reported name is missing, unrecognised, or ambiguous, or discovery cannot establish the expected name. Record the reason. Apply the same treatment as for missing attribution. A successful tool exchange can establish tool behaviour, with provider attribution still unverified.

Add offline fixtures for `google-vertex/global` reported as `Google` and `mancer/fp8` reported as `Mancer 2`, a recognised different provider, and missing, unrecognised and ambiguous names.

### Report and gate outcomes

Print per-case **PASS**, **FAIL**, or **BLOCKED** and write a sanitized machine-readable report. Check the serialized report for the literal API key in process before writing it. Refuse to publish a report containing the key and report a correctness failure without printing the secret. Report the overall verification result and print the named live `release_gate` result last. Preserve historical captures and record each new run separately.

| Exit code | Meaning |
|---:|---|
| 0 | All required offline and live cases passed. |
| 1 | A correctness check failed. This takes precedence over other outcomes. |
| 2 | The named live gate passed and other cases are blocked. |
| 3 | The named live gate is pending, with no correctness failure established. |

Use `pass`, `fail`, or `pending` for the named gate. Report each attempted model and pin where available, the observed stream mode and turn, the outcome and reason, and attempts used.

- **Pass.** One automatically selected model and effective policy completes both streamed and non-streamed VT, with an eligible candidate wheel identity and no confirmed failure or unresolved response-format incident from the named case. Partial observations from different models or policies cannot be combined.
- **Pending.** Record **no eligible route**, **upstream outage**, **account restriction**, **budget exhausted**, or **model behaviour — tool round trip not observed**, as supported by the evidence. For model behaviour, record the sub-cause, including no tool call, truncation, another diagnostic finish, or continuation that did not use the tool result. Mark that model unsuitable for the named case and try the next permitted model within the budget. If none passes and no correctness failure was established, leave the gate pending. Preserve every earlier observation if a later model passes. An unresolved response-format incident keeps the named gate pending even when a later model completes both VT checks.
- **Fail.** A demonstrated contract violation, such as rejecting a valid same-policy replay, unambiguous conflicting provider attribution under the snapshot rule, accepting a demonstrably malformed response as valid, or detecting a secret in the report, fails verification with exit 1. Record the source of the violation where known. A correctness failure cannot be cleared by moving to another model.

Catalogue unavailability can be recorded independently when a bounded fallback lets the named case pass. The script reports the named live gate and the overall verification run. Wheel smoke and downstream retest are additional publication checks.

### Response-format triage

A `ResponseFormatError` shows that the response was rejected. It does not establish whether the response violated the upstream contract or modelSpell mishandled a valid response. Record **response rejected — triage required** as a pending reason. Apply this to named-case confirmation probes as well as VT turns, including rejections preserved in selector evidence. Continue with other candidates or models within the existing budget. A later success leaves the named gate pending with exit 3 until every named-case rejection is triaged. A confirmed correctness failure still takes precedence with exit 1. Other live cases report unresolved rejections as blocked and retain the same diagnostic evidence.

Give each incident an ID scoped to the run ID and tested build (source commit, committed version, and wheel SHA-256). Record model, route, provider and requested pin where known, stream mode, probe or turn, a sanitized parser diagnostic, the bounded schema key paths and value types around the failure when available, and a recognized `finish_reason` when available. Use fixed schema paths or redact untrusted keys. Exclude actual field values, prompts, response bodies, raw exception text that can echo content, and credentials. Mark unavailable structural details explicitly.

Provide an optional report-only triage operation in the same script. It takes the original report, incident ID, verdict (`upstream` or `modelspell`), and a short evidence-based reason. Validate the incident and verdict against the original report's run ID and complete tested-build identity. Reject cross-run or cross-build reuse. Preserve the original and record the verdict and updated results separately. Report-only triage cannot turn diagnostic or ineligible build evidence into a release pass. Apply recorded verdicts cumulatively for that same run and build so multiple incidents can be resolved independently. An `upstream` verdict clears only that incident's hold. A gate pass still requires a complete streamed and non-streamed VT on one configuration in that run. A `modelspell` verdict establishes a defect and exits 1. Fix it and verify a new candidate. Require no triage inputs on the default key-only run.

Test that a later VT pass leaves an earlier rejection pending, that an upstream verdict only clears the corresponding incident, and that a modelSpell verdict fails the run. Cover a verdict without a complete VT, mismatched incident identity or build, and content-free, secret-free diagnostics. Add offline release-provenance tests for dirty payload paths, changed source commit, checkout import, wheel-digest mismatch, and rejection of a triage verdict from a different commit or wheel. Verify that a successful live result with ineligible provenance yields pending with exit 3 unless a correctness failure takes precedence.

## 6. Credential fix

Fix `Call.key` independently within 0.4.0 with `field(repr=False)`. Add a synthetic-key regression check and retain the existing redaction coverage. Rotate the exposed key before owner live verification. The fix covers the identified repr exposure.

## 7. Release acceptance

1. Run the one-command verification, pass the offline suite and installed-wheel smoke, and resolve confirmed modelSpell defects. Update the spec, README, DEVEL.md, release notes and live evidence. Update the DEVEL.md release checklist to explain the named live gate, exit codes, wheel smoke, and downstream retest. List blocked and failed cases with diagnoses. A blocked live case outside the named case can yield exit code 2 and leaves publication eligibility to the named gate and the other publication checks.
2. **Named release case — modelSpell VT on an automatically selected configuration using Balanced and ZDR.** On the final candidate, use the rotated owner credential. Require a successful Automatic Balanced selection with p50 throughput strictly above 50 tokens/s, `zdr: true`, and a successful streamed and non-streamed modelSpell VT on the resulting pin. Resolve every response-format incident from the named case through recorded triage before clearing the gate. If the default run is pending because no eligible route exists or no model completed VT, support optional release-case model and price-cap overrides in the same script, for example `--release-model`, `--release-max-input-price`, and `--release-max-output-price`. Validate price overrides as finite nonnegative caps in USD per million input or output tokens, using the same units as the defaults. Keep the fixed request charge cap at zero and preserve the request and token budgets. Record every override and the effective configuration. The default remains key-only. A lower Balanced floor is useful for diagnosis. Using a lower floor as release evidence requires an explicit change to this acceptance case.
3. **AgentWiz retest and admission.** Retest AgentWiz against the same candidate wheel identified by the qualifying gate report before publication. The script supplies modelSpell VT evidence for the selected configuration. The downstream retest establishes admission evidence under AgentWiz's actual runtime policy. Retain the tested model, base URL, transport, reasoning setting, pin scope, provider or served variant where known, quantization where known, and full effective policy. A capped run can support the retest when model, route, reasoning, endpoint and relevant constraints match. An ambiguous pin or changed routing constraint calls for VT under the runtime policy. A manual and an automatic pin supply equivalent VT evidence when the effective configuration and assertions match. The named gate requires automatic selection. Within any one conversation, preserve exact replay identity, including the whole effective provider policy.

4. **Publication identity.** Update the DEVEL.md checklist and release workflow to verify the named-gate report before publication. The report's source commit must match the committed source used to build the release candidate. If merging introduces a different commit with identical payload paths, publish the verified candidate built from the recorded source commit. If the merge changes those paths, verify a new candidate before publishing. A build taken from the new merge commit requires its own qualifying report. Publish the exact tested wheel bytes. Prefer passing the verified candidate to publication. If the workflow rebuilds, compare SHA-256 before publishing and stop on a mismatch. A changed candidate requires fresh gate evidence, triage as applicable, and downstream retest. Confirm that the published asset matches the recorded digest.

## References

- [modelSpell devel spec at e3caf57](https://github.com/alexchwong/modelSpell/blob/e3caf57/docs/modelspell_v1_spec_v9.md)
- [OpenRouter provider routing, endpoint tags, and price caps](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter model metadata and canonical slugs](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)
- [OpenRouter per-model endpoint discovery](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints)
- [OpenRouter ZDR policy and Safety Retention](https://openrouter.ai/docs/guides/features/zdr)
- [OpenRouter ZDR catalogue](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr)
- [OpenRouter error handling](https://openrouter.ai/docs/api/reference/errors-and-debugging)
- [OpenRouter usage ranking, through 10 November 2026 at lookup](https://openrouter.ai/rankings?stream=top)
- [GLM 5.3 Flash](https://openrouter.ai/z-ai/glm-5.3-flash)
- [GPT-OSS 120B](https://openrouter.ai/openai/gpt-oss-120b)
- [MiniMax M3.1 Flash](https://openrouter.ai/minimax/minimax-m3.1-flash)
- [Qwen3.8 27B](https://openrouter.ai/qwen/qwen3.8-27B)


## Implementation interface

The public names are `choose_provider`, `ProviderSelection` and `ProviderSelectionError`.
`mode` selects `managed`, `manual` or `automatic`. `preset` selects the automatic
ranking. Pass the result's `policy` to the completion or tool API. The developer UI
selects once per request or compatible owner-check run. The release workflow consumes
the verified wheel and `provider-gate.json` from a draft release.

Paid verification of the final committed candidate and the same-wheel AgentWiz retest
remain pending. See the release checklist in DEVEL.md.
