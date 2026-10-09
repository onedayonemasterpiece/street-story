# Product recovery validation, 2026-10-09

Status: final live acceptance pending; no accepted product release. The previous
visual-model RPD denial is historical evidence, not a permanent product blocker.
The deployed backend
remains `db0c0d08024441536a2e59f5c3c433b12c7f4189`. A prepared branch or successful
offline check does not establish deployment or user-visible success.

At 08:44 UTC / 10:44 Kaliningrad, read-only controller evidence showed 20 Gemini
3.8 reservations per configured key in the October 8 Pacific bucket, no entries
in the October 9 bucket, and no active provider cooldown. Street Story incorrectly
extended RPD waiting to UTC midnight instead of the controller's declared Pacific
day. The correction respects an explicit controller retry duration; when it is
absent, only an explicitly declared Pacific bucket supplies its reset boundary.
Unknown bucket strategies retain ordinary bounded retry, without inventing UTC
exhaustion. Admission and unknown-send protections remain mandatory. The focused
quota/send-boundary/reliability suite passed 69 checks, including DST and admission
after the controller reset. Fresh cold102 on `86d5edc` subsequently made one
Gemini3.8 SDK call; its unknown outcome and terminal failure are recorded below.
Metadata alone is not inference or recognition success.

## Current secondary spatial route and actual cold102 failure

Cold102 on `86d5edc6c96b7ceb54d197976e585f73999fb523` was admitted at
08:47:58 UTC. The single Gemini3.8 call was cancelled 17.916 seconds into the
SDK (20.216 seconds for the attempt), by the generic 20-second call timeout.
Its send outcome and usage are UNKNOWN. The existing worker ended naturally
at 180.688 seconds with no confirmed physical object and no eligible facts.
This is a timeout failure after admission, rather than a current RPD blocker.
That addressed operation must not be resent or replaced with another model.

The correction gives SOURCE/MAP the existing 60-second total attempt budget;
ordinary calls keep their shorter timeout. Registered Google routes can rotate
after a definitive unsent refusal. The configured preferred Google route stays
first; the owner's explicitly authorized `gpt-6-luna` is an additional reserve
before the next registered Google route. The same geometry proof contract is
used on every route. Closed responses and UNKNOWN sends do not trigger model
rotation.

Luna reuses Native vision admission, account permission, inline image delivery,
durable receipts and original turn readback. SOURCE and MAP hashes are checked
before send; MAP pixels remain unchanged. Original readback restores the frozen
schema and MAP namespace, including after the available quota drops. The exposed
owned text/schema envelope for the saved photo102 construction is 59,543 UTF-8
bytes, within the existing 65,536-byte role limit. This is a serialization check,
not an inference or an accuracy result.

Affected checks passed **177 tests in 143.36 seconds**, followed by **54 tests in
10.42 seconds** after the final image/scope guards. They include exact SOURCE/MAP
transport, quota refusal, original-turn readback without a fresh call, strict
photo/generation/control binding and the joint timeout budget. No actual Luna
spatial recognition has been demonstrated by these tests. The next live canary
uses independent cold104 on the new frozen commit, preserving cold102's UNKNOWN
receipt rather than creating a duplicate. The complete same-version product
gate and deployment remain pending.

The independent cold104 on `fcfabc7e36685f23246b6c93da5264c23468030c` received
one closed HTTP503 from Gemini3.8 in 9.842 seconds. Its Native spatial reserve
was not invoked: the route incorrectly blocked model rotation for every closed
provider failure. Independent text fallback exceeded its Live input envelope;
the case ended at 180.025 seconds with no object/facts. This is a concrete routing
defect, not evidence of a global resource shortage. The correction permits a
different model after a received availability/auth/model error, preserves each
failed route in the durable marker, and prohibits retrying that same model after
restart. UNKNOWN and semantic responses retain their existing fence. **79 focused
checks passed in 26.83 seconds**, including HTTP429/503 to Native and registered
Google alternatives without weakening geometry evidence. Real Luna spatial
recognition still needs verification.

Hosted backend CI on `fcfabc7` found 18 failures (2,470 passed, five skipped):
the Prussia admission fixture still asserted the old short call timeout after
SOURCE/MAP moved to the existing total attempt budget. Its assertion now checks
that joint budget, and optional admission retry headroom uses the same actual
budget. **97 related checks passed in 45.25 seconds**, then **21 route checks in
8.71 seconds**, including a real persisted-marker restart after closed503 that
skips the failed model. Final hosted CI remains required on the corrected commit.

The current method contract is [photo search methods](../photo-search-methods.md),
following the latest unified owner prompt. Earlier audit prompts are historical
evidence. Runtime receives original photo bytes, available camera metadata and
explicit owner camera context; report-only building labels/addresses are not seeds.

## Disk recovery and offline corrections after the owner amendment

