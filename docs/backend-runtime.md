# Street Story backend · DevCoveer runtime

This is the deployment boundary for the backend source in `backend/`. It intentionally contains no Fly.io plan and no provider credentials.

## Runtime shape

- Python 3.12.
- One FastAPI/Uvicorn process with one in-process durable worker; SQLite is the local source of truth.
- Persistent `DATA_DIR`, recommended `/var/lib/street-story`.
- SQLite WAL plus `synchronous=FULL`.
- Service supervisor with restart policy; see `backend/deploy/street-story.service.example`.
- Reverse proxy/TLS in front of local Uvicorn; Android receives HTTPS only.
- VibePublish is a separate persistent DevCoveer service. Street Story uses only its bearer-protected HTTP API.

## Required runtime configuration

Create a Python 3.12 environment, install `backend/requirements.txt`, place the repository at `/opt/street-story` (or equivalent exact checkout), and configure the systemd example. Populate runtime secrets locally; never commit them.

Required values:
- `STREET_STORY_DEVICE_TOKEN`
- `GEMINI_API_KEY` / configured shared Gemini pool credentials
- `VIBEPUBLISH_BASE_URL`
- `VIBEPUBLISH_BEARER_TOKEN`
- `DATA_DIR=/var/lib/street-story`

`GEMINI_MODEL` defaults to `gemini-3.1-flash-lite`. Keep an identifying OSM User-Agent.

## Exact source SHA gate

`GET /healthz` returns `ok=true` and `source_sha`. For PR Live E2E, `source_sha` must equal the exact current PR HEAD. A missing/mismatched SHA is a deployment failure and the workflow stops before product effects. Do not disable this gate and do not test a newer source harness against an older deployed backend.

When redeploying a source checkpoint, update **both Street Story runtime endpoints** to the same exact checkout, set `STREET_STORY_DEPLOY_SHA` when the deployment mechanism uses it (or otherwise ensure checkout-derived SHA is authoritative), restart the service, then read back `/healthz.source_sha` from both endpoints before dispatching Live E2E.

## Restart/recovery semantics

Admission handlers commit durable state only. Research, visual, publication and cancellation are leased SQLite jobs. Expired `running` jobs are reclaimed on restart. Per-chunk transcripts are persisted, so confirmed chunks are not retranscribed merely because the process restarted.

Raw ASR and cleaned `display_text` are persisted separately. Restart/readback does not recompute cleaned owner text once stored.

VibePublish identity is committed/recoverable across every external effect:

- source image ingress uses a stable idempotency key and immutable source SHA;
- visual command and candidate selection use stable keys and operation readback;
- publication intent stores the exact VibePublish request key before external admission;
- cancellation stores a stable command key and reconciles the existing publication revision.

Lost HTTP responses therefore repeat the same semantic key rather than create a new asset, visual job, publication or cancellation.

## VibePublish requirement

Runtime must expose the VibePublish lineage represented by public commit `dec1c69920f09ebdc0551132e6bdffba73d03e8e` or a compatible newer contract, including:

- authenticated `POST /v1/assets` source-image ingress;
- authenticated asset readback;
- visual command + official selection + operation reconciliation;
- publication scheduling/status;
- publication cancel command/status.

The previous Street Story `visual_blocked / vibepublish_media_ingress_not_enabled` boundary is obsolete. Do not reintroduce it by pinning an older VibePublish runtime. Also do not bypass the contract via VibePublish SQLite, a second Imagegen, or direct Telegram/VK publication.

## VibePublish resident service

VibePublish is supervised independently on DevCoveer and reachable through `VIBEPUBLISH_BASE_URL`. Its own deployment/runbook remains authoritative for provider credentials, native workers and Imagegen. Street Story stores none of those provider secrets.

## Gemini reliability and shared limits

See [Gemini P0 reliability](gemini-reliability.md). Shared reserve / mark_sent / finalize remains authoritative; local pool health alone does not authorize a call. A real quota/rate error remains a durable retry/backoff condition and is exposed to Live E2E as evidence. It must never be converted into fake facts or a false green run.

## Live E2E acceptance

The smoke mode is reversible up to provider publication and must prove:

1. exact deployed source SHA;
2. unique story with real photo fixture + GPS;
3. durable AAC/M4A initial voice manifest and replay reconciliation;
4. raw + cleaned display transcript;
5. OSM, Wikipedia and grounded Gemini provenance;
6. evidence-backed fact selection;
7. a second refinement for the same story preserving selected stable fact toggles;
8. VibePublish source asset ingress and same-key replay identity;
9. real visual operation, candidate selection, verified processed asset SHA readback;
10. final draft and real Telegram/VK destination projection.

`full_social` is accepted only when `SAFE_TEST_DESTINATION_ALIAS` identifies an explicitly test/safe/e2e destination returned as `supported`. It then proves provider-native schedule → provider/status readback → cancel → confirmed cancelled/readback. If the safe destination or cleanup capability is absent, full social fails closed and must not be reported as accepted.
