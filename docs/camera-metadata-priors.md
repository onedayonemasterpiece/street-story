# Optional camera metadata in photo identification

> Historical October2 reference-priority implementation. Nearest-first16, unchanged4/6/6 batches and reference-only acceptance below are superseded by [Photo search methods](photo-search-methods.md). Standard EXIF validation, source provenance and privacy requirements remain; a missing heading/height/accuracy remains unknown.

Date: 2026-10-02. Extends `photo-identity-and-stop.md`; keeps the nearest-first
shortlist, visual evidence threshold and same-photo recovery intact.

## Observed owner photograph

The JPEG delivered in this conversation, `20261001_092216.jpg`, was inspected as
bytes: 907,325 bytes, 1536 x 2048 pixels, no EXIF IFDs. That is not evidence about
all metadata in the camera/gallery original.

The earlier file uploaded by Street Story for the same photographed scene is
3,723,354 bytes, 4000 x 3000 pixels, EXIF Orientation=6. Its GPS IFD contains only
tags 1-6 with invalid/redacted coordinates. It has neither GPSImgDirection nor
GPSImgDirectionRef, GPSHPositioningError or GPSTrack. Its camera IFD contains
FocalLength=9 mm, FocalLengthIn35mmFilm=72 mm, DigitalZoomRatio=3. These readings
are reproducible with the read-only `backend/tools/inspect_camera_metadata.py`
helper. It does not print GPS positions, credentials, a raw EXIF dump or image
bytes. Vendor MakerNotes are not interpreted as a compass.

For that file, no shooting azimuth can be used. Orientation=6 describes pixel
rotation, not the direction the lens faced. The 72 mm equivalent describes an
optical framing reference, not distance to the subject. Its 35 mm reference
DIAGONAL field of view is approximately 33.4 degrees; it is not an exact
horizontal FOV for a cropped/computational-phone photograph. The separate 3x
zoom is NOT multiplied into 72 mm (which would invent a 216 mm equivalent).

## Optional metadata contract

`camera_hints.read_camera_hints` reads only standard allowlisted fields from the
selected bytes: image direction/reference, horizontal positioning error, DOP,
focal length, 35 mm equivalent, digital zoom, pixel orientation and map-datum
compatibility. Non-finite/zero-denominator/out-of-range values are unavailable,
not converted to zero. GPSImgDirection=0 with a valid reference is north and
must not be mistaken for missing data. Broken GPS does not erase valid lens data.

Official tag definitions:
- https://developer.android.com/reference/androidx/exifinterface/media/ExifInterface#TAG_GPS_IMG_DIRECTION
- https://developer.android.com/reference/androidx/exifinterface/media/ExifInterface#TAG_GPS_IMG_DIRECTION_REF
- https://developer.android.com/reference/androidx/exifinterface/media/ExifInterface#TAG_GPS_TRACK
- https://developer.android.com/reference/androidx/exifinterface/media/ExifInterface#TAG_GPS_H_POSITIONING_ERROR
- https://developer.android.com/reference/androidx/exifinterface/media/ExifInterface#TAG_FOCAL_LENGTH_IN_35MM_FILM

T means true north; M means magnetic north. Only explicitly true-north directions
currently influence reference priority. Magnetic or unspecified reference is
recorded but not treated as true north; no geomagnetic dependency, network call,
assumed declination or live-phone compass is added. GPSTrack is movement direction,
not lens direction. GPSDOP is not a positioning error measured in metres.

## Small additional priority, not a new geographic filter

OSM nodes/representative way centres and already returned Wikipedia geosearch
positions supply a candidate bearing. No new Overpass query is necessary. The
Wikipedia cache version changes only to retain geosearch lat/lon; older per-topic
caches without these fields simply cannot supply a wiki bearing.

A direction is usable only when the camera position was verified from the photo
EXIF or the existing same-pixel, consented original-recovery path. An owner-named
map position is not assumed to be a camera coordinate. Bearings for targets
within 25 m or twice the reported horizontal error remain unknown because small
position/centre errors dominate. An explicitly unsupported map datum disables
this prior. Polygon centres are approximate and are never a visibility proof.

The candidate set and its nearest-first 4/6/6 batches do NOT change. Only the
order of fetching the initially limited references INSIDE the current batch is
biased: roughly ahead, then unknown, then off-axis, preserving distance order
inside each group. A wide 45-90 degree half-angle is a heuristic, not the actual
camera frustum. Every candidate remains in the model's original distance order.
Unknown or off-axis candidates may still win and receive targeted verification.
No distant batch jumps the queue. No physical line-of-sight, lens direction or
object distance is inferred from the image alone.

Lens parameters and approximate per-candidate angular difference are supplied to
the identification model with an explicit warning: these are weak priors, not
visual evidence. The existing actual-reference/observations/high-score early
exit, six-reference allowance and 60-second deadline are unchanged.

## Storage, privacy and rollout

Hints are bound to the immutable source photo hash inside the existing research
record. Same-photo GPS recovery keeps the recovered optional metadata even when
the server retains the original redacted source. Accepted identity is reused;
there is no background re-identification of already confirmed topics. Metadata
recovery replay does not increment the identity generation.

Telemetry uses the existing seven-day store: `identity_camera_metadata` records
availability, reason and lens summary; `identity_reference_priority` records
whether order changed and actual reference candidate IDs. No absolute compass
angle, GPS coordinates, gallery URI, capture timestamp, serial or MakerNote is
added to routine logs. Direction/position priors are computed server-side, not
by the Android client. No Android/WSS/auth/signing change is required.

Tests cover no EXIF, the observed 9/72/3 lens combination, true/magnetic/invalid
reference, north=0, wraparound, coincident positions, poor GPS accuracy, preserved
batch membership, unchanged early exit, actual provider payload/reference order,
recovery persistence and idempotent reopen. Synthetic tests establish contracts,
not measured real-world speed/accuracy improvements. Real heading-bearing owner
photo acceptance remains separate; the inspected photograph has no such heading.