The 9 October owner amendment and concrete next step are now preserved in the
repository prompt index. Prussia39 remains an actual architecture-text discovery
and selected-body reader; Wiki is complementary. Regional preparation uses
supplied physical subjects' own or verified entrance addresses. Mixed/anonymous
subjects do not inherit the camera's reverse-geocoded road. Distinct entrance
numbers remain distinct, and coordinate lookup uses the nominated body's point.

Current geometry requires actual indexed primitives, a horizontal pose, an
individual SOURCE pattern, numeric relations and explicit uncertainty scenarios.
Its certificate preserves conditional prior alternatives and the whole physical
context. Current architecture-text acceptance requires an individual structural
correspondence rather than matching color/style/history words. These host guards
validate evidence structure and measured map relations; SOURCE interpretation
remains the joint model's responsibility. Legacy addressed/UNKNOWN contracts are
not rewritten as current proofs.

The initial context includes all body rows but only displayed-area side excerpts.
An explicit `map_detail` expands up to three received bodies, exposing every
observed side, including short setbacks, in the existing single followup. Full
overview and neutral labels remain unchanged. Its actual new MAP hash is frozen;
authoritatively unsent followup reuse restores the original MAP. Selected TEXT
shares this operation and replaces the system schema once. Ready SOURCE/MAP does
not wait for a cold catalogue. Text-discovery adapters receive a small role schema
and check addressed serialization against the existing limits. Native built-in
provider/tool instructions are not fully externally attested; exposed agent
prompt and product request/schema are checked, without claiming a complete
provider billing-token count.

The final affected **159 checks passed in 46.76 s**. They cover detail expansion,
short sides, proof/hash binding, prior alternatives, current structural TEXT,
legacy contracts, catalogue nonblocking behavior, not-sent/restart/UNKNOWN,
regional lookup, selected Wiki text and input-envelope handling. Retained offline
replay on **ten original photos** (102, 106, 111, 120, 121, 122, 125, 126, 130,
132) verified input construction, complete body reachability and unchanged
overview/labels under explicit expansion. It did not run model interpretation or
automatic object/fact acceptance. The retained actual photo102 replay preserves
the old frozen proof hash and rejects its wrong generic decision under the current
contract. Its prepared product system-plus-prompt is **54,874 UTF-8 bytes**;
this is not a provider-token or monetary estimate. New model inferences: **zero**.

Urgent disk recovery losslessly compressed **6,881 completed synthetic pytest
SQLite fixtures**, verifying each decompressed SHA-256, and reclaimed
**4,683,230,700 bytes**. Ten redundant clean worktree directories were removed;
all branches/commits and a verified complete Git bundle remain. Original photos,
actual canary databases, runtime logs and retained reports were preserved. No
retained artifact directory was deleted. Space increased from approximately
73 MiB to 4.7 GiB at completion; subsequent readback showed 6 GiB free. Cleanup
evidence and exact worktree restoration commands are retained centrally in
`/home/dev/artifacts/street-story/20261009T070231Z-disk-recovery-20261009`.

These earlier offline corrections and cleanup did not establish product
acceptance. The Gemini3.8 admission denial described in that snapshot is
historical; the current controller reset and subsequent admitted call are
recorded above. No deployment or product PASS is reported.

Full hosted CI on `5e89d33` exposed nine failures (2,466 passed, five skipped).
Corrections preserve literal partial streets and received nearby road/locality
hints in the small context, retain legacy schema selection for original planner
readback, and adapt context/namespace fixtures to the compact role transport.
The real Live review regression required splitting by the complete escaped
shared setup, tool schema and trigger, rather than prompt bytes alone. Whole
passages remain intact and existing bounded worker turns finish the smaller
packets. The final correction set passed **122 tests, two retained-case skips, in
45.88 s**. The same ten-photo construction replay still passes with no provider
inference. Android checks and CI emulator passed on `5e89d33`; release was skipped.
These CI diagnostics are retained separately from the final product gate.

## Latest diagnostic and bounded correction

Frozen `29935371ac190a7a2eb92d6d80b4391226760365` cold photo102 is **NO PASS**.
At 2026-10-09 02:36 UTC, shared quota admission denied all six configured
Gemini3.8Flash keys with reason `rpd`, `provider_send_state=not_sent` and
Retry-After **77,019.879–77,023.743 s** (approximately the next UTC daily reset).
No Gemini3.8 SDK inference occurred. This is a specific route admission blocker,
not proof that all Google/provider resources are exhausted.

The existing loop subsequently sent one Gemini3.5FlashLite request. Its actual
closed response accepted `osm:way:133035102`, the wrong neighbour, by geometry.
Ordinary wrong-object Stop ended the bounded canary at **21.900 s**, with zero
eligible facts/POI assertions. The post-stop report snapshot was captured at
22.196 s. SOURCE/MAP provenance and actual building-address memberships are
present; the decision supplies only generic contour agreement and no compared
alternative. The actual plan declared only its selected subject, so a guard
covering declared alternatives would not repair this failure. Gemini3.8 spatial
recognition remains untested. Other final cases were not run on this SHA.

