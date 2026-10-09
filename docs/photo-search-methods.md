# Photo search: concrete target methods

Target dated 2026-10-08, amended 2026-10-09 after the failed canaries. The [complete unified brief](prompts/street-story-unified-photo-search-20261008.md)
is authoritative. This document makes the method boundaries and acquisition steps
explicit; it is not a claim that the complete product has passed acceptance.

## One product contract

The ordinary input is original photo bytes plus metadata actually present in those
bytes or explicitly supplied by the owner. GPS denotes the camera. Preserve EXIF
orientation and immutable hashes; record approximate owner coordinates separately.
Unknown heading, accuracy and height remain unknown. Reuse compatible cached
evidence and POI memory; distinguish a cold run from cache/recovery.

Prepare one bounded OSM patch containing the entire available physical pool,
outer/inner contours, roads, parts and mapped address entrances. Nearby Wiki
metadata and direct OSM Wiki links may arrive in parallel. Reverse geocoding is
not a prerequisite for the map. The model receives the actual oriented SOURCE and
readable neutral map, with exact IDs and compact geometry. No paid observation
must precede a mandatory planner and judge chain.

Present one row per physical body with neutral label, contour/side pointers,
boundary distance, angular span, actual own/verified entrance addresses and
unknowns. Full OSM geometry remains durable; distant telephoto targets remain
reachable. Separate postal entrance numbers and literal join provenance must
not require the model to manually join several tables or imply one postal address.
Whole provider input includes the system instructions and schema. Text discovery
gets its own small schema and cannot accept image evidence; Native and Live check
their complete addressed inputs against their existing byte envelopes before sending.

The initial MAP keeps the full overview and every physical body row. Indexed side
excerpts are limited to the displayed area and explicitly state omissions; this
is a presentation limit, not a candidate shortlist. An uncertain joint decision
may request `map_detail` for up to three exact received physical IDs. The existing
single followup then receives the unchanged SOURCE and a MAP with the same full
overview/neutral labels plus the nominated detail panel. All observed sides of
those bodies become available, including short setbacks. The new MAP hash is
bound to that followup; conditional initial alternatives remain hypotheses to
resolve. If admission proves it was not sent, reuse restores the original MAP
and schema. UNKNOWN never authorizes another send. Selected TEXT and detail share
this same followup; its system instruction contains one current contract.

| Method | Evidence the model must actually receive | Sufficient result | Subsequent work |
|---|---|---|---|
| `geometry` | SOURCE + neutral map + available measurements/provenance | A compatible pose and spatial pattern distinguish one physical object from material local alternatives | Accept immediately; start facts without waiting for text or REF |
| `architectural_text` | SOURCE + actually acquired architectural passages, article ID/hash and resolved physical binding; map if available | An individual visible combination distinguishes the bound object; structural contradictions and plausible neighbours are addressed | Accept without REF; reuse the acquired article in the ordinary fact reader |
| `visual_reference` | Full SOURCE + a full external REF whose actual bytes and source are bound to the same physical object | Observed visual agreement satisfies the existing strict pair-proof contract | Accept; reuse properly bound source pages for facts |
| `combined` | Actual evidence from sufficient compatible methods above | One object and scope, with no unresolved decisive conflict | Accept once; do not rerun another method for confirmation alone |

One common accepted-identity predicate applies to workers, Live, progress, facts,
history and POI hydration. `visual_reference_verified` is true only after an actual
REF comparison. An address lead, model confidence, generic style, unavailable
neighbour article or institution name cannot by itself establish a physical object.
An accepted complex retains complex scope; an entrance or tenant is not silently
promoted to a building. A crop or changed paint is not a structural contradiction.

## Prussia39 discovery and architectural text

Prussia39 is a concrete regional source adapter, not merely a `site:` search hint.
Use it for a remaining facade/subject question in its documented region. It is not
a required step after sufficient geometry. The target route is:

1. Derive a literal city/street/address, named hypothesis or candidate area from
   actual owner/OSM/SOURCE evidence. Preserve the original spelling and physical
   entrance/footprint binding. No audit address or known `sid` enters runtime.
   Prefer the nominated body's own address or its verified entrance membership.
   Reverse-geocoded camera road is optional context and cannot nominate its
   street card as the subject. Distinct entrance numbers require an exact entry;
   mixed or partly unaddressed preparation scopes stay unresolved until nomination.
