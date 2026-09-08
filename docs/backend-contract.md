# Street Story backend contract · MVP

Status: Android client **and backend source** are implemented on `work/street-story-mvp-20260908`. Backend long-running deployment is a separate physical runtime step on DevCoveer, not a delegated source task.

## Deployment boundary

- **DevCoveer only** for Street Story backend and its persistent worker.
- **VibePublish must also run persistently on DevCoveer**, supervised separately but reachable by the Street Story backend.
- Do **not** use or plan Fly.io for Street Story or VibePublish.
- Python 3.12, FastAPI, SQLite WAL, persistent `DATA_DIR`; no PostgreSQL.
- HTTPS endpoint for the phone. A single revocable Street Story device bearer token is sufficient for the owner MVP.
- Gemini and VibePublish credentials are server-only. OSM/Wikipedia are accessed server-side. The APK contains none of these secrets.

## Durable story create

`POST /v1/stories`, multipart, authenticated with the Street Story device bearer token and an `Idempotency-Key`.

Parts:
- `photo`
- `client_story_id`
- `photo_sha256`
- `voice_protocol=voice-chunks-v2`
- optional `lat`, `lon`

Android also sends `X-Photo-SHA256`; when present it must agree with the multipart digest. The server hashes the received bytes before committing the asset.

The server binds the idempotency key and `client_story_id` to the exact photo digest and metadata. Same key + same payload returns the same story. Same key or client ID + different payload is `409`.

## Long voice protocol

The Android capture pipeline remains the proven Record Idea Hub profile: AAC-LC mono M4A, 16 kHz, 32 kbps, WebRTC VAD 2.0.10-cf.4, 30 ms frames, adaptive energy gate, pre-roll/hangover and durable chunks.

1. `POST /v1/stories/{story_id}/voice-sessions`
2. `PUT /v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{index}`
3. `POST /v1/stories/{story_id}/voice-sessions/{session_id}/complete`

Every mutation has an `Idempotency-Key`. Chunk upload is raw `audio/mp4` with `X-Content-SHA256`, audio start/end and wall start/end headers. Chunk identity is `(session_id, chunk_index, sha256)`.

Open/re-open returns:

```json
{
  "session_id": "voice-...",
  "recording_finished": false,
  "received": [{"index": 0, "sha256": "..."}]
}
```

The client starts every sync pass by re-opening the session and reconciling the server manifest. A lost HTTP response therefore never requires a new session or blind re-upload. `complete` atomically binds the exact ordered zero-based chunk manifest; a mismatch is `409 voice_manifest_mismatch`, never guessed.

Initial completion queues one durable research job. Refinement voice is uploaded identically; after durable completion Android calls `POST /v1/stories/{id}/refinements` with its `voice_session_id` and current `selected_fact_ids`, which queues a distinct refinement job. Persisted per-chunk transcripts are reused on worker restart/retry.

## Story representation

All story mutation endpoints return the current story representation, and `GET /v1/stories/{id}` returns the same shape:

```json
{
  "id": "story_...",
  "client_story_id": "story-...",
  "state": "researching",
  "place_name": null,
  "summary": null,
  "draft_text": null,
  "processed_image_url": null,
  "scheduled_for": null,
  "published_at": null,
  "revision": 1,
  "error": null,
  "facts": [],
  "destinations": []
}
```

Canonical states used by Android: `photo_ready`, `queued`, `researching`, `review`, `visual_processing`, `visual_blocked`, `ready_to_publish`, `scheduling`, `scheduled`, `published`, `needs_review`.

Facts contain stable `fact_id`, text, confidence, `evidence_supported`, selected and source objects with HTTPS URL. Unsupported facts always persist with `selected=false`. Refinement preserves the prior user selection when the stable `fact_id` survives.

## Research

The worker claims durable SQLite jobs with a lease. Expired `running` jobs are recovered after restart. Long HTTP requests do not execute research inside admission handlers.

Research pipeline:

1. independently transcribe missing M4A chunks with `gemini-3.1-flash-lite` and aggregate strictly by `chunk_index`;
2. bounded Nominatim reverse + bounded 250 m Overpass nearby lookup, identifying User-Agent and persistent cache;
3. MediaWiki geosearch + extracts + canonical URLs, with persistent cache;
4. send source photo, user transcript and structured place/source context to `gemini-3.1-flash-lite` using current `google-genai` and Google Search grounding;
5. persist compact candidate facts, grounding/source URLs and a publication draft.

Gemini is not the only source of truth. A model-proposed fact becomes `evidence_supported=true` only when its declared URL matches an external OSM/Wikipedia/Gemini-grounding source captured by the pipeline. No external evidence means `selected=false`.

## Visual boundary

`POST /v1/stories/{id}/visual` accepts `selected_fact_ids`. The backend persists selected facts, place context and user voice intent as structured visual context. The versioned server-side template is [`backend/prompts/street-story-image-v1.txt`](../backend/prompts/street-story-image-v1.txt), with an explicit owner placeholder for the final production prompt. Android stores no image prompt.

Fresh-read VibePublish PR #1 head on 2026-09-08: `87be8fcfca1229c419a5e0e47a8d68dacff5ea1f`.

Actual HTTP surface relevant to Street Story:
- `GET /v1/bootstrap`
- `POST /v1/visuals/commands`
- `POST /v1/publications`
- `GET /v1/operations/{id}`
- `GET /v1/assets/{id}`
- `Idempotency-Key` on mutations
- provider-native scheduling only (`delivery.kind=at` / `delivery.at`)

At this head there is **no supported authenticated HTTP source-image upload/import endpoint**. VibePublish `service._media()` accepts already verified `asset` refs and explicitly rejects URL/upload-ticket ingress with `media_ingress_not_enabled`. Therefore Street Story does not invent an endpoint, does not write VibePublish SQLite and does not implement a second Imagegen path. Visual work durably becomes:

```json
{
  "state": "visual_blocked",
  "error": {
    "code": "vibepublish_media_ingress_not_enabled",
    "message": "..."
  }
}
```

Source photo, transcript, research, facts, draft and visual structured context remain durable. A later explicit visual retry uses the same story.

## Destinations

`GET /v1/capabilities` fresh-reads VibePublish bootstrap and projects only destinations that have a real publish/post capability. Current VibePublish bootstrap does not expose a dedicated provider field, so Street Story assigns `telegram`, `vk` or `max` only when runtime alias/label makes the provider unambiguous; ambiguous entries are omitted rather than fabricated.

Actual bound **Полюбить Калининград — Telegram** and **Полюбить Калининград — VK** are preselected only when they exist with usable `supported`/`needs_review` capability. `Ух ты, Калининград` is shown only if an actual matching runtime destination exists; `needs_auth` remains visible but disabled by Android. Native IDs are never hardcoded.

## Publication

`POST /v1/stories/{id}/publish` accepts `destinations`, `delay_minutes` (MVP default 60), and optional `text_override`.

A verified visual/VibePublish asset is mandatory. Street Story computes the absolute timestamp and commits, **before any external effect**:
- one publish intent;
- its exact request payload;
- a stable VibePublish request/idempotency key;
- its reconciliation state.

The worker immediately submits `POST /v1/publications` with `delivery.kind=at` and provider-native `delivery.at`; Street Story has no publication timer. If the VibePublish response is lost after admission, Street Story re-admits the exact same payload with the exact same durable VibePublish `Idempotency-Key`. VibePublish's request-key replay returns the original operation instead of creating a second provider effect. Once an operation ID is known, Street Story reconciles via `GET /v1/operations/{id}`.

## Other endpoints

- `GET /v1/stories`
- `POST /v1/stories/{id}/facts`
- `POST /v1/stories/{id}/refinements`
- `POST /v1/stories/{id}/visual`
- `POST /v1/stories/{id}/publish`
- `GET /v1/assets/{story_id}/processed` (private, authenticated)
- `GET /v1/capabilities`
- `GET /healthz` (public liveness)

No local publication timer, no Telegram/VK/MAX credentials in Street Story, no Fly.io.