Further live/paid runs stopped on this objective blocker. The independent offline
correction retains the configured SOURCE/MAP role instead of silently substituting
the repeatedly failing lightweight text model. Unavailability retains its retry
time and the existing independent text planner; ordinary text and REF routing
remain available. UNKNOWN and existing addressed requests still forbid resend.
The affected identity/repair/admission/text-fallback checks passed **52 tests,
2 saved-case skips, in 29.41 s**; Ruff and diff checks passed. Hosted `2993537`
backend CI failed two stale pointer wording/schema assertions (2,450 passed,
5 skipped), now corrected without weakening exact host IDs. Its Android checks
and CI emulator passed; release was skipped. These are CI results, not final
five-building product acceptance.

Observed diagnostic totals through this canary are **95 actual Google SDK calls**,
**49 closed answers / 2,077,744 reported total tokens**, 21 typed HTTP failures and
25 UNKNOWN outcomes. Fourteen closed Live operations report **66,441 tokens**.
Six Gemini3.8 admission denials and the metadata GET are not model inference.
These are historical diagnostic lower bounds, not costs of a successful release.
The latest canary alone reports **67,277 Google tokens**, no Live/review sends.
Actual monetary spending remains **unknown** without the provider billing ledger.
Source logs, model-specific quota evidence, independent audits and refreshed
machine-readable accounting are retained with the product-result artifact.

The following sections preserve per-version diagnostic history; results from
different SHAs do not combine into PASS.

Frozen `045d8028556a784f2b656ae61d017a59b32df221` photo102 is **FAIL**:
actual Prussia39 catalogue arrived at 8.878 s and its article body at 20.742 s,
but architectural identity selected neighbouring `osm:way:133035102` at 29.997 s
instead of independently labelled `osm:way:133035113`. Actual SOURCE/TEXT hashes
and request binding passed; semantic physical discrimination did not. Zero facts
or POI assertions were accepted. Ordinary fenced operator Stop ended at 233.662 s.
Two Google SDK responses reported 136,859 total tokens and one closed Live
extraction reported 2,558; no review was sent. Monetary cost remains unknown.

The second joint lost prior alternatives when no schema repair was needed.
Prepared correction `2cb4e18` preserves initial SOURCE observations/uncertainty
as conditional hypotheses and requires explicit material-alternative coverage in
the same existing second call. The saved erroneous answer fails that new coverage
guard; semantic correctness still requires a live result. Its isolated affected
set passed 122 tests in 43.62 s.

The ordinary fact reader froze 5,438 characters including navigation before the
1,046-character publisher body. Its empty first core deferred useful continuation
for 300 seconds. Prepared `e80c064` reuses the verified publisher parser on the
same cached raw bytes; the saved replay matches the acquired article body hash
without HTTP/model calls and retains the previous source version. Closed committed
cores with valid content/subject and actual unread passages schedule the existing
job after one second; UNKNOWN does not take that path. Sixty isolated affected
checks passed in 28.49 s.

Acceptance-only `3987ae9` sends ordinary fenced Stop immediately after report-only
wrong-object detection, without injecting a correct address or candidate into
runtime. Twenty-five checks passed in 18.33 s. Ruff and diff checks passed for the
three changes. These sets overlap and are not summed. Hosted backend, Android
checks and emulator succeeded on `045d802`; release was skipped. This is CI evidence,
not the final product/emulator acceptance required after the five-building gate.
The merged continuity/body/continuation/acceptance set passed **86 tests in 25.57 s**;
Ruff and diff checks passed. All eleven critical requirements retain their digest.

Frozen `bc1e660b5a0b5fa6e789e79770cf7210482df5fe` photo102 naturally failed
at 19.460 s with no identity/facts. The actual street catalogue returned two cards,
including SID3875 for the target address. The frozen model packet retained both
address numbers and their exact building-node memberships. The initial answer
invented `osm:way:432`; its reported MAP label432 actually names another building,
not the independently labelled target. Strict validation rejected both responses.
Two closed Google SDK responses reported 135,095 tokens; no Live/review was sent.
No quota blocker was observed; monetary billing remains unknown.

Prepared `8d9dfbd` makes explicit frozen MAP pointers consistent across physical
fields and gives exact label/ID mismatch feedback, with no suffix guessing or
automatic subject assignment. It also retains joint2's malformed response in a
separate immutable bounded diagnostic. Its 103 isolated affected checks passed in
36.64 s. Prepared `b45cdac` uses an already registered configured alternative for
the existing second joint only after a closed initial contract failure, retaining
its own quota/executor and frozen model ID. The actual configured tertiary model
was available through API metadata (no inference). Fifty-three merged checks passed
in 12.58 s, including two-call execution and UNKNOWN restart without resend. Ruff
and diff checks passed. The existing NOT_SENT admission/retry/lease set also passed
18 tests in 15.37 s. Neither transport replay nor model metadata proves correct
recognition; the final same-version building set remains gated.

