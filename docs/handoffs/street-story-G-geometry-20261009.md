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

1. Review and integrate the independent G modules and exact tests from #250 only. Never rewrite work-in-progress source in #246 from #247. Preserve received-building set, original pixel SHA, T and current Google/Luna/etc admission logic.
2. On a malformed/false geometry decision, use `diagnose_geometry_nomination` to preserve literal verified-candidate hypothesis and why pose/features failed. Do **not** set accepted physical identity; allow another method to use it as unproven prior.
3. Blind SOURCE/MAP only (no candidate answer key, no T/REF) on frozen **102,106,132** against one current merged SHA and available qualified visual model. Capture selected ID vs separately held acceptance oracle, host proof yes/no, honest UNKNOWN, elapsed time and actual provider send/usage, no parallel duplicate sends. After positive cases test telephoto 111 and 126/130. If G remains insufficient, return UNKNOWN/conditional instead of forcing synthetic heading.
4. The main PR #246 handles facts, POI and deployment. A passing manual G host proof, standalone G module or green tests are **not** the owner's full SOURCE→physical identity→reviewed facts/POI product PASS.

**CI/commands:** in backend `python -m pytest -q tests/test_original_osm_geometry_regression.py tests/test_geometry_adjacency_diagnostics.py tests/test_spatial_correspondence_contract.py tests/test_observed_physical_geometry.py tests/test_geometry_identity_plan.py`. Local isolated latest G suite: **87 passed**, Ruff PASS, one third-party deprecation warning; full hosted CI on the reconciled PR head is the merge gate.


## New blind original-photo live tests and final G isolation (2026-10-09)

The following are CLOSED actual qualified Gemini 3.5 Flash Lite SOURCE+neutral-MAP responses using original retained photos and complete cached OSM pool. No prior owner-accepted target ID, T article or external REF was supplied. Per-attempt response/source receipts are under `/home/dev/artifacts/street-story/20261009-G-spatial-validation/`; the main integration harness should reuse them, not duplicate the same paid sends.

| Case/attempt | Closed elapsed time | Tokens (actual provider receipt) | Chosen physical ID | Status |
|---|---:|---:|---|---|
| 132 initial G-only | ~5.9 s | 23,734 | `osm:way:192217077` (oracle-correct) | Candidate correct, host proof rejected: repeated nominal pose |
| 132 compact G-only | ~4.6 s | Read closed receipt | `osm:way:192217077` (correct) | No spatial relations returned; conditional only |
| 132 street review | ~4.7 s | Read closed receipt | Earlier candidate retained | Selected opposite road direction; no acceptance |
| 132 **independent street-direction** | Closed | Read private receipt | **Prior candidate NOT shown to this model** | Cross-street not confirmed visually, opposite direction; UNKNOWN |
| 106 compact first | ~3.6 s | Read closed receipt | `osm:way:100659323` (wrong, 258 m) | Generic `other` relation; angular mismatch, withheld |
| 106 angular-aware first | Closed | Read receipt | Same distant wrong physical body | Invalid side pair and nominal angular span |
| 106 one conditional angular review | ~7.2 s | 5,783 | `osm:way:150596903` (wrong wing) | Previous wrong far body rejected but no source-spatial relation |
| 106 real model-nominated close pair detail | ~3.8 s | 3,544 | `osm:way:150596903` (wrong wing), `150596899` as companion | Original full OSM + detail, no clear physical scope; conditional only |
| 102 initial Gemini G-only | ~5.4 s | 19,147 | `osm:way:133035111` (wrong, 74.9 m) | Generic `other`, no spatial proof |
| 102 independently nominated MiMo/Gemini pair review | ~3.7 s | 3,595 | `osm:way:133035113` (correct) | Correct chosen body, corner `0/5` source-visibility error |
| 102 same pair with camera-facing halfplanes | ~3.6 s | 3,772 | `osm:way:133035113` (correct) | Repeated corner `0/5`: OSM sides joined but side 5 faces nominally *away* from EXIF camera. No host identity |

**Independent cross-provider nuance:** MiMo `@381` and Gemini `@380` were both generated by prior actual full SOURCE+MAP model sends; the later image+map referee saw only these **two peer hypotheses** and actual original pixels/OSM, not which was expected. Gemini referee twice chose the physically correct `133035113`. This is *valid model-led candidate recovery*, not a G proof. A host-measured pair `0/5` is adjacent but has signed camera-facing halfplane distances **+19.49 m / −19.00 m** for the two walls; model continued to claim both visible. Another actual pair `2/3` is nominally externally facing, but **must never replace model output automatically**. Original camera GPS error/crop/yaw and occluders remain unknown.