2. Choose one appropriate discovery route: complete Windows-1251 GET form at
   `sight/database.php` (`text_np`, `text_adr`, `text_n` and all observed defaults)
   **or** POST `new_coords=lat,lon` at `sight/map_coord.php`. A coordinate lookup
   covers the publisher's observed local window, not an arbitrary-radius database.
   For a supported distant target, use its candidate area rather than retrying the
   camera point. An external web search remains a possible independent fallback.
3. Present the returned card titles, addresses, short annotations, points, actual
   article IDs, total count and pagination to the model. The model selects 1–2
   relevant descriptions and their intended scope. Do not select the first two
   cards automatically. If the first page is incomplete, permit one useful actual
   pagination link or one more specific literal query within the same budget;
   do not crawl a street or claim that 20 of 35 cards exhaust the catalogue.
4. Read only selected actual card bodies through the existing bounded fetch/cache,
   checking encoding, readable content and original byte hash. Deduplicate by
   canonical article ID. Preserve lookup provenance, fetched time and full source
   version separately from the precise excerpt transmitted to the model. This
   path downloads no gallery or reference images.
5. Compare the acquired text with SOURCE in the joint identity operation. Treat
   stable visible correspondences, unseen details, structural contradictions and
   mutable/historical differences separately. Check physical article binding and
   material alternatives; several descriptions of the same bay are not independent
   votes. If sufficient, accept `architectural_text` or `combined`; otherwise
   retain the concrete ambiguity and choose one useful action.

When selected text arrives for a second joint operation, carry the initial SOURCE
observations, uncertainty and declared physical alternatives as conditional model
hypotheses, never as acquired evidence. The model must reconsider them against the
actual SOURCE/MAP/TEXT. A final text proof must explicitly address every previously
declared received nomination other than its subject. The host checks this pointer
coverage; it cannot establish that the model's comparative reasoning is correct.

Cache a literal street lookup across candidates and neighbouring photos. A second
route requires an identified gap in the first; empty external `site:` results are
not proof that a card is absent. No publisher-wide crawler, address-to-answer
rules, new database or automatic domain scoring is introduced.

Wiki is complementary: receive lightweight direct/nearby leads early, read only
explicitly selected and bound pages, and use actual architecture text if useful.
Wiki metadata/snippets are not acquired article bodies. Its lack of an article
does not cancel a sufficient map or Prussia39 proof. Architecture text from any
source must obey the same proof and physical-scope contract.

## Adaptive execution and truthful outcomes

Select the cheapest ready evidence that answers the current uncertainty. Preserve
one joint SOURCE + context decision and at most one meaningful replanning, rather
than add mandatory observation, catalogue-selection, geometry and text judges.
Catalog metadata and selected-body acquisition must be arranged within that
operation budget; simply adding a third mandatory judge is not the target.
Ready SOURCE/MAP does not wait for a cold street catalogue. An owned optional
read may complete during the joint operation; its source cache and scoped receipt
remain available to the existing continuation. Late metadata does not mutate an
already addressed request. Remaining owned work is drained on operation exit.

Fill free worker slots as results complete under existing resource admission.
The SOURCE/MAP joint prefers the configured tertiary Gemini model. Following the
owner's October 9 clarification, it is no longer a mandatory single-model
dependency: a definitive unsent refusal or received availability error (such as
HTTP 429/503) permits a different registered model tuple, including Lite. A
received semantic response and an UNKNOWN outcome do not authorize rotation.
Closed service failures retain their model and status; the failed model is not
repeated after restart. Each model uses its own controller limits and receives
the same SOURCE, MAP and proof contract; a 500-RPD allowance does not change the
acceptance threshold. Wrong lightweight outputs remain regression evidence,
not a blanket capability ban. Model selection is logged and frozen in the receipt.