Frozen `d8557ba8b58b141d9d969a59294cacce674a2ee4` photo102 accepted wrong
`osm:way:134757757` by geometry. Ordinary fenced acceptance Stop ended at 14.073 s
with zero eligible facts. One closed Gemini3.5FlashLite SDK response reported
67,491 tokens. The pointer contract was satisfied, but physical interpretation
was wrong. The next SOURCE/MAP operation therefore prefers the already registered,
configured Gemini3.8Flash route; other roles retain their routing and the same
operation/deadline budget. No model name, key, quota or helper chain is added.
The affected role/geometry/pointer/admission set passed **46 tests in 24.12 s**,
with Ruff/diff checks passing. Actual inference and correct recognition on the
new route remain unverified until its bounded canary.

## Bounded live diagnostics

All listed runs are cold photo104 diagnostics, not final-set acceptance. Other
selected buildings were not started after a failed canary.

| Frozen source | Identity | Full/stop time | Reviewed facts | Outcome |
| --- | --- | --- | --- | --- |
| `c033e33` | None | 180.05 s | 0 | Natural identity deadline; malformed closed plan |
| `c09a123` | None | Stop 105.18 s | 0 | UNKNOWN retries exposed; one closed map-label answer rejected |
| `45197c6` | None | 23.69 s | 0 | Closed Google 503, then independent text preflight limits |
| `2142455` | Correct OSM relation, geometry 15.67 s | 480.94 s | 0 | Natural facts deadline; nine nonliteral review quotes, later extraction schema failure |
| `c528647` | Correct OSM relation, geometry 17.23 s | Stop 168.69 s | 0 | Seven candidate-as-quote copies rejected; all observed provider receipts closed before harness termination |
| `80fb6cd` | None | 29.58 s | 0 | Optional joint follow-up denied before send by shared TPM; caller blocked reuse of the closed initial plan |
| `4cdd3f6` | None; conditional context only | 181.07 s | 0 | Natural identity deadline; initial plan reused after not-sent follow-up, Native comparison uncertain against illustration REF |

The correct photo104 physical object is `osm:relation:3665416`, independently
labelled for reporting. Neither its identity nor raw extracted statements count
as useful accepted facts. First useful reviewed fact time is unavailable in these
runs. Operator Stop is separate from natural completion and cannot earn PASS.
The `80fb6cd` refusal had an observed retry delay of 50.776 seconds. Independent
helper planning was never dispatched, so this is not evidence of global provider
unavailability. Further paid cases were stopped while fixing that caller transition.
The initial response hash survives, but its complete successful JSON was not
persisted; it cannot be reconstructed honestly for recovery of this expired run.
The follow-up correction persists a complete bounded, host-validated original
answer before an optional TEXT operation. Only known `not_sent` can reuse its
valid nominations without another planner. SOURCE, camera/context, configuration,
schema and receipt changes block reuse; malformed or UNKNOWN decisions do not
authorize identity. Twelve new regressions and 66 affected isolated checks passed;
the merged initial-plan/diagnostics/fence set passed 48 tests in 39.81 seconds.

The `4cdd3f6` run verified that transition: after a known not-sent TPM denial,
the valid initial plan was reused without a fresh Google planner. Its exact
Google image pair was blocked before SDK invocation; Native then returned
uncertain against a stylized illustration reference. A later reference triage
did invoke the SDK and was cancelled with an unknown outcome. The original
receipt remains fenced. No physical identity, canonical POI facts or three
substantive claims were accepted; conditional context cannot earn PASS.

## Corrections and offline evidence

Initial joint operations and optional follow-ups retain original UNKNOWN sends
across keys, models and restart. Known closed failures can use existing independent
routes; admission failures before actual SDK invocation are `not_sent`. Reservation
journals are not refunded by that classification. Exact compact map identifiers
resolve only against their own frozen input; physical-proof validation is unchanged.

Regional discovery retains actual address/coordinate cards and aliases, waits only
for bounded optional preparation, and reads the model's 1–2 nominations. Selected
Wiki article text is complementary. Text-only planning retains the full received
packet: the saved Native base prompt shrank from 65,672 to 62,507 characters, but the
strict schema yields 110,579 addressed characters. Live correctly refuses planning
inputs above 24 KB. This remains an input-size limitation, not global quota evidence.
The then-current three-second catalogue window could miss cold HTTP responses of
5–15 seconds. A complete narrow lookup returning 1–2 cards can deliver their bodies
for the second joint call; late broad/partial inventory cannot currently produce a
text proof within that two-call budget. Photo102 has actual bound OSM address entries,
but no retained blind number-query response proves that its lookup will be narrow.
This limitation is separate from completed geometry or reference proofs.

Headless candidate IDs are already host-assigned from frozen batch/index. Omitted
advisory model keys no longer discard otherwise complete claims. The unchanged
13-candidate saved Live answer passes this bookkeeping change; deleting each of
nine substantive required fields still fails. Interactive semantic-key requirements
remain intact, and candidates stay unreviewed until supported by semantic review.

