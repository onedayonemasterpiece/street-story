# Street Story owner DoD fixture — Закхаймские ворота

This fixture is the owner-visible acceptance pair supplied on 2026-09-28.

- `source.jpg` is a repository-sized JPEG derivative of the supplied source photo.
- `reference-generated.jpg` is a repository-sized derivative of the earlier generated result and is **visual reference only**.
- The two images may be from a slightly different capture of the same viewpoint. Acceptance must not require pixel registration.
- Text rendered inside the reference image is not factual evidence. Street Story must obtain publishable facts through its Live internet-search path and store real source URLs.
- `fixture.json` records both committed-derivative hashes and original-upload hashes so provenance remains auditable.

Definition-of-done use: run the production Live canary with this fixture, generate a fresh visual from the source photo, prepare/confirm a test Telegram publication through Street Story -> VibePublish, then verify the resulting message from Telegram provider readback.
