# Facts research finish — 2026-10-03

Overall: `FAIL_SEMANTIC` for broad/holdout acceptance. Targeted ordinary real-model
research passed its manually assessed frozen-corpus gold gate. This is not full
product acceptance. PR #126 is unmerged; backend/APK delivery was not performed
because content review exposed false-positive eligibility despite green CI.

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

Local tests at frozen functional source `bec193e55fbac2a3698558d28732e34e8f2e63a8`:
**475 passed**, **34.64 s**, two dependency deprecation warnings; Ruff passed.
The installed private SDK is exercised through a private artifact-path link.
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

Intermediate targeted run: BLOCKED_RESOURCE at tool-response budget after
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
| D05 | FAIL_SEMANTIC overall | Ordinary targeted passed; broad and holdout failed atomic gold criteria. Broad also admitted assertions unsupported by their own spans. Warm-cache real-model reuse remains unverified. |
| D06 | IMPLEMENTED_BUT_EXTERNAL_CHECK_BLOCKED | Backend selection/draft gates tested. Android focus/PCM tests and delivered build checks pending. Physical microphone/cancel/resume requires owner's phone. |
| D07 | NOT_DELIVERED | PR #126 remains open after semantic failure; backend stays at #124, no new canonical owner APK delivered. Disk space is not the blocker. |
| D08 | IMPLEMENTED_AND_VERIFIED for reporting | This report separates controlled/real/phone evidence, PASS/FAIL/provider block, measured times and residual work. |

## Delivery receipts

Frozen candidate CI: backend, Android checks and emulator passed at `bec193e`.
Release job is skipped on PR runs; a debug APK is not canonical owner delivery.
CI: https://github.com/onedayonemasterpiece/street-story/actions/runs/37163185994
and https://github.com/onedayonemasterpiece/street-story/actions/runs/37163185999.
Last runtime readback before the retrospective: HTTP 200, backend
`74c8f71da901a2c1fd9339eb79e0500efcfc0110`; no installation/restart was attempted.
No new canonical signed APK, physical microphone acceptance, real Internet
retrieval acceptance, social publishing or image-generation run is claimed.

Retained production backup `production-before-facts-finish.sqlite3` contains
private story data (mode 0600); it is sensitive evidence, not a distributable
fixture. Credentials were not copied. Production data and rollback were preserved.


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

Offline: **475 backend tests passed**, including the actual installed SDK estimator
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
U2/U3: controlled verification passed; broad results below show that a valid
packet contract alone does not guarantee accurate model support verdicts.
U4: targeted PASS, broad/holdout FAIL_SEMANTIC; no blanket acceptance.
U5: this single report is current; backend/canonical APK delivery is not complete.

## Retrospective and freeze — 2026-10-04

The review covers this task's preserved receipts, source history including #123
and #124, and the earlier owner-review report. There is no matched historical
phone/fact-research baseline proving that an earlier release met all these gates.
Earlier guided prompts and the old discovery-fixture label differ from current
ordinary prompts; counts and times are not an isolated causal benchmark.

Checkpoint comparison uses three exact runs, not the newest changing filename.
Paths below are relative to the retained evidence directory. All three used
`gemini-3.8-live`, cold isolated stores with zero baseline facts, the pinned Royal
Gates excerpt (9,651 frozen characters), controlled HTTP/discovery and unavailable
research/reconciliation helpers. Earlier discovery fixture provider labeling and
guided input differed; this is descriptive comparison, not an isolated causal test.

