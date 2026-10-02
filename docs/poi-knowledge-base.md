# POI knowledge base

Date: 2026-10-02.

Street Story accumulates a regional POI knowledge base as a by-product of editorial
work. This is initially a logical view over the existing SQLite stories, accepted
object identities and facts, not a new service or database.

## Data model

A POI is keyed by the stable accepted object identity. Each topic keeps
evidence-backed atomic facts for that POI. Facts accumulate instead of being
discarded on each research pass.

An atomic fact is a short concrete statement: a date or period, architect or
founder, reconstruction, change of use, ownership event, demolition attempt,
documented visit, or another specific event. The target presentation length is
about 160 characters. A source name, page title, URL or search-result description
is evidence metadata, not a fact.

## Sources

Sources are attached under the fact. The UI shows a publisher label such as
“Википедия”, “Клопс”, “АиФ” or “Официальный сайт”, not the article title or raw
URL.

Research first tries to find an official source when one exists: an owner,
operator, museum, institution or municipality. Media, Wikipedia, aggregators and
tourism catalogues are not classified as official. An official source is accepted
only when it was actually returned by grounded research. Its supported facts have
editorial priority.

## Reuse across publications

When a later topic concerns the same POI, previously considered facts are passed
to research as novelty context. Unless the author explicitly asks to repeat or
update one, the system should find additional facts instead of retelling the same
set.

“Considered before” includes facts that were not selected for publication. Selection
remains per-topic, so one POI fact base can support different editorial angles.

## Story construction

The POI knowledge base is evidence inventory, not a finished narrative. A probable
story is built later from selected facts, the current publication concept and the
author's intent. Unsupported search snippets are not promoted into durable facts.

This remains inside the existing Street Story backend and SQLite store. A separate
POI service or materialized database should be introduced only if future scale or
query requirements justify it.

## Owner review 2026-10-02 15:20 — quality boundary

The canonical review `voice-20261002-152036-8d69118b` showed that prompt wording
alone was insufficient: article titles, photo/licence metadata and multiple
rephrasings of the same event still entered the visible fact list.

A deterministic fact-quality boundary now sits after research and Live web search:
non-factual titles/media metadata are rejected, text is compacted, and facts are
merged by semantic event key (event type + year/period where available). Evidence
URLs from duplicate claims are unioned, with official sources ordered first.
Re-running research rebuilds the current topic inventory from valid accumulated
facts, which also cleans legacy polluted entries.

Emergency public-web snippets remain useful discovery material but are no longer
promoted to durable facts. A source snippet becomes a fact only after the normal
evidence-backed research path expresses an atomic claim.

## Multiple sources for one fact

A merged fact may retain many evidence URLs. This is desirable: one atomic claim can
be supported by an official page, Wikipedia and several independent publications
without becoming several duplicate checklist items.

Source multiplicity is treated as evidence richness, not as a truth vote. Repetition
across many sites can still propagate the same false claim. Street Story therefore:

- keeps every distinct supporting URL attached to the merged fact;
- keeps the official source first when one exists;
- exposes the number of URLs and distinct sites in the UI;
- does not automatically convert source count into factual certainty;
- preserves source diversity for later editorial judgment and future scoring.

When two differently worded claims merge into the same semantic event, their source
sets are unioned rather than replacing one another.

## Legacy inventory normalization

Older topics created before the atomic-fact boundary may contain article titles,
photo captions or multi-sentence excerpts. The maintenance command
`backend/tools/normalize_fact_inventory.py` is dry-run by default and normalizes
one explicitly named non-published story. It extracts the best atomic factual
sentence, drops non-factual/media text, merges semantic duplicates and unions all
supporting source URLs. It refuses scheduled/published stories and refuses a topic
with an already frozen visual asset.

This migration does not rewrite an authored draft. It only repairs the fact
inventory and its selection map so the existing topic can continue under the new
contract.

## Contradictions and arbitration

Source multiplicity is evidence richness, not majority voting. Street Story therefore
keeps a separate durable contradiction ledger instead of hiding disagreement inside
the merged fact.