Private own-quote labels address unchanged literal slices of one fact's selected
evidence. They never prove atomicity, entailment, temporal scope or physical
subject. Both measured label-enabled reviews ignored those labels and copied
candidate prose; the ordinary literal guard rejected all seven quotes. The private
response schema now enumerates exact frozen quote labels; these seven saved
paraphrases fail closure validation. Original schemas and responses are retained
for addressed readback. Closed answers are not repaired or imported after their
attempt expired. This transport correction does not establish semantic acceptance.

The saved installer evidence qualifies Mimo/Nemotron for semantic review: all
five schema/own-passage/qualifier-negative/duplicate/conflict checks have matching
completed receipts and verified file hashes. Live has an extraction contract but
no matching semantic-review proof; its former automatic review exemption has been
removed. Live-first extraction remains available. Original addressed operations
remain technical observations and cannot authorize unqualified eligible facts.
UNKNOWN, malformed closed and exhausted checkpoints retain their exact input,
schema and diagnostic answer; this retention does not promote rejected evidence.

Closed rejected/exhausted review scopes end only when the complete review recipe
is unchanged. Changed claim revisions, owner context, eligible ledger or verifier
contract reopen useful work. UNKNOWN requests, deferred source pages and joined
continuations remain pending. Exhaustion establishes no factual support.

Relevant checks: integrated quote/review/pool 73 passed; advisory-key ingestion 11
passed; closed completion 118 isolated and 35 merged passed; acceptance Stop 18 passed;
private constrained schema, review and completion 87 merged passed.
The follow-up retention set passed 21 tests; semantic-review admission passed 121
tests in its isolated checkout. The combined review, retention, pool, completion
and acceptance-harness checks passed 151 tests in 83.74 seconds. Ruff and diff
checks passed. These are offline checks, not product acceptance.
Earlier affected geography, regional/Wiki extraction, initial/follow-up fences and
actual SDK/quota boundaries remain covered. These sets overlap and are not summed.
All 11 critical requirements remain unchanged (requirements digest
`f40c8766cca8f7d49519f1fe76c4caff8c6d39a14e895a6288b4735963dd35e3`).

The follow-up preparation and admission correction now releases the initial key
before article HTTP work. A useful second joint uses its own ordinary executor;
only typed, definitively unsent shared admission with a finite delay can retry once
outside leases while waiting plus the call still fit the original identity cap.
The exact prepared request and once-only transition persist. Restart during that
wait conservatively reuses the closed initial plan rather than sending again.
The combined affected identity, regional source/fact leads, freeze/restart,
admission and acceptance/installer fixture checks passed **208 tests in 87.42 s**;
Ruff and diff checks passed. The saved invalid Wiki-number-to-OSM alternative stays
rejected. Explicit received Prussia cards and verified acquired bodies now reach
ordinary facts even after geometry acceptance; mixed-subject nominations preserve
only the accepted subject's article. Fact review and POI eligibility remain required.

Hosted backend CI on `4cdd3f6` failed 12 checks while 2,324 passed; it was not a billing
blocker. Causes included incomplete minimal-adapter compatibility, stale initial
planning expectations, an obsolete text-unavailability fixture, devserver-only
installer transport and filesystem assumptions. The targeted corrections above
passed locally. Hosted Android checks and emulator jobs on that old version passed;
they are not final product E2E. These offline results establish no release PASS.

## Frozen `24bbff5` product evidence

Photo104 established `osm:relation:3665416` by acquired Wiki architectural text
in **31.32 s**, with no external REF. A real Live extraction and qualified
semantic review committed seven eligible claims to the same physical POI at
**238.97 s**; the natural useful-partial outcome completed at **242.44 s**.
Independent own-passage and POI readback verified at least three substantive
atomic claims: foundation date (distinct from construction), diameter, and the
paired defensive purpose. The source's 1853/1859 construction conflict remains
explicit. One supported construction-and-namesake claim was wrongly reviewed
as atomic; this granularity defect remains open. Secondary heritage-status text
does not constitute a dated 2026 registry verification.

Photo102 failed at **40.25 s**, with no identity or facts. Both closed answers
nominated a received road ID in a field restricted to candidate IDs. The map
also contained the actual building and its address entrances; the gate correctly
rejected the road. Prussia preparation had the correct already-received street,
but the three-second cutoff delivered zero cards. No broader paid set followed.
The prepared reader-budget correction preserves the existing 12-second publisher
read envelope before model admission and passed **59 offline tests in 21.45 s**,
including delayed cards beyond the obsolete cutoff and cancellation/draining.
This is not a new live acceptance result.

Role-aware nomination diagnostics passed 50 focused checks, followed by 48 merged
identity/catalogue/admission checks in 31.85 s. They distinguish received map
context from eligible nomination IDs in the existing useful repair; strict enums,
unchanged SOURCE/MAP bytes and the two-call budget remain intact. Actual saved
photo102 entrance memberships preserve literal house numbers through the real
Prussia query; no transport patch or runtime audit hint is required.

