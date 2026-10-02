import json
from types import SimpleNamespace

import pytest

from street_story.db import Store
from street_story.fact_conflicts import (
    conflict_candidate_pairs,
    normalize_conflict_records,
    persist_fact_conflicts,
)


def fact(text, *, fact_id, source_urls=(), official=False):
    return {
        "fact_id": fact_id,
        "claim_key": "",
        "text": text,
        "confidence": 0.9,
        "evidence_supported": True,
        "selected": True,
        "sources": [
            {
                "type": "official" if official and index == 0 else "web",
                "title": "Source",
                "url": url,
            }
            for index, url in enumerate(source_urls)
        ],
    }


def test_candidate_pairs_keep_source_multiplicity_as_evidence_not_a_vote():
    left = fact(
        "Архитектор — Фридрих Август Штюлер.",
        fact_id="left",
        source_urls=(
            "https://a.example/1",
            "https://b.example/2",
            "https://c.example/3",
        ),
    )
    right = fact(
        "Архитектор — Август Штюлер.",
        fact_id="right",
        source_urls=("https://official.example/history",),
        official=True,
    )
    pairs = conflict_candidate_pairs([left, right])
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["left"]["evidence"]["source_count"] == 3
    assert pair["left"]["evidence"]["domain_count"] == 3
    assert pair["right"]["evidence"]["official"] is True

    records = normalize_conflict_records(pairs, {
        "conflicts": [{
            "pair_id": pair["pair_id"],
            "relation": "source_disagreement",
            "suggested_resolution": "unresolved",
            "confidence": .78,
            "rationale": "Количество повторов не разрешает расхождение.",
        }]
    })
    assert records[0]["suggested_resolution"] == "unresolved"
    assert records[0]["detector_confidence"] == .78


def test_unknown_or_non_conflict_model_rows_are_not_persisted():
    pairs = conflict_candidate_pairs([
        fact("Построены в 1843 году.", fact_id="a", source_urls=("https://a.example/x",)),
        fact("Построены в 1845 году.", fact_id="b", source_urls=("https://b.example/y",)),
    ])
    assert normalize_conflict_records(pairs, {
        "conflicts": [{
            "pair_id": "conflict_unknown",
            "relation": "contradiction",
            "suggested_resolution": "prefer_left",
            "confidence": 1,
            "rationale": "bad ref",
        }, {
            "pair_id": pairs[0]["pair_id"],
            "relation": "none",
            "suggested_resolution": "both_valid",
            "confidence": .9,
            "rationale": "compatible",
        }],
    }) == []


def _insert_story(store):
    now = store.now()
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "story_conflict001",
                "client-conflict001",
                "a" * 64,
                "image/jpeg",
                "/tmp/photo.jpg",
                "voice-chunks-v2",
                "review",
                "{}",
                now,
                now,
            ),
        )


def test_conflict_ledger_is_durable_and_keeps_statistics(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    _insert_story(store)
    service = SimpleNamespace(store=store)
    pair = conflict_candidate_pairs([
        fact("Построены в 1843 году.", fact_id="a", source_urls=("https://a.example/x",)),
        fact("Построены в 1845 году.", fact_id="b", source_urls=("https://b.example/y",)),
    ])[0]
    records = normalize_conflict_records([pair], {
        "conflicts": [{
            "pair_id": pair["pair_id"],
            "relation": "contradiction",
            "suggested_resolution": "unresolved",
            "confidence": .93,
            "rationale": "Один объект не может иметь две даты завершения в одинаковом смысле.",
        }],
    })

    persist_fact_conflicts(service, "story_conflict001", "wiki:1", records, detector="test")
    persist_fact_conflicts(service, "story_conflict001", "wiki:1", records, detector="test")

    with store.connection() as db:
        row = db.execute(
            "SELECT relation,times_seen,final_resolution,evidence_json FROM fact_conflicts "
            "WHERE story_id=?",
            ("story_conflict001",),
        ).fetchone()
        research = json.loads(db.execute(
            "SELECT research_json FROM stories WHERE id=?",
            ("story_conflict001",),
        ).fetchone()[0])
        telemetry = db.execute(
            "SELECT COUNT(*) FROM live_diagnostics WHERE story_id=? AND source='fact_conflict' "
            "AND event_type='fact_conflict_detected'",
            ("story_conflict001",),
        ).fetchone()[0]

    assert row["relation"] == "contradiction"
    assert row["times_seen"] == 2
    assert row["final_resolution"] is None
    assert json.loads(row["evidence_json"])["left"]["source_count"] == 1
    assert research["fact_conflict_stats"]["total_detected"] == 1
    assert research["fact_conflict_stats"]["open"] == 1
    assert telemetry == 2
