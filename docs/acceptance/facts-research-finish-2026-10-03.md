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
