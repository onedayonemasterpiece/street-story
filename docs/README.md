# Documentation authority

[Test-photo registry](testing/photo-registry.md) is the persistent original-byte, EXIF/provenance and evaluator inventory. It links back to the [acceptance procedure](testing/street-story-product-acceptance-20261010.md); frozen inputs and historical results are unchanged.

Documentation baseline: [implementation `e40dba99`](https://github.com/onedayonemasterpiece/street-story/tree/e40dba990ff56a989811cdbd43825e146835fa45).
This docs-only synchronization does not update product code or prove deployment.
Target changes are identified separately and tracked in the [recovery brief](prompts/street-story-universal-product-recovery-20261010.md).

The owner's later 9 October messages clarify model availability: one Gemini
model's RPD refusal is not a product blocker; registered alternatives, including
Lite, may receive the unchanged proof contract, and `gpt-6-luna` is an optional
secondary spatial route under its account limit. The current implementation
policy is described in [photo search methods](photo-search-methods.md). This
clarification supersedes the earlier single-tertiary / no-Lite routing restriction.

The eleven critical requirements in `.devcoveer/requirements.json` remain binding.
The current owner-approved recovery target and execution priorities are the
[10 October universal recovery brief](prompts/street-story-universal-product-recovery-20261010.md).
The [core and end-to-end execution design](design/street-story-first-usable-slice-20261010.md)
is the concrete to-be contract for the next bounded implementation: accepted photo,
autonomous backend work, exact identity/hint recovery, usable facts and reopen.
It combines the necessary dependencies of the first two stages; it is not implemented yet.
The [product acceptance procedure](testing/street-story-product-acceptance-20261010.md) defines the unchanged image corpus, execution protocol and per-input evidence.
The [G/T method evidence audit](reports/street-story-identity-methods-audit-20261010.md) records historical requirements, current working paths, new limits and targeted changes; it does not rewrite the earlier measured day report.
The owner-approved [G method vision](design/street-story-spatial-geometry-vision-20261010.md) preserves the complete spatial/EXIF/morphology intent and maps historical requirements to existing, partial and target behavior.
The recovery brief remains the full-task entrypoint; completing this core slice is a checkpoint, not a substitute for final product verification.
Its [measured day audit](reports/street-story-measured-day-audit-20261010.md)
records observations and limitations; it is not proof that proposed repairs shipped.

The later owner-approved [map-selection MVP design (12 October)](design/street-story-owner-map-selection-mvp-20261012.md) defines the manual MVP path: photo → map → confirmed building or candidate group → sourced facts → saved material → publication. For an exact owner-confirmed building, it explicitly changes the CORE-PHOTO-IDENTITY boundary for this MVP: no additional identity check is required; approximate groups still require identification. It includes changing the confirmed selection within the story and isolating stale results. This is a proposed implementation contract, not evidence of shipped behavior; requirements.json and product code are unchanged.

The [photo search methods](photo-search-methods.md), [backend contract](backend-contract.md),
[runtime runbook](backend-runtime.md) and [POI contract](poi-knowledge-base.md)
describe their domain boundaries. Label current implementation separately from
the recovery target where they differ. The 8 October unified brief and 9 October
product-unblock brief are historical requirements/evidence, superseded only where
the later recovery brief changes them; they no longer set the next execution step.
Historical prompts do not override later requirements. Preserve Stop, UNKNOWN,
source provenance, access controls and publication authorization.

Earlier documents retain evidence and the history of decisions:

| Document | Current role |
|---|---|
| [Product recovery audit](prompts/street-story-product-recovery-audit-20261008.md) | Measured failures and audit of `db0c0d08024441536a2e59f5c3c433b12c7f4189` |
| [GPS/OSM research](prompts/street-story-gps-osm-research-20261008.md) | Original metadata, candidate coverage and geometry limitations |
| [Geometry/Wiki shortlist](prompts/street-story-geometry-wiki-shortlist-20261008.md) | Early Wiki and retained scenes; historical five-case acceptance cohort |
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

The [historical recovery validation](https://github.com/onedayonemasterpiece/street-story/blob/e40dba990ff56a989811cdbd43825e146835fa45/docs/reports/product-recovery-validation-20261009.md)
separates prepared corrections from measured live outcomes and records remaining
acceptance gates and observed usage.