| Outcome / exact artifact, code and evaluator | Request / corpus-cache / helpers | Supported atomic claims / three figure relations | Own evidence / actual admission | Completion / time / tool repetition |
|---|---|---|---|---|
| Early most useful: `targeted-1791065439571442723/acceptance.json`; code SHA absent in receipt; legacy evaluator lacks semantic assessment; manually reread with current criteria in `retrospective-assessment.json` | Guided; pinned Royal/cold; helpers unavailable | 3 atomic figure claims; all 3 depiction identities supported, relief classification scope below | Each has 1 own span, same source/version; durable status remains **unreviewed**, 0 eligible; no retrospective relabeling | 1/2 cores, not completed; 180.35 s; 9 tool results: search1/chunk3/save1/facts1/evidence1/finalize2; exact-args repeats unavailable because args were not captured |
| Best current: `targeted-1791071594320565279/acceptance.json` plus `gold-assessment.json` and `acceptance-assessed.json`; clean code/evaluator `bec193e55fbac2a3698558d28732e34e8f2e63a8` | Ordinary; same pinned Royal/cold; helpers unavailable | 3 supported atomic claims; 3 separate eligible figure IDs; manual **PASS** | 2 own spans/observations per fact, repeated same source/version, not independent corroboration; 3 eligible, owner-selected | 2/2 cores, completed; 119.59 s; 19 calls: search1/chunk7/save3/packet6/finalize2; 9 repeated name+args (chunk4/packet5), including legitimate default resume/packet access |
| Last completed: `broad-1791071858579050101/acceptance.json` plus `gold-assessment.json` and `acceptance-assessed.json`; evaluator `bec193e`, functional code plus removed WIP safeguard described below | Ordinary broad; same pinned Royal/cold; helpers unavailable | Conservative manual count **6/18** wholly supported atomic claims; all 3 figure relations share 1 compound assertion, not 3 selectable IDs; **FAIL_SEMANTIC** | 18 durable eligible flags remain unchanged; all spans from one source/version. Own evidence does not support 3 identified assertions below; other bundled claims excluded from atomic count | 2/2 cores, completed; 140.03 s; 32 calls: search1/chunk9/save9/packet11/finalize2; 14 repeated name+args (chunk4/save3/packet7), not automatically 14 wasted operations |

Atomic count assesses the whole selectable assertion: attributes of one event
may stay together; independently selectable people, roles, properties or events
must split. The six accepted broad IDs are recorded in the existing JSON
assessment. This manual measure neither alters persisted eligibility nor claims
an independent multi-source confirmation.

The earlier initial broad's five eligible records also fail the stronger own-span
criterion: its shop and anniversary spans omit the asserted year, the official
document span does not itself establish protection, and the restoration span
omits the asserted July 2005 endpoint. Those weaknesses predate the latest
packet changes. The agent's adapter regression (~2.8 s bootstrap failure after
`9cc28e`) and premature continuation (`365119c`, 5.99 s, zero facts) remain real;
their repairs are not evidence that every subsequent edit improved the outcome.

Holdout remains separately assessed: exact run
`holdout-1791071858577683009`, 94.09 s, 7 eligible, completed 1/1 core,
FAIL_SEMANTIC because both portrait relations share one assertion. Its goal and
query incorrectly say Berlin despite the Kaliningrad fixture and source.

### Candidate historical baseline around `97b3317`

