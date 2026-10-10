# Documentation authority

The owner's later 9 October messages clarify model availability: one Gemini
model's RPD refusal is not a product blocker; registered alternatives, including
Lite, may receive the unchanged proof contract, and `gpt-6-luna` is an optional
secondary spatial route under its account limit. The current implementation
policy is described in [photo search methods](photo-search-methods.md). This
clarification supersedes the earlier single-tertiary / no-Lite routing restriction.

The current photo → physical object → reviewed facts target is defined by the
[unified brief](prompts/street-story-unified-photo-search-20261008.md), supplied by
the owner at commit `68a34bab230d0ae2d537aac3be6d82ee5123ee4b`, amended by the
owner's complete 9 October attachments after the failed canaries at `3a568a7`.
The [next repair step](prompts/street-story-product-unblock-20261009.md) sets
execution order; the unified brief sets method and acceptance requirements. It supersedes
earlier search-method and acceptance instructions. The concise operational scheme
is [Photo search methods](photo-search-methods.md); it does not change that brief.
The eleven critical requirements in `.devcoveer/requirements.json` remain binding.

Earlier documents retain evidence and the history of decisions:

| Document | Current role |
|---|---|
| [Product recovery audit](prompts/street-story-product-recovery-audit-20261008.md) | Measured failures and audit of `db0c0d08024441536a2e59f5c3c433b12c7f4189` |
| [GPS/OSM research](prompts/street-story-gps-osm-research-20261008.md) | Original metadata, candidate coverage and geometry limitations |
| [Geometry/Wiki shortlist](prompts/street-story-geometry-wiki-shortlist-20261008.md) | Early Wiki and retained scenes; current final five come from the unified brief |
| [Spatial fast path](prompts/street-story-spatial-identity-fast-path-20261008.md) | Independent SOURCE + map proof and conditional pose checks |
| [Old universal strategy](prompts/universal-search-strategy-20261008.txt) | Archived strategy; “Prussia39 only a search hint” is superseded |
| [Photo identity and Stop](photo-identity-and-stop.md), [camera priors](camera-metadata-priors.md) | Historical reference-only resolver; original recovery, metadata validity, privacy and Stop contracts still apply |

In particular, the old mandatory REF, top16/4–6–6 candidate sequence and assumed
Kaliningrad location are not current target requirements. Research maps containing
the answer, known addresses and article IDs are fixture truth, never runtime inputs.

The [backend contract](backend-contract.md), [runtime runbook](backend-runtime.md),
[POI memory](poi-knowledge-base.md) and shared Live framework keep their respective
transport, authorisation, persistence and review requirements. A target document,
offline fixture, source commit or isolated identity match does not prove deployment
or product acceptance. A release report must identify its source/deployed SHA,
manifest, provider configuration and actual case receipts.

The [current recovery validation](reports/product-recovery-validation-20261009.md)
separates prepared corrections from measured live outcomes and records remaining
acceptance gates and observed usage.
