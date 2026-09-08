# Street Story backend contract · MVP

Status: Android client contract implemented on `work/street-story-mvp-20260908`. Backend implementation is intentionally delegated to the later Codex task.

## Deployment boundary

- **DevCoveer only** for Street Story backend and its persistent worker.
- **VibePublish must also run persistently on DevCoveer**, supervised separately but reachable by the Street Story backend.
- Do **not** use or plan Fly.io for Street Story or VibePublish.
- Python 3.12, FastAPI/Starlette, SQLite WAL, persistent `DATA_DIR`; no PostgreSQL.
- HTTPS endpoint for the phone. A single revocable Street Story device bearer token is sufficient for the owner MVP.
- Gemini, OSM/Wikipedia and VibePublish credentials are server-only. The APK contains none of them.

## Durable story create

`POST /v1/stories`, multipart, authenticated with the Street Story device bearer token and an `Idempotency-Key`.

Parts:
- `photo`
- `client_story_id`
- `photo_sha256`
- `voice_protocol=voice-chunks-v2`
- optional `lat`, `lon`

The server binds the idempotency key and `client_story_id` to the exact photo digest and metadata. Same key + same payload returns the same story. Same key or client ID + different payload is `409`.

## Long voice protocol

The Android capture pipeline is deliberately the proven Record Idea Hub profile: AAC-LC mono M4A, 16 kHz, 32 kbps, WebRTC VAD 2.0.10-cf.4, 30 ms frames, adaptive energy gate, pre-roll/hangover and durable chunks. Chunks normally close at 180 seconds and may close earlier after a long silence.

1. `POST /v1/stories/{story_id}/voice-sessions`
2. `PUT /v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{index}`
3. `POST /v1/stories/{story_id}/voice-sessions/{session_id}/complete`

Every mutation has an `Idempotency-Key`. Chunk upload is raw `audio/mp4` with `X-Content-SHA256`, audio start/end and wall start/end headers. Open/re-open returns:

```json
{
  "session_id": "voice-...",
  "recording_finished": false,
  "received": [{"index": 0, "sha256": "..."}]
}
```

The client starts every sync pass by re-opening the session and reconciling the server manifest. A lost HTTP response therefore never requires a blind re-upload or a new recording. `complete` must atomically bind the exact ordered chunk manifest; a mismatch is reconciliation-required, never guessed.

Initial voice completion queues research. A refinement voice session is uploaded identically; after durable completion Android calls `POST /v1/stories/{id}/refinements` with its `voice_session_id`.

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

Facts contain stable `fact_id`, text, confidence, `evidence_supported`, selected and source objects with HTTPS URL. Unsupported facts default selected=false.

## Research

Worker survives restart and resumes durable jobs. Research uses:
- EXIF coordinates supplied by Android when available;
- bounded/cache-aware OSM/Nominatim reverse + nearby lookup;
- MediaWiki geosearch/extracts and source URLs;
- `gemini-3.1-flash-lite` using current `google-genai` for chunk transcription, photo/context synthesis, Google Search grounding, evidence-aware fact ranking and draft text.

Gemini is not the only source of truth. A fact without external evidence cannot default into publication.

## Visual and publication

`POST /v1/stories/{id}/visual` accepts `selected_fact_ids`. Prompt text is backend-side and versioned. The final image prompt is intentionally a separate file with an obvious placeholder for the owner/Codex task.

Street Story must integrate the **actual VibePublish PR #1 HTTP contract** observed at head `196d302fe0f39326ced4767c9afae58d74ee37ec`:
- `GET /v1/bootstrap`
- `POST /v1/visuals/commands`
- `POST /v1/publications`
- `GET /v1/operations/{id}`
- `Idempotency-Key` on mutations
- provider-native scheduling only (`delivery.kind=at` / `delivery.at`)

Do not invent VibePublish upload/publish APIs. Current VibePublish media ingress accepts verified assets and its real Imagegen executor may still be unavailable. If the real visual runtime is absent, persist the story as `visual_blocked`; never substitute a fake production generator. Retry later resumes the same story.

`GET /v1/capabilities` asks VibePublish bootstrap and projects only real bound destinations. For MVP, preselect actual bound destinations whose labels correspond to **Полюбить Калининград — Telegram** and **Полюбить Калининград — VK** when they really exist. `Ух ты, Калининград` appears only if a real matching VibePublish destination exists. Android does not hardcode or fabricate aliases.

`POST /v1/stories/{id}/publish` accepts `destinations`, `delay_minutes` (MVP=60), and optional `text_override`. Backend computes an absolute time and immediately submits a provider-native scheduled publication to VibePublish. No Street Story publication scheduler. Persist the VibePublish operation/request key before external effect, then reconcile the same operation after crash or lost response.

## Other endpoints

- `GET /v1/stories`
- `POST /v1/stories/{id}/facts`
- `POST /v1/stories/{id}/refinements`
- `POST /v1/stories/{id}/visual`
- `POST /v1/stories/{id}/publish`
- private authenticated asset endpoint returned as a backend-relative `processed_image_url`
- `GET /v1/capabilities`
- `GET /healthz`

No local publication timer, no Telegram/VK/MAX credentials in Street Story, no Fly.io.