Cold review dispatch now uses measured qualification receipt durations when
there are no current same-operation measurements: Mimo 91.741 s and Nemotron
34.284 s in the verified source receipts. Current dispatch history overrides
these hints. The installer exports only bound scalar metadata; runtime opens no
receipt paths. Original qualification provenance and derived runtime-cache hashes
are frozen separately. The scheduling change passed 68 focused checks; provenance
and existing 429-history compatibility passed 34 checks. These overlap and are
not summed. No new provider probes, model caps or timeout changes were used.
Live review qualification and the compound-claim defect remain open; neither
changed dispatch order nor literal labels establishes semantic correctness.
The final merged reader, nomination-role, measured-review, qualification and
acceptance checks passed **114 tests in 32.61 s** before the next bounded canary.

Hosted backend CI on `24bbff5` passed. Android checks passed, but instrumentation
failed its month-old original-photo selector (one failure of 27 tests). The
retained DocumentsUI hierarchy shows the Camera card still at the Images root;
its exact navigation failure remains to be resolved before Android acceptance.
There is no final five-building/backend PASS, accepted release or deployment.

## Usage and remaining acceptance

Observed diagnostic totals as of 01:09 UTC: 85 Google SDK invocations, 39 closed
model answers reporting 1,406,266 total tokens; 21 typed HTTP failures and 25 unknown
outcomes. Twelve closed Live operations report 59,742 total tokens. Three actual
GigaChat sends in `2142455` have unreported token usage; addressed Native outcomes
also contain unknown usage. Provider attempts are not inference counts. These
are lower bounds across historical diagnostics, not final-release costs.
Google counts are deduplicated by actual SDK call ID across 18 retained logs;
the HTTP failures comprise thirteen 429, six 400 and two 503 responses. The
`4cdd3f6` run adds six SDK calls and 103,389 reported Google tokens. Its closed
Native pair reports 10,282 tokens and remains an uncertain identity result. A
reported partial Mimo search counter of 10,647 tokens is retained separately:
its closing and counter provenance are unverified, so it is excluded from
whole-inference usage totals.
Monetary total is **unknown** because no complete provider billing ledger is
available; one Native free-route cost field of 0 does not establish total spending 0.

Final acceptance remains pending on one frozen SHA for 102,104,111,130,132;
122 is reserve and 121 a separate retained spatial holdout. Rich cases require
at least 3 substantive reviewed facts, ordinary cases at least 1, with actual own
sources, semantic checks and canonical POI readback. Verify first useful fact
within 300 s and natural terminal within 480 s, at least one actual Live-first send,
and geometry before REF for 130/132. Only then run one Android emulator E2E and
consider deployment of that same version. No mixed-SHA, identity-only, unreviewed
facts, forced terminal or manually supplied runtime answers may establish PASS.

## October 9: remove invented input refusals, retain actual Live constraints

On frozen `c2dcc5a`, cold photo111 received one closed Google HTTP200 answer,
but the nominated modern building lacked valid physical proof; the host rejected
it and the case naturally failed at 180.016 s with no identity or facts. Cold
photo130 received a known HTTP503, selected the secondary Luna route, then hit
the **local** 65,536-byte refusal on a 69,191-byte input before Native inference.
Google Flash Lite subsequently returned HTTP200 with incomplete hypothesis
coverage. The case naturally failed at 180.026 s with no identity or facts.
This proves route selection, not successful Luna spatial inference.

The owner rejected artificial limits and separately asked to preserve genuine
Live context constraints. Prepared code removes the local 24/64 KB refusals in
Native, OpenCode/shared Native, Live and GigaChat, the semantic catalog ceiling,
and the fact-review size filter that excluded qualified routes or exhausted an
indivisible packet. Complete input size remains observable. An old known-unsent
planner refusal can get a new recorded attempt without changing its prior receipt;
UNKNOWN still requires original observation and never authorizes a duplicate.
Small complete fact packets remain a scheduling preference, with no passage clipping.

The configured Live model is `gemini-3.8-live`. Google documents **131,072 input
tokens** and **65,536 output tokens**, verified October 9 against its
[model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live).
Those values are recorded separately from wire bytes; token counts not measured
remain unknown. The shared Live transport already enables sliding-window context
compression. Every bounded headless operation starts its own scoped session.
The former 24,000-byte gate was not a check against the documented token window.

Schema-valid joint responses that fail host geometry evidence or first-wave
coverage now feed their concrete failure into the existing one useful repair.
The failed claim is explicitly not confirmation. This change neither lowers the
proof threshold nor adds a third semantic judge.

The final transport, complete-packet, Native, GigaChat and geometry-plan checks
passed **202 tests in 55.74 s**; catalog, repair, first-wave, pointer, UNKNOWN
and Prussia-admission checks passed **80 tests in 25.11 s**. Ruff also passed.
Actual new release acceptance remains pending;
these mock transport tests do not establish full product PASS or a deployment.