**New production-neutral G components:**
- `identity_camera_visibility.py` and corresponding `identity_model_context.py` columns: original OSM outer-ring wall sides in nominal camera exterior/interior halfplanes. A bad visible corner returns a **measured contradiction**, never a replacement corner, model heading, or hard candidate exclusion. Owner-approx positions are nominal; search_context provides no camera-facing rays. The actual real 102 case has regression.
- `identity_geometry_nomination.py`: short provider-valid JSON visual nomination contract and non-authorizing OSM literal-label/road/connected-side + advisory wide-angle checks.
- `identity_visual_disagreement.py`: peer candidate comparison for two *independent* model leads, including actual corner topology and nominal camera-facing wall check. Always non-authorizing unless separate strict physical proof.
- `identity_frontage_context.py`, `identity_frontage_review.py`: one **model-proposed** physically adjacent OSM pair; host measures only 2D gap (106: **0.3 m**, not a physical passage). SOURCE+MAP-detail model describes facade/wing roles but no target oracle is encoded, and output remains non-authorizing.
- `identity_road_direction.py`, `identity_road_followup.py`, `identity_road_witness.py`: independent original SOURCE road-direction comparison against conditional prior body. Geometric ray azimuth derived from actual OSM only, no forced model yaw; only matching first-hit across distinct inference and strict checked scenarios can yield a full host geometry certificate. Real blind 132 *did not satisfy the visual cross-street premise*; model correctly allowed UNKNOWN.
- All code lives in #250 G branch, not in the dirty main checkout; no Android, T rewrites, new servers, queue or production deployment.

**Concrete integration contract for PR #246:** preserve original deterministic geometry and all candidate labels; run a qualified vision role for SOURCE/MAP; attach these extra scene features; if model supplies a **literal** physical nominee but cannot substantiate geometry, keep `conditional_physical_nomination` (never `accepted_geometry`, POI-memory or fact binding). Only call optional followup if it is one genuinely new falsifiable spatial question and previous provider send has an independently CLOSED outcome. Dispatch and cost budget remain with main Codex. On an explicit model-chosen street direction, host may compute fixed nominal/±2m bearing scenarios; never silently choose the opposite ray or a different corner to force validation. A successful schema, candidate ID, manually constructed geometry witness or non-authorizing wall check **is not full G product PASS**.


## Added G morphology / original photo shapes (owner clarification, 2026-10-09)

The earlier backend did **NOT** explicitly measure whole-footprint plan shape or map height-to-width ratios, so visible 'tall narrow candle' vs nearby elongated compound was too easy for an image model to miss. The new branch implements:

- `identity_shape_context.py`: actual ORIGINAL complete-outer OSM geometry → minimum-area rotated long/short axes, elongation, exact 2D footprint area, convexity, compactness/concavity, major-axis orientation, optional **observed** height meters and building:levels, and height/long-axis + height/short-axis ratios **only with true mapped height**. Incomplete/multiple outer bodies return explicit UNKNOWN; no 3D shape, guessed floor height, camera yaw or fictional roof outline.
- `identity_model_context.py`: all received physical body rows get `plan_morphology` with `plan_morphology_columns` and clear 2D-vs-image policy. Also recovers distance and angular sector for an actual complete OSM **relation with observed member-way vertices** when its prior map row had null metrics; **does not** manufacture measurements from bare ways/centres/search-context. No ranking, nearest-K filter or body deletion.
- `identity_geometry_nomination.py`: backwards-compatible optional SOURCE `source_silhouette_form`, `source_crop_scope`, `source_shape_observations`; compare model PHOTO observation to actual OSM dimensions and all model-nominated alternatives. Report conservative review flags (e.g. tall source vs much wider observed plan, full-building crop versus extreme nominal FOV ratio), **never turn a soft shape heuristic into a physical proof or automatic ID**.
- `identity_source_map_prompt.py`: tells image model to compare SOURCE vertical silhouette, complete/partial crop, neighboring physical volumes and OSM plan/height separately; end-on wing and telephoto are explicit exceptions.
- Original map regression `test_original_osm_shape.py` plus original frozen two-rival OSM geometry `tests/fixtures/g_original_111_morphology.json`. Full isolated G cohort **103 passed**, Ruff PASS. Original OSM route/way null semantics preserved (fixed one test-discovered overly broad fallback).

