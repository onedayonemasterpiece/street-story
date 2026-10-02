# Selected-photo GPS, persistent identity and immediate Live Stop

Date: 2026-10-01. Incident: `inc_181ca71615174ed50b347a31`.
This document describes implemented behavior and remaining acceptance boundaries;
it is not a claim of a successful physical-phone retest.

## Observed defects

In the owner's 0.1.438 test, the uploaded photo had a GPS IFD, but its latitude /
longitude reference strings were NUL and its fractions were not usable. Both
coordinate fields in the stored story were NULL. This establishes that the
uploaded copy was GPS-redacted/invalid, not that the gallery original lacked GPS.
The old Android import did not request ACCESS_MEDIA_LOCATION or an unredacted
selected original. See the official Android shared-media documentation:
https://developer.android.com/training/data-storage/shared/media#location-info-photos
and MediaStore.setRequireOriginal / getMediaUri:
https://developer.android.com/reference/android/provider/MediaStore

The same run completed model speech, but subsequent Stop/Start interactions
created multiple new sessions. The connecting state was not considered an active
interaction by the mic toggle. Stop also depended on a recording archive being
present, and archive finalization could join its thread on the main/UI thread.

## Import and recovery

Declare and request ACCESS_MEDIA_LOCATION with an explanation about metadata in
the chosen photo, not current device location. Use a single ACTION_OPEN_DOCUMENT
selection. When supported, convert the granted URI with MediaStore.getMediaUri
and request MediaStore.setRequireOriginal. Do not derive internal provider IDs,
scan the gallery, request current-location permission, or bypass a denied grant.
If the provider cannot supply an original, read only the user's selected URI and
report the limitation. Files exported by messengers or cloud providers may still
have no GPS even with permission.

Copy the selected bytes without re-encoding, preserve EXIF orientation, use
ExifInterface Double coordinates, enforce 16 MB and coordinate validity, and
record whether originals/permission/GPS were available. The server independently
parses GPS from the supplied file when coordinate form fields are missing. Invalid
NUL refs, non-finite fractions and out-of-range values are not coordinates. Never
substitute (0,0), the current phone position or an assumed landmark location.

For an existing topic with missing GPS, the button “Прочитать GPS из оригинала
фото” asks for the same camera original. The authenticated recovery endpoint checks
the original topic photo hash and exact oriented decoded RGB pixels before
accepting GPS. It changes only location/provenance and identity generation; the
photo, story ID, notes and recordings are retained. A different, edited or
recompressed image fails closed. Repeating the same successful recovery is
idempotent. No GPS can be reconstructed from already redacted bytes alone.

## Candidate selection and visual decision

GPS is the camera position. Preserve the existing bounded geographic retrieval:
close buildings/POIs and separate landmark/Wikipedia coverage, rather than flooding
the model with hundreds of shops. Within that suitable shortlist (maximum 16),
order candidates by actual approximate distance, nearest first. Reverse geocoding
no longer pretends its result is zero metres away. Distance is a prior, not visual
proof, and representative polygon centres are approximate. The fixed radius does
not calculate physical line of sight or guarantee every visible object is mapped.

Compare batches of 4, 6, then 6 candidates. Fetch references lazily for the current
batch, initially at most two, rather than downloading every candidate's image.
A promising candidate whose reference was not sent gets one targeted reference
verification before moving farther. All passes share six references and a
60-second deadline (at most three batch and three targeted comparisons).
The source photo is EXIF-normalized and resized for model input; the
stored original is not changed. Total visual evaluation has a 60-second deadline.

Stop early only for a valid candidate ID, model match with score >=0.90, nonempty
visual observations, no competing alternative, and an actual reference image for
that candidate included in the request. This is an engineering threshold, NOT a
calibrated 90% probability. Model-authored claims that a reference was sent are not
accepted: the host records the actual transmitted reference IDs. Missing images,
lookalikes, ambiguous results or exhausted budget produce an uncertain result,
not a forced nearest-building match or a mandatory request for the author to name
the object. The UI presents the candidate, observations and missing evidence;
voluntary correction is separate from publication consent.

### Reference delivery and partial views (2026-10-02)

The v449 Royal Gate trace fetched an original successfully (HTTP 200), but rejected
it against the former 2 MiB input limit. The model consequently saw no reference.
A reference download now has a separate bounded 12 MiB budget and 40-million-pixel
decode limit. It is EXIF-normalized and fitted, without cropping, to a 1280-pixel
JPEG capped at 2 MiB before model input. Decode work is off the event loop.
The existing HTTPS host/redirect checks, six-image budget, deadline, cache and
Retry-After behavior remain. No new service or database is introduced.

A successful comparison records the actual reference URL, normalized byte count
and SHA-256 alongside the source-photo hash. A proposed URL or the model's own
claim of having seen a photo is not this receipt. The comparison prompt treats
partial framing and another viewpoint as normal: a feature outside the frame is
not a contradiction; visible distinctive details must match. Observations are
requested in Russian.

Wikipedia text and its Wikimedia illustration are related evidence, not two
independent votes. Geography selects candidates; the article establishes an
object record; actual visual agreement selects the object. A second source or
angle is useful only when it resolves a remaining ambiguity. Confidence alone
does not replace any of these checks.

### Private corpus procedure

`tools/owner_topic_corpus.py` acquires only the authorized forum topic through
the standard Telegram E2E launcher, preserving original bytes and hashes.
`tools/identity_corpus.py` runs the real resolver without workers, generation or
publication in a retained private artifact directory; it uses the same provider
configuration and resource control as the deployed application. Completed cases
are not silently rerun. A changed source snapshot requires a new run name.
Expected labels are reviewed independently and never enter model input.

