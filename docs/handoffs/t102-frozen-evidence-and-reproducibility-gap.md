# Photo102 — evidence handoff and reproducibility gap

**Current status, 2026-10-09:** a previous blind-v4 photo102 SOURCE + architectural T model response was closed; `freeze_architectural_text_proof` and the then-existing T gates returned Accepted `osm:way:133035113`. That result belongs to the **isolated experiment** and does not overwrite the primary Codex's independent uncertain102 state.

**Exact retained private artifact** (DevCoveer, current managed retained store):

`/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t102-exact-replayed-request-handoff.json`

This includes the original SOURCE path + SHA-256 (`f56c6cfeaf4b88a018dd3567e66d6fd75918d42da30100cff718c069b10d7c7e`), normalized SOURCE hash, MIME, dimensions/bytes, received article URLs/SHA/texts from publisher cache, observed OSM candidates and their addresses, model name, decoded closed T response, proof SHA where retained, request SHA and response SHA. The underlying image and raw HTML live in the same managed artifact directory; do not send them to public GitHub.

## Do not misrepresent original bytes

**The exact original model request prompt was NOT retained at send time.** The model runner's current source and retained inputs regenerate a nearly identical prompt (21,649 UTF-8 bytes), but its SHA does **not** match the original 21,655-byte input receipt.

- Frozen send-intent request SHA: `e6969800422f09e94375c9a6aa4ffac7fa441bdc779d77abeb1a8fa6d70f1793`.
- Regenerated candidate SHA: `e00e76f3c6a4d28de9fc6756505dc30aeb213901664b925181e769f6e42d3736`.
- The original closed response SHA is `cd24126aa1b8e1d82d03ad8dd6a08091e9e1f7abd06220bdedf8498d72bc062d`; only the parsed response survived and the original raw JSON serialization cannot be SHA verified. **Do not treat reconstructed prompt or parsed response as exact wire evidence.**

This is likely why a report alone cannot reproduce Codex's uncertain102. We must reproduce it with a NEW controlled, single, quota-admitted SOURCE+T operation which freezes the exact prompt/schema/normalized-image checksum BEFORE SDK dispatch and stores the original response.text verbatim on successful readback. Do not reuse or overwrite Codex's addressed operation or compare SHA between different service/state routes without recording those differences.

## T method fix under development

PR #249 is being improved rather than closed as an experiment:
- Removed false proximity-only veto. Geometry distance is competitor context, not a proof of foreground visibility.
- Article reading now admits unresolved corpus/address scope; physical acceptance is delayed until SOURCE semantic decision.
- Bounded multiarticle T prepares up to eight actual architecture descriptions in one call, without dropping later SIDs simply because first two catalogue cards were wrong.
- Next: true source-text quote spans, non-strict literal postal alternatives backed by independently observed OSM↔publisher links, blind corpus quality/regressions and controlled single Photo102 readback.

**Full product PASS remains unclaimed.** Do not silently replace your uncertain102 with this historical experiment result.