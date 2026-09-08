# Street Story

Small Android product for the reliable path **one city photo → long voice note → source-backed facts → styled visual → provider-native scheduled publication**.

Android MVP lives on `work/street-story-mvp-20260908` and deliberately reuses the proven Record Idea Hub capture architecture: WebRTC VAD, auto-silence, AAC-LC M4A chunks, foreground recording, SQLite WAL and WorkManager reconciliation. The visual language follows Repeat It: sage/paper/graphite, large editorial type, rounded surfaces and one primary action per stage.

The phone stores the selected photo and voice chunks before any network effect. Backend and VibePublish credentials are never embedded in the APK. The Street Story backend is a separate Python/SQLite service to be deployed on **DevCoveer**; VibePublish must also be resident there. **Fly.io is out of scope for both.**

See [`docs/backend-contract.md`](docs/backend-contract.md) for the exact Android/backend protocol and VibePublish integration boundary.
