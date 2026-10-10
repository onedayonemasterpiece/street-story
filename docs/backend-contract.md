# Street Story backend contract · MVP

Status: the Android client and backend source are implemented on `work/street-story-mvp-20260908`. Deployment/redeployment is a separate physical runtime step on DevCoveer and must use the exact source SHA exposed by `/healthz`.

## Deployment boundary

- **DevCoveer only** for Street Story backend and its persistent worker.
- **VibePublish also runs persistently on DevCoveer** and is reached only through its authenticated HTTP API.
- Do not use or plan Fly.io for Street Story or VibePublish.
- Python 3.12, FastAPI, SQLite WAL, `synchronous=FULL`, persistent `DATA_DIR`; no PostgreSQL.
- HTTPS endpoint for Android; one revocable Street Story device bearer token is sufficient for the owner MVP.
- Gemini/VibePublish/social credentials are server-only and never enter the APK.

## Current product path · Live-first topic editor

The current owner flow is a list of topics → one Topic Detail → iterative conversation over the same visible result
(image + text) → publication. A topic can stay open for many Live turns; there is no infinite feed interaction model.

Street Story uses application-owned VAD as the speech-activity authority. Live sessions therefore enable the shared framework's `manual_activity_detection`: each VAD speech segment is sent as `activity_start` → ordered bounded PCM16/16 kHz → `activity_end`. Provider auto-VAD is disabled for these sessions, so the same contract works for physical microphone capture and already-durable prepared PCM without inventing a second transport or using `audio_stream_end`.

The Android app bootstraps the authenticated Live session over HTTPS and then keeps speech/events on WSS:

- `POST /v1/stories/{story_id}/live-sessions` — authenticated bootstrap; returns relative socket URL and one-use ticket;
- `GET /v1/stories/{story_id}/live-sessions/{session_id}/socket` — `wl-live-v1` WebSocket carrying binary PCM and pushed events/audio;
- `POST /v1/stories/{story_id}/live-sessions/{session_id}/socket-ticket` — authenticated ticket renewal;
- `POST /v1/stories/{story_id}/live-sessions/{session_id}/diagnostics` — bounded Android transport/capture/playback diagnostics;
- `POST /v1/stories/{story_id}/live-sessions/{session_id}/stop`.

The old HTTP `input`/`events` routes are compatibility-only. Once WSS attaches, HTTP input cannot silently become a fallback.

The backend uses `live-interaction` for provider transport/session lifecycle and `ai-resource-control` for the common
Google Live lease. Street Story owns only topic context and domain tools (research, fact selection, text/literal editing,
Undo, visual generation, publication preparation/confirmation/cancel).

Before Android reports Live ready, the backend queues an orientation-normalized, bounded JPEG snapshot of the current source photo into the same Gemini Live session. This lets Mira answer direct visual questions about the selected photo. Object identity is still verified independently by `resolve_place`, which compares the original source photo with OSM/Wikipedia candidates; conversational vision never replaces that guard.

On physical-phone Live capture Android uses the existing WebRTC VAD with `VOICE_COMMUNICATION` input and enables Acoustic Echo Canceler/Noise Suppressor when the device exposes them. This is intended to preserve barge-in while reducing the assistant speaker output being reclassified as new user speech.

Live agent structure follows the shared `live-interaction` operating standard (`docs/live-agent-architecture.md` in that repository). The Live model remains the conversational controller; the backend validates capability transitions and exposes only the small tool bundle needed for the current task. Research, visual work and publication are distinct capabilities rather than one eager tool surface. Capability changes must preserve conversation continuity and use the shared provider/session-resumption primitives once released and adopted. Any Street Story-specific prompt remains here; transport/prompt-layering/tool-loading rules remain centralized in `live-interaction`.

Text edits keep optimistic `text_revision` protection. A Live `edit_text` revision conflict must not silently overwrite newer state: the same conversational turn reads the current topic, takes the fresh revision and may retry the intended edit once.

A Live failure is not an instruction to switch transport. The former async voice/session pipeline is preserved only as a
compatibility boundary so it can be developed again deliberately if needed.

## Durable story create