`gpt-6-luna` is an explicit secondary SOURCE/MAP route after the preferred Google
route is unsent or returns a definitive availability error. It reuses the installed
Native vision transport, tool-free profile,
shared workload admission and account quota permission. It receives the exact
prepared SOURCE and MAP bytes, with MAP labelled as a map rather than a reference
facade. Its addressed prompt, schema, images and original host proof context are
retained for turn readback; quota loss does not prevent reading that original turn.
Fresh input exceeding the existing 65,536-byte role budget is unsent. Native quota
refusal permits the next registered Google route only with authoritative unsent
evidence. UNKNOWN cannot rotate to another model or create another turn. A closed
valid spatial proof ends the operation; Luna is not a mandatory additional judge.
Ordinary text planning and Live-first fact extraction remain independent.
The initial joint SDK call releases its key before reading selected article bodies
or validating its semantic result. A useful second joint gets its own ordinary
executor timeout. Google SOURCE/MAP uses the existing configured attempt budget
(normally 60 seconds), rather than cancelling at the shorter key-failover timeout
(normally 20 seconds). Overall identity and upload deadlines remain unchanged.
If the shared controller definitively refused it before send,
one finite Retry-After of at most 60 seconds may be observed outside the key lease,
only when both waiting and an ordinary call still fit the identity deadline.
The retry preserves the exact SOURCE/MAP/TEXT, prompt, schema and configuration;
it re-enters the same route's admission without refund or a third joint operation.
UNKNOWN and closed provider failures do not qualify. An interrupted wait retains
the original plan for conservative restart reuse and does not automatically resend.
After a closed initial contract failure, the existing second joint may use the
already configured and registered tertiary model, with that tuple's own executor
and quota. Its model ID is frozen with the prepared request. This substitutes a
route for the same operation; it adds no judge and makes no stronger-model chain
mandatory for valid initial geometry or selected-text work.
All physical pointer fields use the same exact received ID or explicit `@N`
namespace. Only the frozen MAP dictionary resolves `@N`; `osm:way:N` cannot be
guessed from a display label. Exact label/ID mismatches reach the existing repair
operation as feedback, without assigning a corrected subject. Closed invalid
initial and follow-up answers are retained separately; UNKNOWN authorizes no repair.
Reserve extraction/review capacity before spending the fallback budget on images.
Thumbnail sheets only triage the conditional REF route; retain `unclear` images and
exact tile/image/article binding. Final REF proof uses full images.
The complex REF fallback has an outer reserve of 6–8 distinct query hypotheses,
4–6 useful pages and 4–6 exact pairs within admission and the upload deadline.
These are ceilings for unresolved cases, not mandatory work per photo or separate
quotas per provider. A sufficient geometry/text proof stops earlier.

| Observed outcome | Required handling |
|---|---|
| Parsed, explicit empty catalogue | Completed empty for that lookup; revise a specific hypothesis or finish |
| HTTP200 with empty bytes or only an unparsed form | Transport/parse failure; do not report absent building |
| Incomplete inventory | Retain totals and actual next-page links; do not claim exhausted catalogue |
| Article lacks distinctive architecture | Possible facts source after binding; insufficient identity text |
| Definitely `not_sent` | Reassign within admission and deadline |
| Submitted or UNKNOWN | Observe the same frozen original operation; no duplicate payment; independent work continues |
| All useful work exhausted or deadline expired | Truthful finite uncertain/partial/blocked outcome; preserve accepted identity and facts |

Hard caps persist from upload across restarts: identity 3 minutes, first useful
eligible fact 5 minutes, completion 8 minutes. Approximately 30 seconds after SOURCE is
ready is an engineering target, not a demonstrated guarantee. Expiry blocks new
sends; late readback keeps the original ID, provenance and generation fences.

## Facts and acceptance

All methods enter the same existing `fetch/freeze → extract → semantic review →
POI readback` path. Preserve actual supporting passages, source version, temporal
meaning and subject scope. Architecture and historical claims from Prussia39 still
require review; institution founding is not building construction. Display the
first substantive accepted fact immediately. Address-only, upload status and generic
city summaries do not count as useful facts.
The ordinary reader uses Prussia39's verified article-body parser on the same
cached raw bytes; login/navigation is not an article fallback. Original source
versions remain immutable. After a closed, committed core with valid subject and
content, actual unread frozen passages can schedule the existing continuation
promptly. A model's `continuation_needed` alone cannot authorize that transition;
UNKNOWN keeps its original operation and wait.
Explicitly selected, received regional cards can schedule the ordinary fact reader
after identity without delaying geometry for their bodies. Already acquired and
bound text remains a source lead even if geometry established identity. Validate
each nominated article's physical scope independently; a neighbouring selection
neither supplies facts for the target nor discards its valid article. These leads
never become REF proof or eligible claims by themselves.