After factual extraction, deterministic code selects only plausible competing pairs
(same semantic kind such as construction date, architect, ownership or use). A
bounded research model then classifies each pair as one of:

- `contradiction` — both claims cannot be true in the same meaning;
- `scope_difference` — the wording refers to a different object, period or scope;
- `temporal_sequence` — both can be true at different times;
- `source_disagreement` — sources disagree and current evidence cannot resolve it;
- `uncertain` — there is not enough evidence to classify safely.

The model also records a suggested resolution (`prefer_left`, `prefer_right`,
`both_valid`, or `unresolved`) with confidence and rationale. This suggestion is
not silently applied to fact selection and does not change the publication text.

Every detected conflict is stored durably in SQLite with both claim snapshots,
source/domain counts, official-source presence, detector confidence, suggestion,
first/last observation and repeat count. A matching telemetry event is also written
to the normal diagnostic stream. This allows later measurement of conflict rates,
repeated disagreements and arbitration quality without inventing a UI now.

Mira receives the recent contradiction ledger in Live topic context. When an
unresolved conflict matters to the story she should gather more evidence with
`search_web`; when evidence is sufficient she may record an arbitration through
`resolve_fact_conflict`. Mira's resolution, preferred fact, rationale and confidence
are persisted and logged, but the tool deliberately does not check/uncheck facts or
rewrite the draft. That separation lets us collect real conflict/arbitration data
before deciding how conflicts should be represented to the author.

The read-only `backend/tools/fact_conflict_stats.py` reports aggregate relation,
open/resolved and Mira-arbitrated counts for later analysis.



## Cross-service evolution: Regional Knowledge Base

The earlier “logical view over stories” was sufficient while Street Story was the
only producer. Regional Knowledge Base now creates a second evidence-rich producer
from books and journals, so a stable materialized POI layer is justified.

Street Story remains the canonical POI owner. Regional Knowledge does not create
its own competing POI database.

### Stable POI identity

Introduce a service-owned opaque poi_id and an alias table. Existing accepted
Street Story candidate_id values remain valid aliases, not the permanent primary
identity.

A POI may have aliases for:
- Wikipedia/MediaWiki identity;
- Wikidata QID;
- OSM object identity;
- current and historical names;
- legacy Street Story candidate_id;
- geo/name signatures used only as resolver hints.

External producers submit a poi_locator. They do not declare a canonical poi_id.

If the locator is ambiguous, keep the evidence unattached or create an
identity_ambiguity review case. Never merge two places only because their names
look similar.

### Canonical POI claim model

Move toward the following logical entities without requiring a separate service:

~~~text
pois
poi_aliases
poi_claims
poi_evidence
poi_conflicts
poi_review_cases
poi_review_decisions
expert_profiles
~~~

Existing story facts remain valid producer evidence during migration. They can be
projected into the new POI claim/evidence model without rewriting already published
stories.

A claim is the semantic statement. Evidence is a source-specific observation
supporting or contradicting it. This lets one claim have web evidence and several
book/page evidence records without duplicating the visible fact.

### Knowledge evidence intake

Street Story owns the versioned producer contract:

- docs/contracts/poi-fact-evidence-v1.schema.json
- contract_version = poi.fact_evidence.v1

Knowledge delivery is idempotent. A repeated event with the same idempotency key
must return the same accepted evidence identity or an explicit conflict if the
payload changed.

The accepted envelope preserves:
- source document reference;
- exact Knowledge evidence/page/region references;
- access scope;
- POI locator;
- atomic claim text/event key;
- source-family lineage;
- author/source verification-score snapshot.

Object-store URLs/keys are never copied into the POI database.

### Access scope

POI identity can be globally known while evidence remains private.

~~~text
public POI
  ├── public evidence
  ├── workspace evidence
  └── private evidence owned by one user
~~~

Queries and review projections must apply evidence ACL before exposing claim text,
source references or author/source metadata.

A private book does not become common POI evidence merely because it describes a
public landmark.

## Verification score for books

Book evidence carries an evidence-strength score produced by Regional Knowledge
under a versioned scoring policy.

The first policy uses:

