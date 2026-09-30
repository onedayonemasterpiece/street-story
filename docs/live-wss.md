# Street Story Live WSS — current integration

Date: 2026-09-30. This document supersedes the old HTTP-audio batching guidance
for the migrated Android client. Release status: candidate pending real consumer
acceptance. Test results and deployed SHA belong in acceptance receipts, not in
claims inferred from source code.

## Runtime decision

Keep the existing single Python FastAPI backend and SQLite domain store. Do not
port Street Story to Node solely to reuse voice, and do not add a Node gateway.
This is not a claim that a Node port is difficult: it is feasible before owner
adoption. The deciding comparison includes KenigEvents, whose Live-search host
is also Python/aiohttp, and the common model provider, which is already Python.
Porting Street Story alone does not unify those components. A Node sidecar adds
a permanent deployment/IPC failure boundary without removing the Python domain.

Instead use one versioned live-interaction package with Python/Node transport
bindings and cross-platform conformance tests. Python is not faster by assumption;
we have not measured a like-for-like resource benchmark. We accept the explicit
maintenance cost of two small socket bindings, not two product voice engines.
Node Wonderful Lections stays on its separately accepted release while these
Python/native consumers are verified. No shared running process or database is
introduced across products.

The complete cross-product rationale and contract are in the pinned framework's
`docs/native-wss.md`. Future fixes to framing, tickets, queues and voice lifecycle
belong there. Product repositories keep only authorization, tools/context and UI.

## What changed

Android consumes the shared Java WSS transport from the immutable archive named
in `live-framework.lock.json`; Gradle verifies its SHA-256 and generates the SDK
source set through `scripts/prepare_live_framework.py`. The backend installs the
same version with the same archive checksum. No copied editable SDK fork.

Authenticated HTTPS session bootstrap now returns `socket_url` (same-origin
relative path), one-use `socket_ticket`, `attempt_id` and `transport_protocol`.
The ticket expires after 15 seconds and is offered only as WebSocket subprotocol
`wl-ticket.<ticket>` with `wl-live-v1`. URL credentials, foreign origins, wrong
resources, stale generation and reused tickets fail before audio admission.

The socket carries 100 ms PCM batches and pushed model events/binary output.
There is no audio HTTP POST or events poller in the migrated controller. The old
HTTP endpoints are retained only for explicit compatibility; a session that
attached WSS cannot silently return to HTTP input. JSON ping/pong keeps liveness.

Manual speech boundaries follow existing VAD: activity_start -> PCM ->
activity_end. ACK means server relay admission, not model comprehension. Queued
and unacknowledged PCM is bounded; expired speech is not replayed. Provider gap
input is marked damaged until a clean later speech boundary reaches the provider.
The native candidate uses explicit restart, not automatic reconnect or startup
recording before hello acknowledgement. Setup is shown before “Слушаю”.

The client separates transport and playback generations. Provider/transport
failure finishes the durable microphone archive without flushing already
received PCM. Explicit user Stop still interrupts/clears playback immediately.
Errors and long waits distinguish transport, provider, tools and resource limits;
routine diagnostic records contain identifiers/counters/codes, not audio, full
transcripts, tickets, device tokens or provider credentials.

## Preserved domain boundaries

One existing story/photo, resolve_place -> owner confirmation -> grounded
search_web, independent text/image revisions, immutable reviewed visual, test
Telegram-only destination and exact two-step publication confirmation remain.
No raw provider calls move to Android. No PostgreSQL, broker, new service or
alternative credential path is introduced. The fixed image-v2 safe layout and
FIT_CENTER preview are unchanged.

## Verification and rollout

Run backend pytest including test_live_socket.py and the archive integrity gate.
Run Android unit tests/lint/APKs/emulator; the existing LiveGoldenInstrumentedTest
must assert WSS and preserve transport evidence. Reuse the owner Zakheim fixture.
Verify the public TLS reverse proxy actually upgrades the socket; /healthz alone
is not proof. Exercise real prepared PCM, grounded internet search and provider
readback before claiming product readiness. Prepared PCM is not a physical-mic
test. Keep native scheduling/cancellation separate from visible-publication proof.

The existing VibePublish cancellation-policy failure must not be relabelled as
a WSS or model success. Never weaken production destination/consent checks merely
to make the acceptance test green. Candidate rollout is test-only until receipts
show the end-to-end result. Rollback changes the immutable release, not SQLite,
device token or publication records. Future updates use semantic versions and
explicit per-consumer acceptance, never live-session auto-update.
