# Street Story T — product G → SOURCE+TEXT → REF integration, 2026-10-09

**Owner:** T PR #249. **Integrator:** Codex PR #246. **Independent spatial supply:** G PR #250. This is a genuine T implementation tested on real original photos, **not** approval of deployed Street Story or a mandate to replace another agent's shared checkout.

## Actual contract (no orchestrator rewrite by T)

- `backend/street_story/identity_architectural_funnel.py`: `prepare_t_g_funnel(g_result, observed_candidates, source_articles, source_sha256=..., g_source_sha256=..., independent_closed_T_leads=())`, `t_g_funnel_schema(prepared)`, `close_t_g_funnel(...)`.
- `backend/street_story/identity_architectural_pool.py`: `prepare_architectural_pool(story, received_osm_bodies, source_text_receipt, g_funnel=closed_G_funnel, g_source_sha256=original_SOURCE_sha, independent_closed_T_leads=[...])`. Feed `packet['prompt']` **with actual original SOURCE pixels** and `packet['schema']` through the existing qualified visual provider. **No second semantic model call required for narrowing.** Close the original raw model answer with `close_architectural_pool_response(...)`.
- Positive `closed['accepted']` -> standard frozen, SHA/provenance checked architectural proof for a specific OSM body. No REF or separate G acceptance required. If G has already independently accepted with its own strict proof, T is **not a barrier**.
- Otherwise `closed['T_shortlist_and_REF_plan']` contains `active_physical_candidate_ids`, all `reserve_physical_candidate_ids`, explicit **conditional** contradictions, exactly model-authored `next_distinguishing_question`, `downstream_REF.image_research_goals`, and `downstream_REF.already_acquired_source_image_links`. Persist this as a **research priority**, never `accepted_geometry`, canonical POI identity or fact authorization.
- Reopen an observed body from G reserve only using a separately **closed** same-SOURCE model nomination with its original response SHA, never an oracle, nearest map contour or host linguistic matcher. Every reserve body remains reconsiderable.
- `backend/street_story/prussia39.py` captures actual publisher image URLs from the **same acquired HTML**. `identity_architectural_context.py` now carries them through all three publisher article acquisition routes. Images are **not** asserted visually verified merely by URL presence.
- 11 critical requirements in `.devcoveer/requirements.json` unchanged. No new servers, queues, source-to-reference barrier, postcode regex, proximity-veto, second POI memory, or changes to your source/deploy code.

### Minimal orchestration hook for Codex-owned `identity_discovery.py`

Use the existing SOURCE+TEXT vision call and its normal budget/lease/UNKNOWN journaling. The small integration is a conditional branch at the point where both original G shortlist and acquired publisher articles are already available:

```python
# All values refer to ALREADY RECEIVED/VERIFIED inputs, never hints from an oracle.
packet = prepare_architectural_pool(
    story, observed_osm_bodies, verified_source_text_receipt,
    g_funnel=closed_G_funnel,                        # source_and_map_bound=True
    g_source_sha256=original_G_SOURCE_sha256,       # must equal current original SOURCE
    independent_closed_T_leads=prior_closed_same_SOURCE_T_leads)
if not packet.get("skip_T"):
    # REUSE your current qualified multimodal/source admission+freeze:
    # answer = existing_model(SOURCE_pixels, packet["prompt"], packet["schema"])
    decision = close_architectural_pool_response(
        story, observed_osm_bodies, packet, answer,
        source_text_receipt=verified_source_text_receipt)
    if decision["accepted"]:
        # use decision["proof"] as independently authorized T proof
        ...
    else:
        # PRESERVE all group/reserve and targeted REF goals, not an identity
        followup = decision["T_shortlist_and_REF_plan"]
        ...
```

This intentionally does **not** provide a stale diff applying wholesale to `identity_discovery.py`: #246 has its own concurrent modifications there. Integrate only this hook into the **current Codex checkout**. In particular, do not overwrite #246's existing `identity_proof.py` or already addressed UNKNOWN operations with old PR #249 equivalents.

## Two genuine original SOURCE+MAP → SOURCE+TEXT tests, no oracle in model prompts

