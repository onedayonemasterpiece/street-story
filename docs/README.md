# Documentation authority

`main` is the canonical home for basic documentation, [owner requirements](requirements.md) and the [machine-readable critical manifest](../.devcoveer/requirements.json), [persistent photo registry](testing/photo-registry.md) and [acceptance procedure](testing/street-story-product-acceptance-20261010.md). Working product branches and runtime releases remain separate.

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

## Published clarifications and independent evidence

The [11 October owner priority clarification](prompts/street-story-temporary-core-priority-20261011.md) temporarily changes execution order while preserving the full original requirements; it is dated historical guidance, not a new acceptance gate. The [12 October independent multi-photo comparison](reports/street-story-independent-cases-method-recovery-20261012.md), [case125 method study](reports/street-story-case125-independent-methods-20261012.md), [case125 fact register](reports/street-story-case125-fact-register-20261012.md) and [case130 method study](reports/street-story-case130-independent-methods-20261012.md) retain their stated sources, truth limits and assisted/blind distinctions. They are research evidence, not proof of runtime acceptance.

Canonical consolidation sources: [basic documentation and registry `3fe3fc1`](https://github.com/onedayonemasterpiece/street-story/tree/3fe3fc160843b9e41d79631c03ee513c1de8a123), [historical audit `9e7e511`](https://github.com/onedayonemasterpiece/street-story/tree/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b), [owner priority source](https://github.com/onedayonemasterpiece/street-story/tree/62f08b4f02db4d42111374029cd6da02544a27a5) and [independent reports source](https://github.com/onedayonemasterpiece/street-story/tree/3c45cf7e42775374d41053fe20783ab66081a014). The critical manifest is transferred byte-for-byte from the existing product/documentation baseline; this consolidation creates no new owner requirements.