`POST /v1/stories`, multipart, authenticated with the Street Story device bearer token and `Idempotency-Key`.

Parts: `photo`, `client_story_id`, `photo_sha256`, `voice_protocol=voice-chunks-v2`, optional `lat`, `lon`. Android also sends `X-Photo-SHA256`; when present it must agree with the multipart digest. The server hashes the received bytes before committing the source photo.

The server binds idempotency key + client story ID to the exact photo digest and metadata. Same key/same payload returns the same story. Same key or client ID with different content is a conflict.

## Long voice protocol · compatibility contract

This API is retained for compatibility and future deliberate development. It is **not** the current Live product path,
not the current golden acceptance path, and not an automatic fallback after a Live/resource failure.

The preserved capture profile is AAC-LC mono M4A, 16 kHz, 32 kbps, WebRTC VAD 2.0.10-cf.4, 30 ms frames, adaptive energy gate, pre-roll/hangover and durable chunks.

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

The worker claims durable SQLite jobs by lease and recovers expired `running`
work after restart. The current target is the adaptive [photo-search contract](photo-search-methods.md):

1. Preserve the original photo and actual camera metadata; transcribe available
   owner M4A chunks by `chunk_index` without making voice a prerequisite for photo identity.
2. Reuse compatible evidence and prepare bounded physical OSM context. Reverse
   geocoding and optional Wiki metadata do not block an already ready map.
3. Accept a sufficiently supported physical object through geometry, acquired
   architectural text (including Prussia39 discovery), actual visual reference or
   compatible combined proof. Each method opens the same facts path; a REF is
   required only for the reference method.
4. Fetch/freeze subject-scoped sources, extract small bounded facts through the
   ordinary Live-first path and semantically review their own supporting passages.
   Grounded search and qualified helper models are conditional acquisition/fallback
   routes, not a mandatory Gemini chain before every accepted fact.
5. Persist reviewed eligible facts and provenance in shared POI memory and the
   story projection; retain owner selection for editorial drafts. Exhausted,
   blocked and deadline states have finite truthful outcomes.

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
4. submits a real `tune` visual command referencing only the VibePublish asset ID; the versioned owner prompt remains exact, while dynamic city-note context is bounded so the `brief` never exceeds VibePublish's 5000-character input contract;
5. reconciles the operation until a candidate is available;
6. uses VibePublish's official `select` command (`job_id`, `candidate_id`, `expected_revision`, selection token) with a stable key;
7. requires `verified`, `selected_asset_ref`, `selected_sha256`;
8. reads the selected asset through VibePublish, verifies the SHA over bytes, and durably stores a private Street Story processed-image readback.

Street Story does not write VibePublish SQLite, does not implement a second Imagegen, and does not bypass VibePublish with direct Telegram/VK publishing.

## Destinations

`GET /v1/capabilities` fresh-reads VibePublish bootstrap. Internal acceptance-only aliases in the `street_story_e2e_*` family (including the historical `street_story_e2e_tg`) are never projected into the product destination list; they may exist in VibePublish for isolated provider acceptance without becoming user-selectable channels. The full-social acceptance harness receives its alias only from protected test configuration, asserts that the alias remains absent from product capabilities, and relies on the normal publication bootstrap/preflight to validate the real binding before any provider mutation. Street Story projects only real publish/post destinations and prefers explicit provider data returned by VibePublish; alias/label inference is only a compatibility fallback. A Telegram destination whose fresh proof has aged from `supported` to `needs_review` remains visible rather than disappearing from the product.

The MVP primary targets are **Полюбить Калининград / Telegram** and **Полюбить Калининград / VK** when actually returned. `supported` and `needs_review` are distinct capability statuses; `needs_review` is not treated as “channel absent”. Immediately before an actual Telegram publish, Street Story refreshes only a requested `needs_review` destination through VibePublish `mode=preview`, requires a completed dry run with worker/target validation and `observed=not_attempted`, re-reads bootstrap, and proceeds only after the destination becomes `supported`. This refresh never substitutes a provider dispatch and fails closed for `needs_auth`/unsupported targets. `Ух ты, Калининград` is shown only when returned by VibePublish. Native provider/channel IDs are never hardcoded.

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
