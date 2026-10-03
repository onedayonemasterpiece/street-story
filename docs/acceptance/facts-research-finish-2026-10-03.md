# Facts research finish — 2026-10-03

Overall: `IMPLEMENTED_BUT_EXTERNAL_CHECK_BLOCKED` pending delivered-build and
physical Android checks. A real semantic FAIL below is a FAIL, not a provider
block or product success. This report is updated with final delivery receipts.

Existing worktree: `/home/dev/projects/street-story-poi-runtime-clean`, branch
`chatgpt/street-story-facts-review-finalization-20261003`, inherited from #124
(`74c8f71`). No new worktree; inherited WIP preserved in retained evidence.
Protected requirements unchanged; models continue to own semantic decisions.

## Product result

Live can read frozen full-document chunks when text research/detector is
unavailable, save atomic claims and evidence, review the exact assertion/evidence
revisions including zero conflicts, and expose eligible facts for selection and
drafting. Literal paragraph references avoid rewriting quotes. Every accepted
batch persists its payload in the ledger transaction; replay is idempotent.
Resume uses the same source version and next batch index. Terminal chunks without
payload cannot count as complete. Complete coverage is rejected with pending
chunks; explicit partial review can still admit supported findings.

Final review scan, conflict decisions, eligibility and run state commit together.
Every conflict is aggregated; another successful pair cannot clear an unresolved
or losing assertion. Quarantine survives new evidence. New revisions require new
review. Commit guards reject cancellation, changed POI and concurrent story
revision, including changes during the final semantic await. `edit_text` now gates
selected eligibility before saving a draft. Android retains its existing input
focus gate and displays “Проверяю факты…” throughout review.

## Evidence and scope

Retained task evidence:
`/home/dev/artifacts/street-story/20261003T210444Z-facts-research-finish`.
No credentials copied; isolated fixture stores do not clear user cache.

Local tests: full backend **462 passed** in **14.14 s** before the final routing correction; Ruff passed. Final counts are recorded below at delivery.
`backend/tests/test_facts_research_finish.py` verifies the full controlled fallback
through evidence/review/selection/draft, no-claim receipt, stable replay, partial
coverage, invalid quote rejection, rollback of late review failure, and three
changes during the last reconciliation await. Existing ledger tests cover the
four A3 counterexamples, conflict order, stale scans and stale arbitration.
Existing chunk integration verifies timeout after batch 0, resume at batch 1,
frozen source versions and no repeat extraction of a terminal chunk.

Corpus and reproducible harness: `backend/tools/fixtures/facts-research/README.md`
and `backend/tools/facts_research_acceptance.py`. Royal Gates and Brandenburg
excerpts have pinned Wikipedia revisions and attribution. Gold was checked
against the architecture/history passages; left/centre/right is excluded because
viewpoint is undefined. Broad input contains no expected names. Gold aliases are
only evaluator code, never production fact extraction.

Real Live route: shared `ai_resource_control.run_guarded`, `gemini-3.8-live`.
Discovery, helper outage and snapshot HTTP are controlled; extraction, identity
reconciliation decisions and review come from the real model and production
adapter. This does not demonstrate real Internet retrieval or physical audio.

Initial cold broad run: **FAIL**, **116.14 s**, 5 eligible claims, 1/2 chunks
processed, 0/3 figure gold. Receipt: `broad/acceptance.json`. The model tried
invalid rewritten quotes, eventually saved the first chunk, then reviewed too
early. Completed coverage was already reported false; the harness exited nonzero.
Correction: addressable literal passages and rejecting claimed complete coverage
while chunks remain. Later bounded runs are recorded below.

Intermediate targeted run: BLOCKED_PROVIDER at tool-response budget after
181.47 s, three atomic figure assertions saved but unreviewed,
RESOURCE_TOKEN_BUDGET requested 40,119 units. Initial targeted run requested
35,418 units. Initial holdout: FAIL after 180.26 s: saved claims, then invented
review digests were rejected. Another holdout followed six discovery searches
and saved nothing (180.49 s, FAIL). These are distinct observed failures.
Corrections: exact digest/evidence IDs in save receipts, explicit read/retry
guidance, research by coverage instead of a mandatory 4–6-search quota, compact
manifest/checkpoint, and applying the model projection on the actual chunk read
tool route (read tools return before the general mutation projection).

## DoD

| ID | Status | Evidence / exact remainder |
|---|---|---|
| D01 | IMPLEMENTED_AND_VERIFIED locally | Controlled helper outage; Live contract saves, zero-conflict reviews and admits 3 claims, then selection/draft. Real semantic run separately reported. |
| D02 | IMPLEMENTED_AND_VERIFIED | Ledger counterexamples and revision-scoped scans/decisions; quarantine and conflict-order tests. |
| D03 | IMPLEMENTED_AND_VERIFIED locally | Payload checkpoint, continuation/replay, frozen-document resume, terminal no-claims receipt. Actual process death/phone reconnect remains external acceptance. |
| D04 | IMPLEMENTED_AND_VERIFIED | Parametrized cancellation, identity and concurrent revision change inside the last awaited reconciliation reject late batch and preserve the first observation. |
| D05 | NOT_IMPLEMENTED as accepted outcome | Initial real broad failed; final broad/targeted/holdout receipts pending. No mock or gold name match alone is called semantic product success. Warm-cache real-model reuse remains unverified. |
| D06 | IMPLEMENTED_BUT_EXTERNAL_CHECK_BLOCKED | Backend selection/draft gates tested. Android focus/PCM tests and delivered build checks pending. Physical microphone/cancel/resume requires owner's phone. |
| D07 | IMPLEMENTED_BUT_EXTERNAL_CHECK_BLOCKED | Backend/APK delivery receipts pending below. Before delivery disk had 30 GiB free; current and rollback were preserved. |
| D08 | IMPLEMENTED_AND_VERIFIED for reporting | This report separates controlled/real/phone evidence, PASS/FAIL/provider block, measured times and residual work. |