**Actual original photo102 (tall, narrow facade SOURCE):** accepted-body test oracle `osm:way:133035113` maps a **33.37×10.93m** plan (elongation 3.05; 365m²), wrong alternate `osm:way:133035111` **31.55×13.19m** (elongation 2.39; 404m²). Thus BOTH plan footprints are somewhat elongated: morphology alone does not select a house. Their *camera-to-contour angular spans* **35.6° vs 11.6°** and source-visible wall halfplanes/neighbor relations matter substantially more. Neither OSM body has mapped numeric height; do not convert visual storeys into invented height. A new blind G-only Gemini 3.5 Lite source+MAP run **classified SOURCE independently as `tall_narrow` but returned `uncertain`** with no physical ID (4.155s, 27,432 reported tokens, closed receipt `real-102-google35lite-G-morphology-once-v2/`). This is honest UNKNOWN, not photo102 accepted recognition.

**Original photo111** is a **partially cropped view of a dome/rotunda and neighboring upper building volumes**, not an entire visible candle-shaped physical building. Offline original OSM expected-way `osm:way:95290265` has **47.79×28.60m** plan (964m², mapped height UNKNOWN). Earlier live Gemini wrongly/controversially selected composite `osm:relation:19306342`, mapped **112.06×100.87m** (5,821m², explicit height 49.64m, 12 levels); OSM relation member-way geometry was reconstructed from its literal source members, not guessed. Distance/sector from *original EXIF camera* **38.6m/58.7°** vs expected-way **188.8m/11.6°**. Beware: a cropped dome on a broad real complex could appear narrow; OSM plan size alone is not proof that the complex is wrong. One new blind shape-aware SOURCE+MAP Gemini 3.5 Lite run completed in **5.776s, 23,646 tokens**, reported `multi_volume` and `upper_or_partial`, selected `osm:relation:19306342` again; host status **conditional, NOT accepted G proof**. Private receipt: `real-111-google35lite-G-morphology-once-v2/`. No identity override based on owner expected OSM-way.

**Integration caution:** A model could visually classify a narrow corner turret of a large apartment complex as a 'candle'; the map footprint belongs to the entire physical body, not only the visible turret. This is the main reason morphology must be combined with SOURCE crop status, true projected angular span, camera side visibility, edge/corner geometry and nearby distinct physical bodies. The G shape comparison is meaningful contradictory evidence, **not a rigid hard exclusion**. The main Codex owns final provider dispatch, verified-source fact binding and acceptance.


## Strict host corner acceptance correction — verified original SOURCE photo102

Original photo102 demonstrated that map adjacency alone is insufficient to certify a visible wall return. New G-only update in `identity_geometry_contract.measured_correspondence()` uses the *real original EXIF* camera anchor: when a model claims pattern `corner`, BOTH original OSM sides must constitute an actual joined outer-ring corner, be appreciably non-collinear under the existing criterion, and have nominally outward-facing wall halfplanes. The host must never silently replace a claimed side or adjust camera yaw to make the proof pass. When the only camera basis is owner-approximate or search-context, this additional exact EXIF rear-wall veto is **not applied**; true GPS precision remains unknown.

On the original full OSM geometry for `osm:way:133035113`, camera anchor and candidate unchanged:

- `0/5`: actual OSM wrap-around corner at −90°, but exterior signed offsets approximately **+19.49 / −19.00 m**; one model-claimed SOURCE wall is behind the nominal camera side. Host REJECTS.
- `1/2`: actual ~90° map corner, both exterior offsets **+19.49 / +8.08 m**. Host numeric predicate MAY accept this claimed pair (but never asserts the PHOTO actually shows it).
- `2/3`: both outward offsets ~+8.08 m, but sides are nearly collinear (**0.03°**), hence NOT a distinguishing corner; host REJECTS.
- These candidate pairs are **geometric diagnostics only**, not a new model decision. The model repeatedly claimed invalid `0/5`; do not relabel it as `1/2` or `2/3` without independent SOURCE evidence.

The pre-existing synthetic positive geometry fixture had itself claimed a back-facing east wall (`0/1`) from a southwest camera; it has been corrected in `tests/test_geometry_identity_plan.py` to a physically valid south/west `0/3` corner. This is a test-fixture correction, **not** a relaxation of the host proof. `tests/test_corner_host_exif_visibility.py` adds real 102 gate checks; local G corner suite passed **24 tests**. Full backend CI must pass on the reconciled merge commit before integration.

This strictly narrows **accepted** camera-facing corner proofs; it does NOT forbid preserving a correct but geometrically unproven `conditional_physical_nomination` and continuing to an independent T method.
