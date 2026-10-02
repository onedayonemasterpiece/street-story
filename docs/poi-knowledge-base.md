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