| Evidence | Photo132 | Photo106 |
|---|---|---|
| Original SOURCE SHA-256 | `4f7dc18d93b6cea0382c206fca8ff7a9707df9c600824e11361ed883cb338b7d` | `ec1717997abf9a78b5443c2315fe28311709ecbe9be0c594ccc0b7c8b2e9f04f` |
| Genuine G initial OSM physical count | 122 | 81 |
| Actual closed G nominees | `osm:way:192217626`, `osm:way:192217077` | `osm:way:100659357` (alone, **wrong**) |
| Independent closed T lead from previous operation | not needed | `osm:way:150596899` **reopened from G reserve**; prior full proof invalid |
| Group supplied to NEW SOURCE+TEXT model | 2 | 2, one from G, one independently from T |
| Real Prussia39 article bodies | SID 1019, SID 4894 | SID 2458, SID 2571 |
| New closed T model physical choice | `osm:way:192217077` | `osm:way:150596899` |
| Gemini 3.5 Flash Lite actual T inference | 6.534 s; 6,451 input / 1,489 output tokens | 8.631 s; 6,529 input / 1,806 output tokens |
| Current T **HOST** outcome, replay of exact original raw text after method fix | **accepted_T_identity**, proof SHA `d79aac3fb2cb5308981c6429c6c742a99dbcb4107b65d40b5daff5c2b7344634` | **conditional_T_shortlist**: 1 priority, 80 reversible reserve, **no authorized physical ID** |
| Why not full identity on 106 | — | Source article describes a **multi-building complex**; actual model-selected publisher/OSM refs did not establish a substantive building-specific address/provider binding. Do **not** override host rejection. |
| Task for existing-image / REF | Not needed | Model's own question: *Are additional courtyard elevations required to confirm adjacent segment numbers?* No host-generated viewpoint. |

**Exact original immutable provider inputs and RAW responses** in retained private DevCoveer artifacts:

- 132: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-G132-genuine-G-shortlist-live-receipt-v1.json` — request SHA `6bd8e3758a0234e234adde2fc19fa3bf0b3dfd09172318a8513d4cb4881be599`; RAW model response SHA `0ea8e1d3ac8f184476d9a082daca2d7ac2c90e1bd3e60f5d1ed3b85ea4fed9f1`; exact G handoff `/home/dev/artifacts/street-story/20261009-G-spatial-v3/cases/132/inference-gemini-3.5-flash-lite/funnel-handoff.json`.
- 106: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-G106-independent-closed-G-and-T-shortlist-live-v1.json` — request SHA `7b9649fc1c5bca044563cbd0a0c1bec506254e2f366375b0485900a1d003754d`; RAW model response SHA `02883f7534115ba1d9eea4ea48ea31840927532f5f007b547d36d635bb530587`. Two original closed model leads mechanically assembled in `t-G106-genuine-independent-closed-G-and-T-preflight.json`; G raw inputs `/home/dev/artifacts/street-story/20261009-G-spatial-v3/cases/106`.
- Independent original Photo102 T-with-ALL-91-OSM-objects positive receipt: `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-unfiltered-all-observed-llmfirst-live-results.json`, case 102. **Accepted** `osm:way:133035113`, model 6.182 s, 30,296 input / 1,612 output tokens, exact original raw SHA `22b714cd3099e51e2fa586c28d8f60c596c9172d06cd9a932ba8fcf682b1e1d1` (before later prompt compression). See [the separately SHA-verified controlled Photo102 exact handoff](t102-byte-verified-exact-success-20261009.md). Do not confuse this with primary Codex's distinct `uncertain102` operation.
- Negative control: the older blind-v4 closed SOURCE+TEXT Photo108 was `uncertain`; another historical v2 attempt falsely nominated a neighbor and was **withheld**. These are **NOT** successful cold results for the new G/T contract; preserve separate receipt generations. Photo130's older neighbor proposal likewise is not a new G/T PASS.

## Observed latency / economics — distinct stages, NOT an invented E2E SLA

