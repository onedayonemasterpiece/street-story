# Street Story

Application chat: [Street Story in Telegram](https://t.me/c/4488229487/2). Agent delivery details: [Telegram routing](docs/telegram-routing.md).

Street Story is a small Android product for the reliable path **city photo → long voice context → source-backed facts → VibePublish visual → provider-native scheduled publication**.

The backend keeps the uploaded original, including EXIF, privately with its story until the story is deleted. Originals are not encrypted or exposed as public static files. RAM is a bounded cache; closing the app, returning the next day, or restarting the backend does not require reupload for stories created with this storage contract. Existing stories uploaded before it may need one normal reupload. Background identity and source-backed fact review run on the server; Mira can use eligible backend facts without repeating their review. Opening the app synchronizes the persisted result before further editing or publication.

The Android client on `work/street-story-mvp-20260908` uses one dense messenger-like vertical feed rather than a wizard. It renders the latest 10 durable story sessions while older sessions remain in SQLite/backend storage. Each thread keeps the original photo, cleaned owner voice/refinements, research status, inline fact review, processed image state, collapsible editable publication text, destinations and provider status together. A fixed microphone dock targets the explicitly selected story; `+ Новая история` opens original photos in the Images document root rather than the default Recent list, preserving GPS where the provider and owner permit it. Gallery → Share → Street Story is also supported. A new story can start while older stories keep processing in the background.

Recording keeps the proven Record Idea Hub architecture: WebRTC VAD with adaptive gate, pre-roll/hangover, AAC-LC 16 kHz mono M4A durable chunks, foreground service, pause/resume/finish, SQLite WAL, process-death recovery, WorkManager, exact chunk-SHA reconciliation and stable semantic idempotency. The raw ASR transcript remains durable source truth; a separate persisted `display_text` is used in the feed so fillers/repeats/false starts are not shown to the owner.

The Python 3.12 backend under [`backend/`](backend/) provides FastAPI, SQLite WAL + `synchronous=FULL`, durable voice manifests, restartable research/visual/publish/cancel jobs, OSM/Wikipedia caching, grounded Gemini research, evidence-backed facts and HTTP-only VibePublish integration. The phone copies the photo privately and hashes it before any network effect; Gemini/VibePublish/social credentials never enter the APK.

## VibePublish boundary

Street Story uses VibePublish as the **only** social and visual boundary. The supported lineage is VibePublish commit `dec1c69920f09ebdc0551132e6bdffba73d03e8e` or a compatible newer deployment. Street Story uses:

- authenticated, idempotent `POST /v1/assets` source-image ingress and `GET /v1/assets/{id}` readback;
- `POST /v1/visuals/commands` for the real visual job plus official candidate selection;
- operation reconciliation through `GET /v1/operations/{id}`;
- `POST /v1/publications` for provider-native scheduling;
- publication command/cancel through VibePublish, with cancelled provider readback.

Street Story never writes VibePublish SQLite directly, never implements a parallel Imagegen, and never publishes directly to Telegram/VK/MAX. The former `vibepublish_media_ingress_not_enabled` blocker is not the current product contract.

## Runtime and acceptance

Street Story backend and VibePublish are intended to run persistently on **DevCoveer**. **Fly.io is not used or planned for either service.** The backend exposes public `/healthz` including exact `source_sha`; real Live E2E fails closed before provider work when the deployed SHA does not match the PR HEAD.

The live smoke chain covers real photo+GPS, durable initial voice, a second refinement, raw/display transcript persistence, OSM/Wikipedia/grounded Gemini evidence, manual facts, selected-toggle preservation, VibePublish source ingress replay, visual generation/selection/reconciliation, processed-image hash readback, draft and Telegram/VK projection. Full social acceptance additionally requires an explicitly safe test destination and proves schedule → provider readback → cancel → cancelled readback with no residual test publication.

See:
- [`docs/photo-search-methods.md`](docs/photo-search-methods.md) — current target identity methods, Prussia39 discovery and acceptance;
- [`docs/README.md`](docs/README.md) — documentation authority and historical research;
- [`docs/backend-contract.md`](docs/backend-contract.md) — Android/backend and VibePublish protocol;
- [`docs/backend-runtime.md`](docs/backend-runtime.md) — DevCoveer deployment/recovery runbook;
- [`docs/gemini-reliability.md`](docs/gemini-reliability.md) — shared Gemini quota/failover rules.