Headless extraction assigns an opaque candidate ID from its frozen batch and
position. A model `claim_key` is advisory here; its absence does not establish
equivalence or invalidate otherwise complete evidence. Interactive author tools
retain their semantic-key contract. Candidates stay unreviewed until the reviewer
checks their own passages, one independently selectable claim, temporal qualifiers
and physical subject. Private quote labels address literal slices of the exact
frozen packet and selected own evidence; they do not supply a support verdict.
Paraphrased quotes remain invalid. Original responses and UNKNOWN operations stay
immutable and are observed under their original contract after restart.
Live-first extraction does not qualify its model as a semantic reviewer. Every
automatic review route must have matching verified schema, own-passage, qualifier
negative, nearby-duplicate and nearby-conflict checks. Only the existing matching
qualified routes can make facts eligible; extraction remains Live-first.
Cold review scheduling can use a finite measured duration from its hash-verified
qualification receipt, bound to the same provider/model/endpoint/directory and
review operation. Current review receipts override that hint. This changes
dispatch order, never qualification, permission to resend or semantic verdicts.
The intended ordinary Live-only research path still needs equivalent review
qualification; current extraction evidence cannot supply it. Qualified helper
reviews are the observed quality fallback, with their actual use and costs reported.
The supported compound claim seen in photo104 remains a semantic quality defect;
it cannot be fixed by deterministic conjunction splitting or passage labels.
Closed rejected or exhausted reviews stop automatic work only when the complete
review recipe is unchanged: candidate revisions, owner context, eligible ledger
and verifier contract. Changed claims or context remain reviewable. A source
manifest with no remaining actions can finish honestly even with unresolved
candidates; deferred source pages, UNKNOWN requests and joined continuations
still block that conclusion. Exhaustion never establishes support for a fact.

First replay saved evidence and negative controls, including the wrong dormitory,
house 21 versus 22, water tower versus 121 and unresolved 6/6A. Then use one frozen
candidate/configuration/manifest with at most five live cases: 102,104,111,130,132;
122 is a reserve, 121 a separate retained holdout. Cases 130/132 must establish geometry
before any external REF. Rich cases 102/104/132 require at least 3 substantive reviewed
facts; ordinary 111/130 at least 1. Verify sources and canonical POI readback, the
3/5/8 caps and at least one actual Live-first send. Run one emulator E2E only after
backend PASS. A mixed-SHA recovery, isolated identity, raw claims or manual research
does not pass this gate.

## Implementation status and remaining gate

The existing queues support geometry, actual architectural text and visual
reference proofs through common facts/POI handling. Prussia39 uses its real
publisher forms, cp1251, observed cards/pagination and model-nominated bodies;
Wiki remains complementary. Full physical candidates, frozen operations,
resource control and the upload-based deadlines remain in place.

Optional early regional preparation uses only an already received camera street
or one unambiguous observed locality/street. It now uses the publisher reader's
existing 12-second envelope, further bounded by the upload-based identity deadline,
while overlapping SOURCE/MAP preparation and before acquiring a model key.
Late or partial cards remain visible, but do not authorize automatic first-two
selection or another mandatory judge. Independent text fallback preserves the
received packet; its Native addressed prompt can still be large, and Live's
24 KB planning limit remains a real limitation.
The former three-second preparation cutoff repeatedly lost cold cards. Aligning
it with the reader envelope preserves responses slower than three seconds; a
response exceeding the ordinary reader limit can still be unavailable.
A complete literal address lookup with 1–2 results can supply their actual bodies
for the second joint call. A late broad or partial catalogue cannot currently
produce that text proof within the same two-call budget. Preserve its inventory
and continue useful independent work; do not invent first-two selection or a third
mandatory judge. This is a material limitation of the prepared text path.

The `c528647` cold canary (photo104) correctly identified the
physical tower by geometry in 17.23 s, but accepted zero facts. Its closed reviews
copied candidate prose instead of literal source evidence and were rejected.
The operator stopped it at 168.69 s. This establishes an identity result, not
product acceptance. Private review schemas now enumerate only exact frozen quote labels. Saved
answers verify that candidate prose is rejected at this transport boundary;
later live fact acceptance is reported below; this saved transport check alone
did not establish it.
The next frozen canary (`80fb6cd`) stopped at 29.58 seconds before identity: its
optional joint follow-up was known not sent after a temporary shared TPM denial.
The caller prevented the closed initial plan from continuing independently.
This failure does not prove global quota exhaustion.
Closed initial plans now retain complete bounded JSON/schema/input binding and
can continue after a definitively unsent optional follow-up. Reuse checks SOURCE,
camera/context, configuration and the unchanged strict contract; UNKNOWN still
prevents replacement. The new path passed 48 merged offline checks. The `4cdd3f6`
cold canary confirmed that reuse and independent search proceed, but expired at
181.07 seconds with no accepted identity or facts. Its complete 12-card Prussia39
inventory, model-selected article and acquired body reached preparation; shared
TPM admission prevented SOURCE + TEXT from being sent. The selected external
illustration did not establish identity in the full pair comparison.
The SOURCE, MAP and full 667-object pool match earlier successful geometry runs;
the rejected alternative instead used an invented OSM prefix for a Wiki page ID.
Current planning instructions explicitly separate article IDs from received map
objects; exact host validation and outside-coverage uncertainty remain unchanged.

