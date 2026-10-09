> **Historical snapshot — superseded for current integration.** This document records an earlier experiment and contains an obsolete proximity-based veto and an obsolete claim that exact postal-string matching is required. **Do not implement those guards.** The current LLM-first, G-shortlist → T → existing Live REF method and actual original 106 native host acceptance are described in [t-g-product-integration-20261009.md](t-g-product-integration-20261009.md). The original experiment is retained only for retrospective evidence, not runtime instructions. Main Codex #246 owns shared orchestration/deploy; PR #249 owns independent T code.

# Street Story T — integration contract after 26 real photographs

**Date:** 2026-10-09. **T owner:** ChatGPT PR #249. **Main integration owner:** Codex PR #246. **G:** PR #247.

## Actual measured experiment

The complete per-photo matrix and public source URLs/individual checksums are in [README](../research/20261009-t-corpus/README.md) and [matrix.json](../research/20261009-t-corpus/matrix.json). Full original images, publisher raw HTML/normalized text and model transport receipts are retained at `/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/` (do not copy private assets into git).

26/26 SOURCE originals SHA-verified. Geographic Prussia39 search returned cards for 22/23 coordinate-backed images; unlocalized 124/127/128 used SOURCE-only model terms and exact bounded Prussia39 title lookup. 115 individual publisher bodies were actually acquired and SHA-verified. Of 20 manually evaluated target articles, 18 were independently discovered in bounded automatic lookup. The geographic SOURCE+TEXT model issued 22 closed responses, of which 18 immediately met the strict schema and one was safely normalized from an inert `type: object` schema echo. Three responses had malformed schema.

**Final conservative T-only accepted physical IDs: 102, 120, 132.** Source-first separate scene observation did not reliably solve the 131 neighboring school false match; guards reject it when EXIF verified closer physical footprints remain. Six wrong physical ID model proposals were rejected. This corpus experiment is **not general product acceptance**.

## Included in clean T branch (built on actual Codex #246 baseline)

- `identity_architectural_comparison.py`: only **two additive** T admission helpers, `verified_publisher_physical_scope` and `source_subject_competition_guard`. The existing Codex compact prompt, prior geometry correction context, and no arbitrary 8-body truncation remain untouched.
- `identity_architectural_context.py`: carry actual article metadata `address_provenance` to existing source_text_receipt. Prussia39 had this metadata in HTML, but the previous receipt lost the provenance, preventing a safe exact-address join.
- `prussia39.py`: strictly publisher-scoped `title_search(query)`, local CP1251 query, observed DOM card/title/postal metadata; no inferred building identity; no automatic pagination crawl. The rest of the existing Prussia adapter is unchanged.
- `backend/tests`: additive negative and metadata/title tests while retaining Codex's more recent no-cap T source tests.

## Specific shared-file integration in Codex only

The single known acceptance site is `backend/street_story/identity_discovery.py`, function `accept(...)`, immediately after existing `freeze_architectural_text_proof`. Do **not** modify this common file concurrently from PR #249. When a *positive T decision* has yielded a non-null frozen text proof, validate it **before** storing/publishing:

```python
from .identity_architectural_comparison import (
    verified_publisher_physical_scope, source_subject_competition_guard)
subjects = [*observed, *candidates]
article_scope = verified_publisher_physical_scope(
    story, subjects, source_text_receipt, payload['accepted_architectural_text'])
photo_subject = source_subject_competition_guard(
    story, subjects, payload['accepted_architectural_text'])
if (article_scope.get('applicable') and not article_scope.get('supported')) or (
        photo_subject.get('applicable') and not photo_subject.get('supported')):
    # Fail T admission; mark evidence/reason, preserve acquired articles.
    # Do not assert accepted T from LLM's own "physical_binding_resolved".
    # Independently valid G proof must not be cancelled by missing T.
    ...
```

**Important:** the current Codex acceptance method rejects malformed/invalid T before evaluating `accepted_geometry` in the same payload. Codex must choose the appropriate existing outcome/recovery contract to preserve an independently valid G proof when T is nonapplicable; this is a shared orchestration concern, not a new T planner. The two additional T gates are necessary, not sufficient: existing quote/SHA/schema/individual architectural correspondence proof remains mandatory. In `source_subject_competition_guard`, verified camera metadata plus actual closer OSM contour distances is a deliberately conservative T-only fail-closed condition; use an independently proven G/REF match to resolve a farther building.

## Existing T transport

`prepare_architectural_comparison` and `combine_architectural_decision` are already present on Codex's integration head. **Do not cherry-pick an old full comparator file over Codex's version.** Keep its newer prior candidate coverage and conditional geometry analysis. The acquired full article body is reused through `architectural_text_proof.source_text_receipt.articles` and `headless_facts.acquired_subject_articles` for fact review/POI-memory; no recrawl of the same verified source.

For no-GPS SOURCE, the product's current `identity_location_missing` branch does not itself run the bounded architectural title search. The T component now supplies a native `Prussia39Adapter.title_search`; Codex should invoke it only on a **previously model-nominated SOURCE-visual keyword** (no generated location/address), return verified received article text as T leads, and keep physical ID uncertain without an independent footprint binding. The experiment confirms 127 (specific barracks article lead) and 124/128 (honest insufficient evidence).

## Acceptance / operational boundaries

- No concurrent edits to Codex `identity_discovery.py`, `identity_lifecycle.py`, provider routing, quotas, facts/POI, deployment or G checkout from T branch.
- Re-run backend tests after merging these additive T files with the main branch, then run real product E2E under Codex's provider budget. T report is already complete and should **not** rerun 26 paid provider comparisons just to recheck source-file merging.
- A true `UNKNOWN` model response, an unclosed/unknown send, malformed JSON, missing article, wrong physical hypothesis and an accepted SHA-bound T proof are separate outcomes. Never show publisher search or unit-test green as semantic PASS.