On frozen `2b0c9f1`, full hosted backend CI passed **2505 tests, 5 skipped** with
Ruff passing. Cold photo132 naturally failed at **180.021 s**, no identity or
facts. Its primary Google request closed HTTP503; Luna reached the real Native
turn with **77,438 bytes** of owned textual input but closed HTTP400 because
`uniqueItems` is unsupported in Native strict structured output. Thus the local
byte refusal is removed, but successful Luna spatial inference is still unproved.
Live's complete 48,705-byte setup reached the host and failed before text send
with `ConnectionClosedError`; no confirmed Live context overflow is established.

Prepared Native transport now removes unsupported uniqueness and conditional
composition keywords from only the provider schema. The original host schema
is separately frozen unchanged and validates duplicates and conditional evidence
on return. This also prevents injecting transport `additionalProperties=false`
into host `if` conditions, which could otherwise make them silently inapplicable.
Exact original schemas/images remain bound during readback. Final affected
checks passed **59 tests in 14.37 s**, including deliberately invalid duplicate
and conditional answers; no weaker proof or artificial input gate was introduced.

## G/T integration and productive insufficient-proof continuation

Frozen `4fe8e28` hosted backend CI passed **2508 tests, 5 skipped**. Actual
Luna SOURCE/MAP inference on cold132 closed successfully with 35,537 input and
1,998 output tokens. Its physical nomination agreed with independent report-only
labels, but two sides of one body were wrongly submitted as `frontage_sequence`.
The host correctly rejected this proof; natural outcome was failure at 56.317 s,
with no accepted identity/facts. This is useful nomination, not product PASS.

Integration selectively uses G supply `c125a2f` (EXIF/body angular reference,
without ranking) and T supply `ed0a370` (actual publisher-address joins, verified
entrance context and compact SOURCE/article comparison). Overlapping independent
orchestrator/transport changes are not merged. No new arbitrary T input-size or
candidate-count refusal is introduced. A closed insufficient Native nomination
is preserved separately as an unconfirmed hypothesis, with exact original
response, source/map hashes, physical/address memberships and rejection reason.
The existing joint2 can repair the concrete geometry failure or independently
evaluate already acquired architectural text. Sufficient G does not require T.

The integration suite passed **127 checks in 49.29 s**; the separate broader
guardrail suite passed **231 checks in 102.74 s** (overlapping suites, not an
additive test count). Controlled ordinary-worker tests cover G repair, rejected G
to accepted T to cached article/facts/own-evidence review/canonical POI readback,
T uncertainty, and UNKNOWN without resend. A local replay of the original132
receipt verified the concrete continuation with **zero new provider inferences**
and no expected-answer input. These are integration checks; real cold132 followed
by the same-SHA independent product set and deployment are still required.

## Exact nominated-address continuation after the first integrated canary

Cold132 on `86ddf089fb6ba2942c23db6f5c99bd584e4352bb` naturally failed at
111.882 s with no accepted identity or facts. Primary Gemini 3.8 returned 503;
Native Luna completed and preserved a concrete physical hypothesis. The existing
joint2 on Gemini 3.5 Flash Lite succeeded (HTTP 200, 12.433 s, 39,209 input / 1,728
output tokens), but replaced `frontage_sequence` with an unsupported `corner`.
The proof validator rejected that geometry. The subsequent reference attempt hit
the directed acceptance's intentional `directed_acceptance_external_reference_disabled`
seam; the generic provider error did not represent exhausted quota.

The initial model had skipped articles because it believed G was sufficient.
After the host invalidates that premise, the integration now reads the exact
model-nominated body's own/verified-entrance address through the existing regional
reader before spending joint2. It does this only when no explicit text selection
or map-detail action is pending and the rejection is semantic. Ambiguous addresses
and neighboring publisher cards remain refused; address retrieval never accepts
identity. Useful actual article bytes go to the supplied compact T component and
the original proof authority, without a third model/planner.

The original unconfirmed132 nomination independently retrieved a real publisher
body with facade/risalit/window-axis/gable descriptions. Replaying its original
SOURCE/MAP receipt reached compact T with those acquired bytes and **zero new
inference sends**. This is evidence of input supply, not a real T decision.
Focused ordinary-worker checks passed **16 tests in 12.96 s**, including accepted
T to cached publisher parsing/facts/own-evidence review/canonical POI readback,
neighbor-card refusal to the original G correction, UNKNOWN and the updated single
T schema. Full local backend verification passed **2,545 tests, 2 skipped in
425.59 s**; Ruff passed. These are implementation checks; the changed-SHA
product canary and its fact/POI outcomes remain separate acceptance evidence.

Cold132 on `0e2302587545f3e5df585ab77e235329a86f3513` naturally failed at
73.153 s. This time the original Native result was uncertain with a blank final
candidate ID and an explicit received physical target for `map_detail`. The
requested detail reached joint2, but the invalid-accepted-proof-only acquisition
guard supplied no text. The Lite result again failed geometry; directed REF
acceptance stopped the subsequent reference attempt. Hosted backend on this SHA
passed; this is not product PASS.