~~~text
A = contextual author authority
M = publication method/editorial quality
P = provenance precision

score = round(0.55*A + 0.25*M + 0.20*P)
~~~

Important rules:

- unknown author authority is null, not a neutral numeric value;
- authority is scoped by subject/geography/period;
- chapter author overrides generic book authorship when known;
- editor/translator authority is not silently attributed to every passage;
- source-family lineage prevents the same upstream claim from masquerading as
  independent corroboration;
- the numeric score is evidence strength, not truth probability.

Street Story may add a bounded corroboration bonus from independent source
families, but unresolved contradiction remains a hard contested-state gate. A
high-scoring claim does not automatically defeat another claim.

## Shared contradiction journal and expert review

The existing durable fact-conflict ledger becomes the foundation of a regional
contradiction journal instead of remaining only a story-local diagnostic.

The existing relation vocabulary remains valid:

- contradiction;
- scope_difference;
- temporal_sequence;
- source_disagreement;
- uncertain.

Add identity_ambiguity for unresolved POI linkage.

A conflict can be detected by Street Story research, a Knowledge book import or a
later source. All observations accumulate under one stable case.

### Canonical review case

Street Story owns the review case and publishes the versioned projection:

- docs/contracts/poi-review-case-v1.schema.json
- contract_version = poi.review_case.v1

Projects Hub is the expert work surface, not another contradiction database.

A review case includes:
- POI;
- competing claim snapshots;
- verification scores and score components;
- exact evidence refs;
- required expertise;
- access scope;
- detector suggestion;
- required number of independent expert reviews.

### Expert routing

Expert identity uses the shared platform issuer + sub.

An expert profile is scoped, not global:

~~~text
expert_id = issuer + sub
geography[]
period[]
subject[]
languages[]
institutional_roles[]
allowed_scopes[]
~~~

Cases are assigned only when the expert has both the required expertise and access
to all evidence required for that review. Private evidence must not be leaked merely
because the user is an expert in that topic.

Ordinary cases may require one expert. High-impact, low-confidence or deliberately
contested cases may require two independent decisions.

### Resolution history

Expert decisions are append-only. Do not overwrite the original detector record or
source snapshots.

Supported expert outcomes:

- prefer_left;
- prefer_right;
- both_valid_scope;
- both_valid_temporal;
- unresolved;
- needs_more_sources;
- wrong_poi_link.

An expert may request more research without selecting a winner.

The canonical fact/POI state changes only after Street Story receives the typed
resolution and records durable readback.

## Projects Hub projection

Projects Hub should show contradiction review as a first-class work item:

- compact POI/context card;
- both claim variants;
- source/author strength, but not a fake “truth meter”;
- exact page/source links when authorized;
- predefined resolution buttons;
- voice rationale through the central Live agent;
- “need more sources” action.

Projects Hub must not copy the evidence corpus or create an independent final truth
record. It reads a Street Story review case and returns a decision to the same case.

## Delivery reliability

Regional Knowledge book finalization must not synchronously depend on Street Story.

Preferred topology:

~~~text
Knowledge finalize
  -> durable POI integration outbox
  -> asynchronous authorized delivery
  -> Street Story idempotent ingest
  -> claim merge/conflict detection
  -> optional Projects Hub review case
~~~

For private user evidence, delivery requires a user-approved Street Story resource
delegation. Public corpus maintenance may use a narrow service identity.

If authorization or Street Story is unavailable, the book remains successfully
finalized and the integration event stays pending.

## Cross-project acceptance

Before calling the bridge ready, prove:

1. Wikipedia, OSM and book evidence converge on one service-owned poi_id.
2. Ambiguous object identity fails to review instead of silent merge.
3. Private evidence remains private through Projects Hub.
4. Author authority null stays null.
5. Context-specific author scoring is reproducible from a versioned policy.
6. Same-source propagation is not counted as independent corroboration.
7. Conflicting high-score claims stay contested.
8. Duplicate events are idempotent.
9. Street Story downtime does not fail Knowledge ingestion.
10. Expert decision updates Street Story and is read back in Projects Hub.
