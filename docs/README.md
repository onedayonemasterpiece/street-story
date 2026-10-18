# Documentation authority

Documentation baseline: [implementation `e40dba99`](https://github.com/onedayonemasterpiece/street-story/tree/e40dba990ff56a989811cdbd43825e146835fa45).
This docs-only synchronization does not update product code or prove deployment.
Target changes are identified separately and tracked in the [recovery brief](prompts/street-story-universal-product-recovery-20261010.md).

The owner's later 9 October messages clarify model availability: one Gemini
model's RPD refusal is not a product blocker; registered alternatives, including
Lite, may receive the unchanged proof contract, and `gpt-6-luna` is an optional
secondary spatial route under its account limit. The current implementation
policy is described in [photo search methods](photo-search-methods.md). This
clarification supersedes the earlier single-tertiary / no-Lite routing restriction.

## Current entrypoint and authority

Read the eleven critical requirements in [the manifest](../.devcoveer/requirements.json)
from the **same worktree and revision as the task**. A generic project name may
resolve to another checkout and an older requirementsContext. Record the selected
checkout/SHA; do not silently substitute the primary checkout's contract.

The manifest incorporates the owner's 12–13 October map-selection decisions and
governing revision14. Keep these paths distinct:
- Automatic identification remains available as its own product path.
- Exact map selection: the owner confirms one building contour; that selection is
  final and does not require model re-identification. A missing name/address is
  missing metadata, not an unknown object or a mandatory question to the owner.
- Approximate selection: the model narrows within the explicitly selected group;
  the group itself is not one accepted POI.
- When contours are unavailable, the existing explicit-point fallback still uses
  a confirmed point with a voice-provided name/address.

For revision14 imported/shared-photo manual creation, original EXIF GPS is required
before story creation and only initializes the map. The selected object's location
determines the city; distance from the camera does not bound selection. Do not
replace this path with a new no-GPS creation flow or mandatory automatic recognition.

Continue from the [backend contract](backend-contract.md) and the
[current map-path acceptance section](testing/street-story-product-acceptance-20261010.md#map-selection-acceptance-revision14).
The test must include a contour with neither name nor address, reverse geocoding
with and without an address, and research without a mandatory owner-address question.
This clarification is an acceptance target, not proof of deployment or a completed run.

The [10 October recovery brief](prompts/street-story-universal-product-recovery-20261010.md),
[core design](design/street-story-first-usable-slice-20261010.md),
[G/T evidence audit](reports/street-story-identity-methods-audit-20261010.md) and
[G vision](design/street-story-spatial-geometry-vision-20261010.md) remain historical
context and automatic-path references where compatible with later owner decisions.
Their frozen corpus and autonomous identity steps do not override revision14 exact
selection or define complete acceptance of the current map-first product.
The [measured day audit](reports/street-story-measured-day-audit-20261010.md) remains
historical evidence, not proof that proposed repairs shipped.

The [photo search methods](photo-search-methods.md), [backend contract](backend-contract.md),
[runtime runbook](backend-runtime.md) and [POI contract](poi-knowledge-base.md)
describe their domain boundaries. Label current implementation separately from
the recovery target where they differ. The 8 October unified brief and 9 October
product-unblock brief are historical requirements/evidence, superseded where later owner decisions and the current manifest change them; they do not set the next execution step.
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
