# Street Story backend contract · MVP

Status: the Android client and backend source are implemented on `work/street-story-mvp-20260908`. Deployment/redeployment is a separate physical runtime step on DevCoveer and must use the exact source SHA exposed by `/healthz`.

## Deployment boundary

- **DevCoveer only** for Street Story backend and its persistent worker.
- **VibePublish also runs persistently on DevCoveer** and is reached only through its authenticated HTTP API.
- Do not use or plan Fly.io for Street Story or VibePublish.
- Python 3.12, FastAPI, SQLite WAL, `synchronous=FULL`, persistent `DATA_DIR`; no PostgreSQL.
- HTTPS endpoint for Android; one revocable Street Story device bearer token is sufficient for the owner MVP.
- Gemini/VibePublish/social credentials are server-only and never enter the APK.

## Durable story create

`POST /v1/stories`, multipart, authenticated with the Street Story device bearer token and `Idempotency-Key`.

Parts: `photo`, `client_story_id`, `photo_sha256`, `voice_protocol=voice-chunks-v2`, optional `lat`, `lon`. Android also sends `X-Photo-SHA256`; when present it must agree with the multipart digest. The server hashes the received bytes before committing the source photo.

The server binds idempotency key + client story ID to the exact photo digest and metadata. Same key/same payload returns the same story. Same key or client ID with different content is a conflict.

## Long voice protocol

The capture pipeline remains the proven profile: AAC-LC mono M4A, 16 kHz, 32 kbps, WebRTC VAD 2.0.10-cf.4, 30 ms frames, adaptive energy gate, pre-roll/hangover and durable chunks.

1. `POST /v1/stories/{story_id}/voice-sessions`
2. `PUT /v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{index}`
3. `POST /v1/stories/{story_id}/voice-sessions/{session_id}/complete`

Every mutation has an `Idempotency-Key`. Chunk upload is raw `audio/mp4` with `X-Content-SHA256` plus audio/wall time headers. Chunk identity is `(session_id, chunk_index, sha256)`. Open/re-open returns the durable received manifest; Android reconciles this before uploads so a lost response never creates another semantic chunk/session. Complete atomically binds the exact ordered zero-based manifest.

Initial completion queues durable research. Refinement voice uses the same upload/manifest protocol; after completion Android calls `POST /v1/stories/{id}/refinements` with `voice_session_id` and the current stable `selected_fact_ids`.

### Raw transcript vs feed text

Each voice/refinement session persists two distinct values:

- `raw_transcript`: diagnostic/research source truth from ASR;
- `display_text`: a one-time, restart-safe conservative normalization for UI.

`display_text` removes fillers, immediate repeated words/syllables and false starts while preserving the actual request, uncertainty, names and detail. It does not add facts or summarize away meaning. A story/API re-read must not retranscribe or renormalize a completed session merely because Android was recreated.

## Story representation / feed projection

`GET /v1/stories/{id}` and mutation responses project the durable story for the unified Android feed. Relevant fields include:

```json
{
  "id": "story_...",
  "client_story_id": "story-...",
  "state": "review",
  "place_name": "...",
  "summary": "...",
  "draft_text": "...",
  "processed_image_url": null,
  "revision": 4,
  "voice_messages": [
    {
      "session_id": "voice-...",
      "kind": "initial",
      "raw_transcript": "...",
      "display_text": "..."
    }
  ],
  "facts": [],
  "visual": {},
  "destinations": [],
  "publication": null,
  "research_provenance": {
    "osm_present": true,
    "wikipedia_page_count": 2,
    "grounded_source_count": 3
  }
}
```

Android renders only the latest 10 stories, ordered as a vertical thread with newest at the bottom. Older stories are retained; no archive/delete contract is introduced by the MVP.

Canonical states remain `photo_ready`, `queued`, `researching`, `review`, `visual_processing`, `visual_blocked`, `ready_to_publish`, `scheduling`, `scheduled`, `published`, `needs_review`. `visual_blocked` remains a recoverable legacy/error UI state, but the former `vibepublish_media_ingress_not_enabled` state is no longer an accepted normal product boundary.

## Research / facts