On frozen `24bbff5`, photo104 established the correct physical tower by actual
Wiki architectural text in 31.32 seconds, without REF. Seven reviewed facts
reached canonical POI memory at 238.97 seconds; the run naturally ended at
242.44 seconds with useful partial coverage. Independent readback verified at
least three substantive atomic claims. One source-supported compound claim was
incorrectly marked atomic; this remains a quality defect. Prussia preparation
timed out, so this case does not verify Prussia text acquisition. Photo102 then
failed at 40.25 seconds: both model answers nominated a received map road where
the field required an allowed candidate. The host rejected it correctly; neither
identity nor facts were accepted. Its already received camera street was correct,
but the three-second catalogue cutoff delivered no cards. Other final cases were
not run on this version.
The prepared role-aware repair now identifies the exact received context ID that
is outside a nomination field's catalogue, without broadening that field or
remapping the ID. Saved photo102 data also confirms that its bound entrance
number reaches the real literal Prussia address query unchanged; the failed
model answer never requested that narrower route.

On frozen `045d802`, photo102 acquired actual Prussia39 catalogue metadata at
8.88 seconds and the selected article body at 20.74 seconds. It nevertheless
accepted the wrong neighbouring physical object at 30.00 seconds. Its second joint
answer omitted the initial alternatives and used generic architectural agreement;
valid hashes did not establish correct identity. Zero facts were accepted; ordinary
operator Stop ended the run at 233.66 seconds. The ordinary facts reader also
placed navigation before the article and deferred an empty first core for 300 seconds.
The prepared corrections preserve conditional alternatives, freeze the actual
publisher body and promptly continue closed cores with actual unread passages.
Saved replays verify those boundaries, not recognition correctness. The acceptance
harness now sends ordinary fenced Stop after a report-only wrong-object failure,
without supplying the expected object or a repair hint to runtime.

Frozen `bc1e660` photo102 ended unsuccessfully at 19.46 seconds. Actual catalogue
metadata contained two cards, including the correct-address article, and the
model received complete building/entrance memberships. It nevertheless invented
an OSM ID from a MAP label belonging to a different building; both responses were
rejected. No identity or facts were accepted. The next prepared pointer/repair
correction does not retrospectively fix that answer or establish recognition.
On `d8557ba`, photo102 then supplied a contract-valid wrong geometry identity.
The acceptance harness stopped it at 14.07 seconds, before eligible facts. This
confirmed that pointer coherence alone did not resolve the semantic interpretation
failure. The prepared SOURCE/MAP routing change still needs live acceptance.

On frozen `2993537`, all six registered Gemini3.8Flash keys were denied by shared
RPD admission before an SDK send, with Retry-After 77,019.88–77,023.74 seconds.
The former model loop then sent one Gemini3.5FlashLite request and accepted the
wrong neighbouring footprint. Ordinary acceptance Stop ended at 21.90 seconds;
zero facts were accepted. This tests neither Gemini3.8 recognition nor global
Google availability. Further paid diagnostics stopped. The offline correction
above removes that silent substitution while retaining independent text search.
Its focused identity, repair, admission and original-readback checks passed
52 tests (2 saved-case checks skipped) in 29.41 seconds. Two outdated CI assertions
about the previous pointer wording/schema were updated to the current exact-ID
contract; host membership checks remain strict.

There is no accepted release, final five-building PASS, deployment or Android
acceptance yet. Per-version failures, tests, usage bounds and remaining gates
are recorded in the [validation report](reports/product-recovery-validation-20261009.md).
Release acceptance must supply the same SHA for code/backend/evidence, independently
checked facts and canonical POI readback, first useful fact and full times,
actual sends/usage and measured costs where available. Billing totals currently
remain unknown.
