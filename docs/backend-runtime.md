# Street Story backend · DevCoveer runtime

This is the deployment boundary for the backend source in `backend/`. It intentionally contains no Fly.io plan and no provider credentials.

## Runtime shape

- Python 3.12.
- One FastAPI/Uvicorn process with one worker because the durable job worker starts in application lifespan and SQLite is the local durable store.
- Persistent `DATA_DIR`, recommended `/var/lib/street-story`.
- SQLite WAL plus `synchronous=FULL`.
- A service supervisor using `Restart=always`; see `backend/deploy/street-story.service.example`.
- Reverse proxy/TLS in front of the local Uvicorn port; Android must receive HTTPS.
- VibePublish is a separate persistent DevCoveer service. Street Story uses only its bearer-protected HTTP API.

## Required runtime configuration

Create a Python 3.12 virtual environment, install `backend/requirements.txt`, place the repository at `/opt/street-story`, and configure the example systemd unit for the host. Populate `/etc/street-story.env` locally on DevCoveer; never commit the secret values.

Required owner-runtime values are:
- `STREET_STORY_DEVICE_TOKEN`
- `GEMINI_API_KEY`
- `VIBEPUBLISH_BASE_URL`
- `VIBEPUBLISH_BEARER_TOKEN`
- `DATA_DIR=/var/lib/street-story`

`GEMINI_MODEL` defaults to `gemini-3.1-flash-lite`. Keep an identifying OSM User-Agent.

## Restart/recovery semantics

Admission handlers commit durable state only. Long research and publish work is claimed from SQLite by the worker. A running job has a lease; an expired lease is reclaimed on restart. Per-chunk transcripts are persisted, so confirmed chunks are not retranscribed merely because the process restarted.

Publish intent and the VibePublish request key are committed before external admission. If a VibePublish HTTP response is lost, recovery repeats the same request key, never a new send. After the operation ID is known, recovery uses VibePublish status.

## Health boundary

`GET /healthz` is public liveness and should return an object containing `ok=true`. A successful liveness check is not proof that Gemini, VibePublish, Telegram or VK credentials are valid; authenticated `GET /v1/capabilities` is the runtime boundary for VibePublish destinations.

## Current visual blocker

Fresh VibePublish PR #1 head `87be8fcfca1229c419a5e0e47a8d68dacff5ea1f` exposes `GET /v1/assets/{id}` but no authenticated HTTP source-image ingress. Street Story therefore stops at durable `visual_blocked` / `vibepublish_media_ingress_not_enabled` after preserving the research result. Do not bypass this by copying into VibePublish SQLite or by introducing a second Telegram/VK/Imagegen implementation.

## VibePublish resident service

VibePublish must be supervised independently on DevCoveer and reachable through `VIBEPUBLISH_BASE_URL`. Its own deployment/runbook remains authoritative for provider credentials, native workers and Imagegen. Street Story stores none of those provider secrets.

## Gemini reliability and shared limits

See [Gemini P0 reliability](gemini-reliability.md) for multi-key settings and the
mandatory shared reserve / mark_sent / finalize contract. Local pool health alone
never authorizes a Gemini call. Configure controller credentials before restarting.