Continuation now also preserves received first-wave/action physical nominations
from uncertain G. Exact nominated-address acquisition can accompany the requested
MAP expansion, and useful text selects the supplied compact independent T schema
in that same joint2. The final candidate field is never filled administratively;
only an accepted G/T proof can establish identity. All explicit selections,
ambiguous addresses, original-input readback and UNKNOWN fences remain. The
affected suite passed **85 checks in 30.54 s**, including uncertain/blank-final-ID
MAP-detail nomination to T/facts/POI. The original real response replay reached
actual publisher T input with zero new inference. A new frozen canary is required.

Cold132 on `90c38443cadb22e4e4c952420cb66128c24d6838` reached real compact T
on Gemini 3.5 Flash Lite (HTTP 200) with a model-selected Wikipedia body. Its
positive answer cited only `generic_style` and `historical_fact`; the unchanged
structural proof correctly rejected it. No accepted identity/facts resulted.
Hosted Backend and Android passed on this SHA.

The existing nominated-address reader can now complement a selected Wiki body
before joint2, instead of running only when no text was acquired. Actual raw
source bodies and provenance remain separate. If the existing one/two-article T
operation cannot hold all acquired alternatives, all pending bodies survive in
the lookup receipt rather than silently choosing/truncating cards. The affected
suite passed **86 checks in 34.00 s**, including two-source T to ordinary
facts/POI and zero new HTTP/semantic sends after known-unsent restart. Next actual
T/fact verification starts with the retained completed G turn, explicitly marked
as receipt continuation rather than a new cold G inference.
# Latest T transport integration

Actual addbc25 retained-G/T continuation accepted the correct object at 22.436 seconds and persisted seven independently reviewed canonical POI facts (first at 85.891 seconds). Its external SIGTERM/recovery and 480-second partial ending are not a full acceptance PASS. The subsequent blind cold132 accepted sufficient original Luna G at 52.323 seconds, so no T was required, and persisted four reviewed facts (first at 203.660 seconds). Full completion still exceeded 480 seconds: **FAIL**.

That cold run revealed a concrete facts handoff defect: the autonomous extractor used interactive two-passage/5500-byte tool-reply pagination for a 4116-character frozen source core. Its valid `continuation_needed=false` reply could not close the unread core; later attempts repeated extraction/review. Autonomous preparation now supplies the complete existing source core to its qualified provider. Interactive pagination is unchanged, and the Live semantic client still measures/admit its actual context with real provider token capabilities. No proof, source coverage, acceptance deadline or fact threshold was weakened. Offline: 52 fact/G/T tests plus 32 context/budget/Stop tests passed. Closed source extraction/readback does not require another G/T inference.

The ca37cbb retained-G continuation is **FAIL**, not a cold acceptance: actual original G readback (zero new G inferences) reached two acquired articles and actual T HTTP 200 in 7.326 seconds. The host rejected the response format with `identity_architectural_comparison_invalid`; the old exception branch did not retain that response body. Its semantic cause is unknown. No identity or eligible fact was accepted. Both hosted ca37cbb workflows passed.

Selectively integrated T PR #249 through `000fe93ff78993585ca32fb9fb6fae05424638e3`: publisher article address provenance, compound publisher groups matched against distinct verified OSM entrances as retrieval evidence, and normalization of only the inert top-level `type=object` schema echo. Other additional/malformed fields remain invalid. Existing full host proof authority is unchanged; no new character/candidate refusal gate was imported.

The existing compact T request now supplies its own small JSON schema to constrained decoding. The closed-invalid exception branch retains original raw response/hash, response ID, schema errors and operation binding. Tests cover malformed JSON and missing fields, rejection without identity/facts, and restart with no resend. This repairs the concrete format/diagnostic boundary, rather than providing a third semantic method. Actual verification will reuse the completed original G and public article bodies; its result remains explicitly NOT_COLD until the separate cold acceptance.

## Scoped fact-equivalence contract repair

Actual f18da12 retained-G fact continuation delivered all five frozen passages in one Live extraction, once. Its four candidates included one supported construction fact, one correctly withheld undated current-use claim and two pending candidates. It ended at 347.155 seconds with only one independently reviewed canonical fact: FAIL, explicitly NOT_COLD. No new G/T inference was sent. Both hosted workflows passed on f18da12.

The retained closed final review contained `equivalent_to=-1`. The issued schema allowed arbitrary integers although host validation accepts only local canonical fact numbers. Fresh private schemas now enumerate the packet's actual fact numbers and nullable absence; the prompt explicitly disallows a fabricated -1 relation. The verifier contract changes so this repaired operation has its own identity while original UNKNOWN/readback units keep their saved prompt/schema. Host normalization handles null like omission/self-addressing; negative, foreign and chained pointers remain rejected. Source quotes, atomicity, qualifier checks and canonical proof authority are unchanged. The original invalid response is preserved, rather than repaired into a positive answer. The focused review/qualification/completion suite passed 121 tests; 54 additional ordinary packet, retained-answer and POI regression checks passed. This is a contract repair, not final product acceptance.