The acquired corpus contains 14 original image documents, messages 7–20.
GPS metadata is present in 13; message 15 has no GPS metadata. This is a distinct
case, not permission to substitute guessed coordinates. Full corpus accuracy,
latency and physical-phone acceptance are recorded only after execution.

## Durable binding and correction

Persist the selected candidate ID/name/public source URL, source photo hash,
identity generation, timestamp, observations and visual-proof status in the
existing story research record. Accepted identity and completed uncertain results
are reused on reopen; a screen refresh must not rerun identification. Jobs and
Live tools use the same per-story serialized resolver. Late results compare the
photo hash/generation and cannot overwrite an author's newer confirmation.

The Live tool `reject_place` is for an explicit author correction (“не тот объект”).
It checks the current candidate, stores a bounded rejection/history record,
increments generation and reruns the remaining candidates. Exclusions are applied
BEFORE the 16-candidate cap, so an alternative previously outside the shortlist can
enter it. Geographic results already obtained are reused, not downloaded on every
correction. An absent mapped alternative remains uncertain; it is not invented.

Old facts are deselected/unverified, pending publication confirmations invalidated,
and old visual content revision made stale. Ready/retry research/visual jobs are
cancelled; late research writes are generation-checked. Existing draft text is
retained but requires review/update after identity changes before publication.
Scheduled/published stories cannot be silently rebound; their publication must be
handled explicitly. Neither correction nor recovery publishes anything.

## Stop and telemetry

The mic button treats connecting/reconnecting as cancellable states. It clears
local activity and advances the cancellation epoch immediately, irrespective of
archive existence or server response. Late bootstrap/readiness cannot restart the
mic. Close transport callbacks outside the controller lock. Stop hardware capture
before waiting for archive finalization; that wait occurs on a background thread.
Explicit Stop does not start another session and suppresses auto-start resurrection.

Reuse the existing seven-day diagnostic store. Pre-Live identity/import events
have an empty session_id and a real story_id. Record import permission/read mode,
GPS/tag validity, photo hash, candidate IDs/distances, batch/reference IDs, scores,
early exit, cache reuse, rejection, generation and stage timing. Client start/stop
can be diagnosed before a Live session exists. During capture, five-second RMS /
peak/sample/VAD summaries help distinguish low input level from VAD rejection.
Routine records do not accept a raw gallery URI, precise EXIF coordinates, audio,
image bytes or credentials. Existing owner-authorized transcript diagnostics remain.

In stress testing the added reads exposed SQLite connection descriptors remaining
open until garbage collection. Store connections now close on context exit while
preserving transaction behavior; a 600-read test with garbage collection disabled
verifies bounded descriptor usage. No database/service/queue is added.

## Acceptance

Backend tests cover EXIF redaction, original recovery, wrong-image refusal,
authorized/bounded HTTP diagnostics, no repeat identification, nearest-first early
exit, bounded uncertain evaluation, correction alternatives and stale-result
protection. Android instrumentation covers EXIF byte preservation/Double GPS,
manifest/diagnostic policy, and the actual floating button cancelling a simulated
connecting state without a recording archive. Canonical signing and previous-APK
in-place update gates are unchanged.

Physical Samsung gallery/provider permission behavior and subjective audio quality
still require owner retesting. Generated GPS fixtures and simulated connecting
states are not a substitute for that evidence. Public provider timeouts/quotas
remain visible and are not bypassed by alternative credential selection.

## Autonomous recovery after the geographic shortlist (2026-10-02)

The geographic shortlist remains the cheap first pass. If it produces no visually
verified match, one bounded recovery pass starts automatically; the author is not
asked to supply the name of the object.

The recovery pass uses the source photo to produce search hypotheses, but a model
hypothesis is never accepted as evidence. It performs three complementary searches:

1. up to two proper-name queries in Russian Wikipedia;
2. one query made from visible physical features plus the product region hint
   (Калининградская область in the current regional MVP), deliberately independent
   of the guessed name;
3. a matching English visible-feature query in Wikimedia Commons.

Fetched Wikipedia pages, Commons files and Commons categories become candidates
only after the provider actually returns them. Files within one Commons category,
Wikipedia aliases and modern/historical names are clustered as one physical object
when they share the same original media, a specific Commons/heritage entity key, or
an explicit category-member relationship. Wikipedia is preferred as the display
name when it represents the same physical cluster; Commons remains independent
visual evidence rather than a second object. A cluster may send two different
views under the existing global six-reference budget.

Wikimedia originals are normalized by trying a bounded same-host 1280 px thumbnail
first and the original only when needed. This removes the former >2 MiB failure
without accepting arbitrary redirect hosts or cropping the reference. Every image
actually passed to the model gets a SHA-256/byte-count/source receipt. The visual
gate still requires a real transmitted reference for the selected cluster, concrete
observations, score >=0.90 and no genuinely different competing object. Duplicate
aliases of the same structure no longer veto an otherwise valid match.

Visual identification and visual-query generation use the configured research-model
route with the same bounded key/quota control as other research calls. A temporary
quota/model failure therefore fails over from the primary research model to the
configured research fallback instead of retrying the same unavailable route or
changing credentials.

The private regression corpus exposed the reason for these changes. An early run
verified only 5 of 14 deliberately imperfect photos. After thumbnail-first delivery,
entity clustering and recovery search, a previously missed partial view of the
Zelenogradsk water tower was independently accepted with two delivered references.
The GPS-free case also demonstrated the important fail-closed property: plausible
but wrong churches remained uncertain; no nearest or confident model guess could
cross the visual-proof gate. Full-corpus metrics are recorded from a fresh immutable
run before release rather than inferred from individual debugging examples.
