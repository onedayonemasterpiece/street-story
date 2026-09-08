# Street Story

Small Android product for the reliable path **one city photo → long voice note → source-backed facts → styled visual → provider-native scheduled publication**.

The Android client on `work/street-story-mvp-20260908` reuses the proven Record Idea Hub capture architecture: WebRTC VAD, auto-silence, AAC-LC M4A chunks, foreground recording, SQLite WAL and WorkManager reconciliation. The visual language follows Repeat It: sage/paper/graphite, large editorial type, rounded surfaces and one primary action per stage.

The same repository now contains the Street Story Python 3.12 backend under [`backend/`](backend/): FastAPI, SQLite WAL, durable voice manifests, restartable research/publish jobs, OSM/Wikipedia caching, Gemini grounded research, evidence-backed facts, and HTTP-only VibePublish integration. The phone stores photo and voice chunks before any network effect; Gemini/VibePublish/social credentials never enter the APK.

Street Story backend and VibePublish are intended to run persistently on **DevCoveer**. **Fly.io is not used or planned for either service.** The backend has a public `/healthz`, a single-owner device bearer boundary, a Dockerfile and a systemd-oriented runbook. `DATA_DIR` must be persistent.

Current VibePublish PR #1 has no authenticated HTTP source-image ingress. Street Story therefore preserves research/facts/draft and transitions visual work to durable `visual_blocked` with `vibepublish_media_ingress_not_enabled`; it does not invent an endpoint or write VibePublish SQLite directly.

See:
- [`docs/backend-contract.md`](docs/backend-contract.md) — Android/backend protocol and VibePublish boundary;
- [`docs/backend-runtime.md`](docs/backend-runtime.md) — DevCoveer runtime/deployment runbook.