| Stage | Photo106 | Photo132 |
|---|---:|---:|
| G actual map preparation (old G receipts) | 1.185 s | 1.720 s |
| Real Prussia39 coordinate catalog HTTP lookup (old receipt) | 1.000 s | 0.639 s |
| Actual publisher article fetches | 0.761 / 0.803 s | 0.779 / 1.588 s |
| One new SOURCE+TEXT Gemini request | 8.631 s | 6.534 s |
| Model-input cost (provider receipt) | 8,335 total tokens | 7,940 total tokens |
| Monetary cost | unknown | unknown |

The stages were recorded in **different** sessions; summing them is not a measured complete wall-clock journey. Concurrent G/map and publisher acquisition is possible but is **Codex-owned scheduling**. The canonical product's complete SOURCE→identity→facts→POI path remains unverified by these T tests.

## Tests and state

T-focused isolated test suite: **83 passed** covering G/T conditional narrowing, SOURCE/OSM pointer integrity, original article acquisition, Wikipedia model title matching, Prussia39 image extraction, no-GPS and negative schema/ref controls. Two original raw model replies additionally replayed offline, one accepted and one correctly held as conditional. GitHub-hosted full CI and the concurrent #246 integration still need to be reconciled; #249 currently has moving-base conflicts. Do not merge wholesale over #246; cherry-pick **T-only** modules/methods and their tests.


## Existing Live REF reuse from already acquired articles — product handoff

After a CLOSED G+T result that did NOT independently accept physical identity, call the T-owned methods:

    from .identity_architectural_funnel import (
        to_existing_research_priority,
        acquired_article_images_for_existing_REF)
    priority = to_existing_research_priority(t_funnel_result, acquired_articles)
    ref_candidates = acquired_article_images_for_existing_REF(
        t_funnel_result, acquired_articles)

**Codex-owned minimum hook:** merge these ref_candidates into the EXISTING visual_identity.candidates / already-present Live visual queue; pass priority to the existing research priority mechanism. Do not create a new worker, parser, truth/POI layer or independent match gate. The adapter yields native web: article candidates, each with real original reference_image_urls, article_media metadata, discovery=web_article_media, identity_eligible=False. LiveVisualComparisonMixin._image_entries and identity_references.reference_images already accept them. Only an actual new SOURCE+REF comparison whose model explicitly supplies reference_subject_candidate_id may reach bind_reference_subject, visual_match and canonical POI/facts. The adapter NEVER infers article -> physical building from title or address, and no G/T uncertainty is promoted to identity.

**Real Photo106 SOURCE+G+T+gallery replay with no network or inference calls:** independently CLOSED G initially nominated osm:way:100659357; a different independently CLOSED SOURCE+TEXT response nominated osm:way:150596899. The SHA-verified pair recovers both bodies as active REF peers; 79 of the original 81 OSM bodies remain reversible reserve. Original Prussia39 raw HTML of SID2458 and SID2571 produces 19 distinct usable publisher media links, of which 10 have ALREADY BEEN DOWNLOADED in the saved gallery receipt. Two native web: article REF candidates with 19 original image links are now generated, and all 10 previously acquired image URLs are present. An earlier CLOSED independent SOURCE+gallery model found matching architecture for osm:way:150596899 but deliberately did NOT authorize identity (individual_body_visually_supported_but_not_authorized). Codex may use the normal Live reference comparison; do not relabel that old model response as a new native match.

Private receipts:
- /home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-G106-genuine-independent-closed-G-and-T-preflight.json
- .../t-G106-independent-closed-G-and-T-shortlist-live-v1.json
- .../t-T-photo106-actual-publisher-gallery-v1.json
- .../t-T-photo106-actual-multiimage-REF-mapping-v1.json
- .../prussia-cache.json

T-branch tests (isolated DevCoveer): **145 passed** across funnel, actual article transport, evidence/REF pointers and negative controls. Actual native REF bridge replay passed with the original 19 links and 10 already downloaded assets; this validates compatibility and saves next search work, not deployed E2E. PR#249 has moving-base conflicts with #246; port the small T-owned native adapter and methods into Codex's latest checkout instead of overriding common discovery/proof files.