Read the exact `97b33173deb551231736bf15bd5577691afea215` source and
[PR #96](https://github.com/onedayonemasterpiece/street-story/pull/96): configured
research model extraction/persistence occurs inside search; a second Live save is
only fallback. This preserves the useful earlier reduction in service hops.
[PR #95](https://github.com/onedayonemasterpiece/street-story/pull/95) already
compacted model-facing replies without deleting durable evidence. These are
different conditions from forced helper outage, and not a reason to reset the
current ledger/review/data protections.

Exact baseline CI passed ([backend](https://github.com/onedayonemasterpiece/street-story/actions/runs/37073378462),
[Android](https://github.com/onedayonemasterpiece/street-story/actions/runs/37073378555)),
but matching semantic receipt/texts at `97b3317` were not found in the managed
Street Story artifacts, legacy canary locations or PR discussion inspected.
Corpus/cache/helper availability, atomic count, three figures, exact admission,
completion, elapsed time and repeated calls for that baseline are **missing**.
The bounded journal search did not cover the entire requested historical
interval; it is not evidence that no older result exists. No old production DB
was started and no checkout was switched.

Two earlier primary canaries were found and retained byte-for-byte with hashes:
`baseline-pre97-search.json` at `dceb9eee` reports PASS/8 supported counts, but
contains no fact texts or own spans to reevaluate. `baseline-pre97-product.json`
at `490982b2` records 8 Zakhaym Gates entries (reported supported_count=7): all
bundle independent propositions, truncated snippets or navigation/title text;
7 have URL/title sources and one has none. Under the same whole-assertion
criterion, **0 complete independently selectable atomic assertions** can be
accepted as stored. Literal own passage evidence, the Royal Gates gold, timing,
cache and final version-scoped review are absent. Their original PASS labels
are preserved; no current eligibility is projected backwards onto them.

The historical canary evaluator primarily checked search success and continued
Live output. Its PASS did not mean today's atomicity/evidence/review gates passed.
This establishes weaker old acceptance, not that `97b3317` itself was worse or
better. No paid historical reruns are needed to reach that bounded conclusion.

The targeted PASS has three separate affirmative assertions for Frederick I,
Duke Albrecht and Otakar II, each bound to its own literal evidence IDs and text
digest. The evaluated relation is depiction/person identity; the corpus mixes
relief terminology, so independent classification as bas-relief is not certified.
Online `REVIEW_REQUIRED` receipts are unchanged; offline manual assessment creates
separate `acceptance-assessed.json` files. Broad and holdout assessments correctly
fail the required three-distinct-assertion gate even though their shared spans
mention all three gold relations.

Broad content review found failures beyond atomicity:

- `claim_124a21b1e9c81e3e5e3c` asserts a 1976 shop opening, but both own spans only
  say “В том же году”; neither resolves that year. Full document context does
  not establish support from these isolated own spans.
- `claim_1eb0fc3fc0c72e2350a3` includes Ministry confirmation of protected status;
  its own spans only describe a request to remove protection.
- `claim_a403c081b00666027228` states confidently that all eight towers were
  rebuilt from turrets in the nineteenth century. Its span describes four
  lower-tier turrets and qualifies the later change as probable.

Holdout facts and source are about Kaliningrad, but the model's run goal and
search query say Berlin. That is observed identity drift; the frozen HTTP fixture
still supplies the Kaliningrad source. Correct saved facts therefore do not prove
correct ordinary Internet discovery for the intended object.

Broad/holdout processes loaded `bec193e` plus a tiny uncommitted `ready_to_save`
envelope safeguard. That exact hunk was removed after the freeze; these receipts
are explicitly not exact-clean-SHA acceptance proof. The targeted PASS and final
475-test run do use the clean frozen functional source. No functional edits or
new paid reruns followed this retrospective.

Transport, persistence and completion improved in the measured targeted case,
but semantic acceptance remains failing. The two introduced regressions were
real, and successive fixes did not monotonically improve results. No rollback to
an older version is justified as a proven complete solution by these receipts.
The first incorrect transition in the last broad run is **semantic review**:
the model selected `supported` for the three counterexamples despite complete
literal packet slices containing the missing/qualified information. Their final
commit succeeded; this is not a new evidence-envelope or budget failure.
The retained application tool trace shows the later valid packet-ref decisions;
it is not a raw per-send provider acknowledgement for every read response.

An offline paired final-commit replay used two copies of that exact completed
fixture DB, synthetic reopened review state, fresh packets and the production
adapter. No original DB was changed and no model/network call occurred. With
the captured all-positive choices, all 18 assertions become eligible; changing
only the three manually judged counterexample verdicts to `not_supported` gives
**15 eligible / 3 withheld**, with a completed run and immutable original fixture
hash. This proves the existing final commit honors negative semantic choices;
it does not prove that the real model will choose them or repair compound claims.
The initial synthetic setup attempted the invalid run-state label `running`;
the fixture-copy transaction rejected it. The corrected replay uses `verifying`;
that setup error is not a product acceptance failure or a paid retry.
Evidence: `replay_saved_review.py` and `offline-review-replay.json` in the same
retained task directory; the two replay copies are isolated synthetic evidence.

Minimal next action is a bounded comparison of the **same saved review packet**,
same Live model/budget/cache/helper conditions, changing only the review
instruction's explicit requirement to preserve qualifiers, reject unsupported
attributes and withhold compound assertions. Do not redo search/extraction or
introduce another continuation. Only a measured improvement on the existing
counterexamples justifies that source change; then retain the successful
targeted result as the regression gate and repair the missing separate figure
assertions through model-owned extraction. Normal helper-assisted acceptance
remains a separate scope. No semantic regexes, quota changes, whole-project
rollback, merge/deploy or full-product success claim follows from this checkpoint.

Evidence: `retrospective.json`, `retrospective-assessment.json`, the three latest
case receipts and `gold-assessment.json`/`acceptance-assessed.json`, and
`full-frozen-retrospective.log` in the retained task directory above.