## Delivery receipts

Pending CI, exact backend revision/health, canonical signed APK version and link.
No unrelated social or image-generation runs, and no real channel publication.


## Budget/review unblock — 2026-10-04 (UTC+2)

The prior RESOURCE_TOKEN_BUDGET receipts are reclassified as **local resource
admission**, not Gemini outages. SDK metadata/path read from the running release:
`ai-resource-control 0.1.11`; no private SDK source copied into this repository.
Authority capabilities confirm `ai_resource_leases_v1` and
`grant_spend_deadline_plus_60s`; six currently enabled Live policies have 60,000
admission units, 90-second leases, verified_at 2026-09-24. These are conservative
UTF-8 JSON envelope units, not provider tokens or billing. Historical policy
changes/installed SQL body are not recorded in the receipt and remain unverified.
No numeric policy, estimator, key binding or quota scope was changed.

Saved broad receipt `broad-1791065154220252436/acceptance.json` is unchanged.
Read-only authority grant rows and a bounded event table are retained beside it
as `broad-grants-readonly.json` and `broad-budget-timeline.json`.

| UTC event | Durable/read state | Admission / model delivery |
|---|---|---|
| 22:05:56 search | One source discovered | 1,041-unit response granted |
| 22:05:58 chunk read | Frozen source, first core | 14,429 granted |
| 22:06:03 save | First batch persisted | 1,326 granted |
| 22:06:04 continuation read | Saved first batch remains | 15,610 denied; granted 22:07:27 after wait |
| 22:07:28–29 evidence reads | Assertion evidence exists | 6,673 and 7,135 both granted |
| 22:07:31–37 review attempts | Exact-version / pending-core checks reject premature review | Contract failures, not external provider failures; extra chunk/read/save cost |
| 22:07:36 save | Two payload batches, seven unreviewed claims; only 1/2 cores complete | Save acknowledged |
| 22:07:39–22:08:53 evidence read | Prepared response remains unsent; facts remain durable | 6,673 denied repeatedly; harness ends before next allowance |

At the last denial the same lease's grants 7–17 alone hold 53,667 units until
at least 22:08:56.976; 60,000 leaves 6,333, **340 below the requested 6,673**.
The event reports requested=estimated (no carry). Other leases cannot increase
this allowance; their historical aggregate is not independently reconstructed.
This accounts for the denial under the currently confirmed policy. The first
large denial recovered and is not the terminal blocker. Provider send absence
follows from the installed guard ordering (before_send precedes ws.send); receipt
does not contain raw provider payload or a per-send acknowledgement.

Changes: frozen durable packets map local fact/evidence numbers to exact story,
owner, run, identity and assertion/evidence revisions. Explicit support/negative,
role and equivalence verdicts plus complete cross-page relation review are required.
Negative decisions withhold assertions. Packet read alone grants nothing.
Decisions stage in bounded operations; >240 assertions can finish through multiple
operations without deleting inventory. Existing precise final commit remains atomic.
Document and inventory pages use a 5,500-unit final-envelope ceiling, reserving
room below the observed 6,333 remaining allowance. Document tail cannot become
processed after reading one page. The authority still guards actual batches;
fully occupied windows use the shared bounded wait, without redoing tools.

Offline: **472 backend tests passed**, including the actual installed SDK estimator
and Lease on production execute_tool replies; deny/refill causes exactly one send,
oversize and lease expiry fail closed. Controlled packet negatives, foreign/stale
references, replay, cross-page gate, and 241-assertion completion pass. Ruff passes.
SDK tests use a private artifact-path link to the installed package; CI without
that optional private dependency separately reports the SDK test as skipped.

Acceptance now walks every facts/evidence page, separates BLOCKED_RESOURCE,
BLOCKED_AUTHORITY, BLOCKED_PROVIDER, FAIL_CONTRACT and FAIL_SEMANTIC, and records
ordinary vs guided prompts. Substring matches cannot grant PASS: three specific
relations require manual semantic assessment bound to exact fact text and its own
evidence IDs. The audit's negation/wrong-ruler/wrong-role counterexample fails.
Old probes also used an incorrect controlled discovery-provider label; new frozen
corpus probes use the production discovery fallback branch. Internet retrieval,
fixture identity/helper outage and physical microphone remain distinct scopes.

U1: measured local boundary; historical policy/other-lease details above are explicit.
U2/U3: controlled verification passed. U4 ordinary real-model targeted, broad and
holdout plus U5 final backend/canonical APK delivery are pending this iteration.
