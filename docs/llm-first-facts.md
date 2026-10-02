# LLM-first fact intelligence

Date: 2026-10-02.

Street Story's factual layer is **LLM-first**. This is an architectural invariant,
not a prompt preference.

## Responsibility split

Semantic decisions belong to a model:

- extract factual claims from retrieved evidence;
- split compound prose into atomic facts without losing meaning;
- normalize wording while preserving semantics;
- decide whether two formulations are the same fact;
- compare a new fact with the POI history and decide whether it is new, repeated,
  an update, a temporal continuation, a scope difference or a contradiction;
- identify contradictions even when wording/event type differs;
- collect all evidence that supports or contradicts a fact;
- decide whether additional search is needed;
- propose and, when evidence is sufficient, perform arbitration with an explicit
  rationale and confidence.

Deterministic code must **not** infer those meanings from regexes, keywords, dates,
event categories or handcrafted semantic hashes.

The deterministic layer is deliberately small. It may:

- validate JSON shape, types, lengths and bounded cardinalities;
- require HTTPS evidence URLs to have actually been returned by the provider;
- preserve exact provenance, source lineage and official-source metadata;
- enforce resource limits, timeouts, ACL, idempotency and transaction boundaries;
- generate opaque content-addressed ids from **model-provided semantic keys**;
- preserve author checkbox decisions by stable model-provided keys;
- store model decisions, conflicts, arbitration and telemetry;
- refuse malformed/unverifiable model output rather than repair its meaning.

If the model returns poor semantics, improve model context/prompt/tooling or ask for
another model pass. Do not compensate by growing a parallel NLP rules engine.

## Fact curation contract

A fact curation pass receives:

1. the confirmed POI identity;
2. newly grounded source material and exact source/support references;
3. the current topic fact inventory;
4. previously considered POI facts from earlier topics;
5. existing unresolved/resolved conflicts;
6. the author's current concept/intent when relevant.

It returns a complete semantic proposal:

- `semantic_key` — stable across paraphrases of the same real-world claim;
- `text` — short self-contained display wording;
- `novelty` — new / repeated / update / temporal continuation;
- `source_urls[]` — all supporting evidence actually present in input;
- `contradicts_keys[]` — any existing/new semantic keys it conflicts with;
- `relation` where relevant — contradiction / scope difference /
  temporal sequence / source disagreement / uncertain;
- confidence and concise rationale;
- optional `needs_more_search` query when arbitration is premature.

The host verifies references and persists the proposal. It does not independently
re-interpret the claim.

## Mira

Mira is the product-level orchestrator and final model arbiter. In a Live session
she sees the compact POI fact inventory, unresolved conflicts and evidence
summaries. She can request additional grounded search, then record an arbitration.

Backend research may use a bounded research-model call for the same semantic
curation contract when no Live session is active. This is not a separate semantic
algorithm: both paths implement the same model-first contract and persisted schema.

## Multiple sources

Many sources attached to one fact are retained and useful, but source count is not
truth probability. Independent provenance, primary/official status, temporal scope
and the actual support text are context for the model's judgment.

A widely copied false statement can therefore remain contested even with many URLs.

## Token/context discipline

Do not repeatedly feed the whole topic transcript or raw web corpus to Mira.

Use compact structured state:

- current POI identity;
- current fact cards: semantic_key + short text + selected flag;
- source summaries with bounded supporting excerpts;
- unresolved conflicts;
- newly retrieved evidence only;
- short author intent/concept.

Older source bodies remain in durable evidence storage and are fetched only when a
specific fact/conflict needs deeper review. Compaction is a product requirement:
token pressure must be solved by structured state and targeted retrieval, not by
replacing semantic work with regexes.

## Failure behavior

If semantic curation is unavailable because all configured model routes/quota are
exhausted:

- preserve the newly fetched sources/evidence;
- do not promote raw snippets or deterministic guesses into facts;
- mark semantic curation pending and retry later;
- keep the existing fact inventory unchanged.

Fail closed on semantics, not on evidence acquisition.
