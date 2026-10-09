# Street Story T — Photo102 complete exact input/output replay (2026-10-09)

**This supersedes the earlier incomplete blind-v4 Photo102 receipt.** A newly controlled, single native Gemini `grounded_research` operation used the **actual SOURCE image** + three verified Prussia39 publisher article bodies + eight real OSM physical nominees. Before the provider SDK send, the actual request prompt, response schema, system instruction, normalized original image bytes and all SHA-256s were persisted. The **original raw provider response text** was saved immediately after close, not synthesized from parsed JSON.

**Managed retained DevCoveer artifacts** (private, not committed to this public repository):

- Exact prompt, schema, system instruction, all model input article bodies/metadata, OSM candidate IDs, immutable SDK request hash, **raw response.text**, usage and accepted physical ID: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-multiarticle-contrastive-source-results-v2-reproducible.json`, case `102`.
- Independent offline SHA and host proof readback: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t102-reproducible-v2-closed-proof-manifest.json`.
- Original SOURCE: `/home/dev/artifacts/street-story/20261008T061021Z-cold-photo-workers-20261008/photo-102.jpg`.
- **Exact normalized bytes sent to model:** `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-pool-v2-model-input/normalized-SOURCE-102.bin`.
- Verified raw publisher HTTP bodies: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/prussia-cache.json`. Saved full OSM physical scene: `osm-physical-observed/102.json` in same directory.

## Exact independently verified hashes

| Evidence | Value |
|---|---|
| Original SOURCE SHA-256 | `f56c6cfeaf4b88a018dd3567e66d6fd75918d42da30100cff718c069b10d7c7e` |
| Actual model-input image SHA-256 | `07e134ba386d9c706776e7e0f55e2810d165cb690cbec7f04f2e5c61625fe12f` |
| Actual prompt SHA-256 | `779ffd24651bbfcea6555b3bff8bb23728c621cc7cd2c4bc56d8e92e56481972` |
| Actual JSON schema SHA-256 | `1a9f1caf2a145eae9a68c35ab7d05f3637ecffb5d723bd997ef46a425e381cb9` |
| Frozen request SHA-256 | `08a6257e38e7798ee581d4d5e3c90362d2f9939b397b372945c3e1ea48c431a7` |
| Actual raw response.text SHA-256 | `3da635854b43805250abe2ac0f5e84ca7a5e921b38a1b28560ddd90570b17aa6` |
| Host `freeze_architectural_text_proof` SHA-256 | `9c2a7177e8eed64253ab937f1cfe9a5c4dfa2e0496a0d9d07943e8123b52922b` |

**Model route:** `gemini-3.5-flash-lite`, SOURCE+TEXT one SDK request, **5.582 s model stage**, actual usage **5,988 input / 1,103 output tokens** (1,064 image tokens); money `unknown`. No automatic retry for this identified model operation. This time excludes earlier public publisher and OSM acquisition.

**Received candidate article IDs (original discovery):** `prussia39:sid:900`, `prussia39:sid:901`, `prussia39:sid:3875`. **Actual model selected:** `prussia39:sid:3875`, physical object `osm:way:133035113`.

**Verification:** a second isolated offline run read the frozen prompt and schema, normalized image bytes and raw response, verified **all individual SHA hashes**, reconstructed the source article inputs from the original verified cache, byte-compared the prepared prompt and schema, ran the unchanged existing `freeze_architectural_text_proof` and T physical guards, and reproduced **the same proof SHA-256**. No model call or hidden answer is involved in this replay. The earlier blind-v4 request was *not* byte-reproducible; this new controlled v2 is.

## Codex #246 action

Read the actual managed artifact JSON and compare with your **independent uncertain102**: SOURCE/source hash, OSM candidate scope and address/entrance membership, publisher article inventory and selected body, exact model route, prompt/schema/bytes, model output and host proof. Determine which step diverged. Do **not** overwrite uncertain102 with this result or interpret this T-only pass as product E2E. You own the common `identity_discovery.py`; T-only source methods and guards are in PR #249.

The next product integration should reuse this acquired `source_text_receipt.articles` and original publisher version/sha for fact review/POI-memory rather than re-searching.

**Status:** exact reproducible T component success; product integration/deploy not yet accepted.
