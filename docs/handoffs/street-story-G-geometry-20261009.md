# Street Story G — physical geometry handoff (9 Oct 2026)

**Scope:** independent LLM-first building identity by actual SOURCE + optional original EXIF/camera point + **unmodified** observed OSM geometry; no obligatory REF, publisher text, database, queue, 3D renderer or provider-orchestration rewrite.

- **Main integrator:** [PR #246](https://github.com/onedayonemasterpiece/street-story/pull/246); main agent owns provider admission, original SEND/UNKNOWN readback, T, facts, POI, release.
- **Pure G contribution:** [PR #250](https://github.com/onedayonemasterpiece/street-story/pull/250), cleanly reconciled with the latest integrated #246 at merge `9bb60d486d90e24810702a2fb41bf38784cf9b75`. Original mixed G/T changes and **unchanged T functions/tests** remain archived in [PR #247](https://github.com/onedayonemasterpiece/street-story/pull/247); [T handoff](street-story-t-method-handoff-20261009.md). Do not blindly merge #247 over #246.

## Integration surface: 9 G files, no changes to runtime orchestration

- `identity_model_context.py`: all physical OSM candidates, actual literal addresses, 35mm nominal EXIF angular scale (already imported into #246), plus **observed adjacent wall indices** and **bidirectional road-axis cues**.
- `identity_corner_context.py`: joins are consecutive original OSM ring-index edges, verified including wrap only for an actually closed ring; never join unrelated edges whose rounded coordinates happen to coincide. Bounded excerpt may show a *real* connected pair if four longest sides hid all corners. All original ring sides and broad candidates remain available.
- `identity_road_context.py`: up to three nearby **road axes, not buildings**, measured in both opposite horizontal directions; at most three observed building plan intersections per direction. Type/name from actual OSM, optional nearest named road if only unnamed footpaths are in initial three. This is conditional 2D context, **not a target score, camera yaw, visibility/occlusion guarantee or exclusion of far bodies**. Without a verified original/explicit owner approximate camera, road cues are empty.
- `identity_geometry_contract.py`: reject nearly collinear claimed corners; require a street-termination heading approximately aligned with the original road axis modulo 180 degrees and its first OSM ray hit; only SOURCE-claimed visible body must be forward. An explicitly **rejected** competitor may be behind the camera; a genuine `frontage_sequence` still requires all its displayed bodies forward. Existing strict SOURCE/MAP hash, primitive IDs and pose checks remain.
- `identity_geometry_diagnostics.py`: source-bound, map-label-bound, **non-authorizing** `conditional_physical_nomination` with concrete failure codes. Only `freeze_geometry_proof` can authorize a geometry identity. A correct model ID with a false pose is a hypothesis, not POI/facts evidence. Integrator can call this helper after geometry-proof rejection and pass ONLY as conditional context to T; do not silently set identity=match.
- `identity_source_map_prompt.py`: optional compact independent provider presentation, removing encyclopedia/catalogue metadata but preserving complete spatial body inventory and map. The existing #246 primary route is not replaced. Separate #247 OpenCode transport remains shared infrastructure owned by Codex, not G code to override.
- Tests: `test_geometry_adjacency_diagnostics.py`, `test_original_osm_geometry_regression.py`, literal bounded OSM test fixtures `tests/fixtures/g_original_osm_geometry.json`. Expected IDs in **tests only**, never in runtime prompts.

## Evidence — actual originals, no additional inference

Private frozen SOURCE/OSM snapshots reside under `/home/dev/artifacts/street-story/20261009-G-spatial-validation/`. Reconstruct six neutral cases using original JPEGs + PR245 original OSM snapshots. All target labels were checked only **after** neutral map assembly. No web calls, external reference images or publisher bodies in G:

| Photo | Position provenance | Received OSM pool / buildings | Frozen physical observation |
|---|---|---:|---|
| 102 | original EXIF GPS, no heading | 716 / 91 | Subject at 21.1 m; genuine connected segments vs previous MiMo's nonadjacent 5/2 |
| 106 | original EXIF GPS, no heading | 498 / 81 | 21.831 m first frontage; 45.292 m next frontage, ~11.59 m relative signed setback, ~0.3 m raw OSM boundary gap **not** verified passage |
| 132 | owner approximate, *no EXIF GPS* | 238 / 122 | Smolenskaya axis 4.67 m from camera hint; eastbound ~83.96° first hits the front physical building ~70.16 m away, westbound does not |
| 111 | original EXIF, 10.3° nominal lens diagonal | 724 / 81 | Target at ~188.8 m; not dropped for telephoto distance. Accurate heading and 3D occlusion unknown |
| 126 | owner approximate only | 262 / 142 | Full map retained, unknown final identity in independent owner acceptance labels |
| 130 | owner approximate only | 180 / 137 | Full map retained; no fabricated EXIF GPS/accuracy |

**Offline runtime:** actual map + geometry packet ~0.9–1.7 s per original. Geometry packet stage alone measured 0.10 s (132), 0.28 s (106), 0.57 s (102), including street bidirectional rays; 3 nearest observed road axes shown, all original physical buildings remain eligible. Platform/fixture timings are not deploy or mobile latency.

### Strict existing host proof actually succeeded on two real SOURCE/OSM cases

Source observations were **manually constructed from original image inspection**, *not generated by an autonomous model in these runs*. The exact same `freeze_geometry_proof()` accepted and froze certificates, with no REF or T:

- **132**: west/east OSM axis direction differentiated by the visible street termination; correct first footprint `osm:way:192217077` for **3/3 preset camera scenarios** (nominal and ±2 m). Proof SHA `910a5976089d375a70548cc9cd36408d13d59aba27b4798831f5ffeaeb87a92c`.
- **106**: photographed three-storey/mansard volume and next receding OSM body; `osm:way:150596899` accepted in 3/3 preset frontage-order scenarios. Raw signed mapped setback ~−11.59 m, mapping gap ~0.30 m. Proof SHA `8a0e3ea23a1cddd46e4de4740b58bea8c1e6a14794b45fb3a6ec0b5340d14624`.

Replay receipt JSON: `g-106-manual-host-witness.json`, `g-132-manual-host-witness.json` in the private folder. Source scripts and their SHA-256s persist in the **private** `reproduce/script-manifest.json` folder, independent of short-lived checkout cleanup. These positive host certificates **do not** establish automatic model interpretation or final product acceptance.

### Actual model-bound quality & provider limits (keep distinct)

- Before this G-only refactor, one real SOURCE+MAP MiMo response for photo102 selected `@381` → the correct `osm:way:133035113`, but its provided geometry was **invalid**: nonadjacent sides 5/2, repeated nominal pose and claimed yaw putting nominated bodies behind. The host properly withheld identity. It is a **correct nomination / failed proof**, not recognition PASS. The new diagnostic returns non-authorizing `conditional_physical_nomination` with those exact codes when replayed on the original receipt.
- Subsequent full SOURCE+MAP MiMo G-only sends were slow and exceeded ~120-s worker timeout. No independent full accepted certificate was received. Avoid a fourth identical paid run; use new physical packet and a newly admitted qualified visual route only when shared provider slots actually allow it.
- Codex native Luna quota had reached account reserve (10%) during G diagnostics; do not bypass its account permission. Standalone Gemini3.8 canary photo106 returned `GeminiUnavailable` after ~19 s; no completed SDK/model response or accepted G certificate. The test harness recorded an UNKNOWN categorization, so preserve it as fenced, not a reason to spend a second key blindly.
- Autonomous accuracy **not measured as a passing ratio**. Offline expected ID presence in the complete neutral OSM map is not the same as model recognition. Actual paid tokens/cost unknown absent closed provider ledger.

## Codex integration / smallest remaining acceptance

1. Review and integrate the nine G files from #250 only. Never rewrite work-in-progress source in #246 from #247. Preserve received-building set, original pixel SHA, T and current Google/Luna/etc admission logic.
2. On a malformed/false geometry decision, use `diagnose_geometry_nomination` to preserve literal verified-candidate hypothesis and why pose/features failed. Do **not** set accepted physical identity; allow another method to use it as unproven prior.
3. Blind SOURCE/MAP only (no candidate answer key, no T/REF) on frozen **102,106,132** against one current merged SHA and available qualified visual model. Capture selected ID vs separately held acceptance oracle, host proof yes/no, honest UNKNOWN, elapsed time and actual provider send/usage, no parallel duplicate sends. After positive cases test telephoto 111 and 126/130. If G remains insufficient, return UNKNOWN/conditional instead of forcing synthetic heading.
4. The main PR #246 handles facts, POI and deployment. A passing manual G host proof, standalone G module or green tests are **not** the owner's full SOURCE→physical identity→reviewed facts/POI product PASS.

**CI/commands:** in backend `python -m pytest -q tests/test_original_osm_geometry_regression.py tests/test_geometry_adjacency_diagnostics.py tests/test_spatial_correspondence_contract.py tests/test_observed_physical_geometry.py tests/test_geometry_identity_plan.py`. Local isolated latest G set: **53 passed**, one third-party deprecation warning; run GitHub backend CI on the reconciled PR head before merging.
