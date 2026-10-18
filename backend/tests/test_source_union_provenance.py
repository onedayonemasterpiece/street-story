import copy
import json

from street_story.model_facts import merge_source_payloads
from test_fact_ledger import create_story, persist
from street_story.db import Store


def sources():
    return [{"url": "https://example.org/body", "source_version_id": version,
             "chunk_id": chunk, "supports": [{"kind": "page_excerpt", "text": "Own passage."}]}
            for version, chunk in [("v1", "chunk1"), ("v2", "chunk2"), ("v2", "chunk3")]]


def coordinates(items):
    return {(s["source_version_id"], s["chunk_id"]) for item in items for s in item["supports"]}


def test_union_freezes_effective_revision_and_locator_before_same_url_merge():
    raw = sources()
    unchanged = copy.deepcopy(raw)
    together = merge_source_payloads(raw)
    incremental = merge_source_payloads(merge_source_payloads(raw[:1]), raw[1:])
    assert coordinates(together) == {("v1", "chunk1"), ("v2", "chunk2"), ("v2", "chunk3")}
    assert incremental == together == merge_source_payloads(together, raw)
    assert raw == unchanged


def test_explicit_passage_revision_and_locator_override_parent():
    raw = sources()[0]
    raw["supports"][0].update(source_version_id="own", chunk_id="own-chunk", span_start=2, span_end=14)
    assert coordinates(merge_source_payloads([raw])) == {("own", "own-chunk")}


def test_durable_union_matches_in_batch_without_cross_version_reassignment(tmp_path):
    store = Store(tmp_path / "union.sqlite3")
    story = create_story(store)
    raw = sources()
    item = {"claim_key": "same", "text": "Own claim.", "confidence": .9, "sources": raw[:1]}
    fact_id = persist(store, story, [item], "run", "batch1")[0]
    persist(store, story, [{**item, "existing_fact_id": fact_id, "sources": raw[1:]}], "run", "batch2")
    with store.connection() as db:
        saved = json.loads(db.execute("SELECT sources_json FROM facts WHERE fact_id=?", (fact_id,)).fetchone()[0])
        rows = db.execute("SELECT source_version_id, chunk_id FROM fact_evidence_spans").fetchall()
    assert coordinates(saved) == coordinates(merge_source_payloads(raw))
    assert {(r[0], r[1]) for r in rows} == {("v1", "chunk1"), ("v2", "chunk2"), ("v2", "chunk3")}
