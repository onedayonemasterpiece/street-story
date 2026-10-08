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

First replay saved evidence and negative controls, including the wrong dormitory,
house 21 versus 22, water tower versus 121 and unresolved6/6A. Then use one frozen
candidate/configuration/manifest with at most five live cases:102,104,111,130,132;
122 is a reserve,121 a separate retained holdout. Cases 130/132 must establish geometry
before any external REF. Rich cases 102/104/132 require at least 3 substantive reviewed
facts; ordinary 111/130 at least 1. Verify sources and canonical POI readback, the
3/5/8 caps and at least one actual Live-first send. Run one emulator E2E only after
backend PASS. A mixed-SHA recovery, isolated identity, raw claims or manual research
does not pass this gate.

## Implementation status and remaining gate

The existing queues include accepted geometry/text proofs, common facts/POI
handling, bounded deadlines and strict original receipts. `prussia39.py`
implements both publisher forms, cp1251, card/body extraction, canonical cache
and observed pagination. The regional inventory change prepares received card
metadata alongside SOURCE/map preparation, exposes large and partial inventories
to the initial joint call, and reads only its 1–2 explicit physical nominations.
One actually observed continuation can complete 20+15 rows; distinct address rows
sharing one article ID remain visible, while article bodies are deduplicated.
The existing optional joint follow-up compares actual selected text; no third
mandatory selector/judge is added. Complementary selected Wiki text remains
available when the independently selected regional route is unavailable.

Optional early catalogue preparation is bounded to three seconds and uses only
an already received camera street or one unambiguous observed locality/street.
It does not wait for reverse geocoding or invent a target from a mixed pool.
A slow or incomplete publisher response remains a recorded limitation. A broad
inventory received only after the initial call does not trigger automatic
first-two selection or an additional paid judge. This preserves the operation
budget but does not demonstrate a cold text fast path for every facade.
The affected regional/Wiki/geometry/text offline set passed 102 checks, followed
by 38 affected checks after receipt/fence changes. These checks do not establish
live product acceptance.

The cold 104 canary on `c033e33` received all 12 regional cards before the joint
call, but the closed model plan failed schema validation. It ended naturally at
180.05 s with `identity_deadline_exceeded`, no accepted object and no eligible
facts. The remaining live cases were not started. Exact schema diagnostics and
contract correction must use the existing optional joint follow-up, retaining
the first invalid response and strict evidence validation; a second invalid
answer must not launch another planner chain. Late original-operation readback
is separate from acceptance and cannot restart an expired attempt.

A saved-source-only Live contract diagnostic on `3c6e492` completed in 10.66 s;
it does not establish product acceptance. No final five-case PASS or deployment
is claimed. Release evidence
must report the final source/deployed SHA, per-building facts/sources, first useful
fact and full times, actual sends/usage, known costs and remaining unknowns.
