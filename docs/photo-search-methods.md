# Photo search: concrete target methods

Target dated 2026-10-08. The [complete unified brief](prompts/street-story-unified-photo-search-20261008.md)
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

Fill free worker slots as results complete under existing resource admission.
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
or one unambiguous observed locality/street and is bounded to three seconds.
Late or partial cards remain visible, but do not authorize automatic first-two
selection or another mandatory judge. Independent text fallback preserves the
received packet; its Native addressed prompt can still be large, and Live's
24 KB planning limit remains a real limitation.
Early catalogue preparation can miss observed cold lookups taking 5–15 seconds.
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
new live fact acceptance remains unverified.
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

There is no accepted release, final five-building PASS, deployment or Android
acceptance yet. Per-version failures, tests, usage bounds and remaining gates
are recorded in the [validation report](reports/product-recovery-validation-20261009.md).
Release acceptance must supply the same SHA for code/backend/evidence, independently
checked facts and canonical POI readback, first useful fact and full times,
actual sends/usage and measured costs where available. Billing totals currently
remain unknown.