The worker claims durable SQLite jobs by lease and recovers expired `running` work after restart. The research path is:

1. transcribe missing M4A chunks with the configured Gemini pool, strictly by `chunk_index`;
2. bounded Nominatim reverse + Overpass nearby lookup with persistent cache;
3. MediaWiki geosearch/extract/canonical URLs with persistent cache;
4. grounded Gemini research over source photo, owner voice and structured source context;
5. persist candidate facts, evidence URLs, summary and publication draft.

Facts have stable `fact_id`, `evidence_supported`, selection state and source objects. Unsupported facts are always unselected/disabled. Refinement preserves the owner toggle when the stable fact survives. Runtime readback exposes bounded research provenance and durable retry evidence; quota/rate retry is not converted into fabricated facts.

## Visual boundary · VibePublish

Supported lineage: VibePublish commit `dec1c69920f09ebdc0551132e6bdffba73d03e8e` or compatible newer deployment.

Street Story uses the actual supported HTTP boundary:

- `GET /v1/bootstrap`
- authenticated binary `POST /v1/assets` with `Idempotency-Key`
- authenticated `GET /v1/assets/{asset_id}`
- `POST /v1/visuals/commands`
- `GET /v1/operations/{operation_id}`

`POST /v1/stories/{id}/visual` persists the selected fact set and queues a durable visual job. The worker:

1. reads the private source photo and submits `POST /v1/assets` using a stable key derived from story/photo identity;
2. verifies returned `source_sha256` against Street Story's source hash;
3. replays the same ingress key on recovery; the runtime acceptance boundary verifies same-key replay returns the same immutable asset identity;
4. submits a real `tune` visual command referencing only the VibePublish asset ID;
5. reconciles the operation until a candidate is available;
6. uses VibePublish's official `select` command (`job_id`, `candidate_id`, `expected_revision`, selection token) with a stable key;
7. requires `verified`, `selected_asset_ref`, `selected_sha256`;
8. reads the selected asset through VibePublish, verifies the SHA over bytes, and durably stores a private Street Story processed-image readback.

Street Story does not write VibePublish SQLite, does not implement a second Imagegen, and does not bypass VibePublish with direct Telegram/VK publishing.

## Destinations

`GET /v1/capabilities` fresh-reads VibePublish bootstrap. Street Story projects only real publish/post destinations and prefers explicit provider data returned by VibePublish; alias/label inference is only a compatibility fallback.

The MVP primary targets are **Полюбить Калининград / Telegram** and **Полюбить Калининград / VK** when actually returned. `supported` and `needs_review` are distinct capability statuses; `needs_review` is not treated as “channel absent”. `Ух ты, Калининград` is shown only when returned by VibePublish. Native provider/channel IDs are never hardcoded.

## Publication and cancellation

`POST /v1/stories/{id}/publish` accepts `destinations`, `delay_minutes` (Android default 60) and optional `text_override`. A verified VibePublish visual asset is mandatory. Street Story commits the publish intent, exact request, selected asset ref and stable VibePublish request key before external admission.

The worker submits `POST /v1/publications` with provider-native `delivery.kind=at` / absolute `delivery.at`. Street Story has no publication timer. Lost response recovery uses the same VibePublish key; once the operation ID is known, status is reconciled through `GET /v1/operations/{id}`. Provider rows shown by Android are projections of VibePublish receipts/status, not locally invented state.

`POST /v1/stories/{id}/cancel` queues a durable cancellation for a scheduled VibePublish publication. It uses the supported publication command boundary with `expected_revision` and `change={"kind":"cancel"}`, then reconciles until publication state and provider delivery read back `cancelled`.

## Other endpoints

- `GET /v1/stories`
- `POST /v1/stories/{id}/facts`
- `POST /v1/stories/{id}/refinements`
- `POST /v1/stories/{id}/visual`
- `POST /v1/stories/{id}/publish`
- `POST /v1/stories/{id}/cancel`
- `GET /v1/assets/{story_id}/processed` (private authenticated processed asset)
- `GET /v1/capabilities`
- `GET /healthz` (public liveness + exact source SHA)

No local publication timer, no provider credentials in Street Story, no Fly.io.
