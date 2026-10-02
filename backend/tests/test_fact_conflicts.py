import json
from types import SimpleNamespace

import pytest

from street_story.db import Store
from street_story.fact_conflicts import (
    analyze_fact_conflicts,
    conflict_candidate_pairs,
    conflict_rows,
    normalize_conflict_records,
    persist_fact_conflicts,
    resolve_fact_conflict,
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



def test_mira_arbitration_is_separate_from_detector_suggestion(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    _insert_story(store)
    service = SimpleNamespace(store=store)
    pair = conflict_candidate_pairs([
        fact("Архитектор — Иван Иванов.", fact_id="left", source_urls=("https://official.example/a",), official=True),
        fact("Архитектор — Пётр Петров.", fact_id="right", source_urls=("https://news.example/b",)),
    ])[0]
    records = normalize_conflict_records([pair], {
        "conflicts": [{
            "pair_id": pair["pair_id"],
            "relation": "contradiction",
            "suggested_resolution": "prefer_left",
            "confidence": .88,
            "rationale": "Два разных архитектора указаны для одного и того же проекта.",
        }],
    })
    persisted = persist_fact_conflicts(
        service, "story_conflict001", "wiki:1", records, detector="test"
    )[0]
    assert persisted["suggested_resolution"] == "prefer_left"
    assert persisted["final_resolution"] is None

    resolved = resolve_fact_conflict(
        service,
        "story_conflict001",
        persisted["conflict_id"],
        "unresolved",
        "Источников пока недостаточно, чтобы выбрать сторону.",
        .64,
        arbitrated_by="mira_live",
    )
    assert resolved["final_resolution"] == "unresolved"
    assert resolved["final_fact_id"] is None
    assert resolved["arbitration_confidence"] == .64
    assert resolved["arbitrated_by"] == "mira_live"

    # Exact retry is idempotent: the durable row is not duplicated.
    again = resolve_fact_conflict(
        service,
        "story_conflict001",
        persisted["conflict_id"],
        "unresolved",
        "Источников пока недостаточно, чтобы выбрать сторону.",
        .64,
        arbitrated_by="mira_live",
    )
    assert again["conflict_id"] == persisted["conflict_id"]

    with store.connection() as db:
        assert len(conflict_rows(db, "story_conflict001")) == 1
        research = json.loads(db.execute(
            "SELECT research_json FROM stories WHERE id=?",
            ("story_conflict001",),
        ).fetchone()[0])
        arbitration_events = db.execute(
            "SELECT COUNT(*) FROM live_diagnostics WHERE story_id=? AND source='fact_conflict' "
            "AND event_type='fact_conflict_arbitrated'",
            ("story_conflict001",),
        ).fetchone()[0]
    assert research["fact_conflict_stats"]["poi_total_detected"] == 1
    assert research["fact_conflict_stats"]["open"] == 1
    assert arbitration_events == 1


@pytest.mark.asyncio
async def test_auxiliary_detector_failure_is_logged_but_does_not_fail_research(tmp_path):
    from street_story.gemini import GeminiUnavailable

    store = Store(tmp_path / "street-story.sqlite3")
    _insert_story(store)

    class Detector:
        async def detect_fact_conflicts(self, pairs, context):
            raise GeminiUnavailable(None, "quota")

    service = SimpleNamespace(
        store=store,
        providers=SimpleNamespace(gemini=Detector()),
    )
    result = await analyze_fact_conflicts(
        service,
        "story_conflict001",
        "wiki:1",
        [
            fact("Построены в 1843 году.", fact_id="a", source_urls=("https://a.example/x",)),
            fact("Построены в 1850 году.", fact_id="b", source_urls=("https://b.example/y",)),
        ],
        context={"place_name": "Test Gate"},
    )
    assert result == []
    with store.connection() as db:
        events = list(db.execute(
            "SELECT event_type FROM live_diagnostics WHERE story_id=? AND source='fact_conflict'",
            ("story_conflict001",),
        ))
    assert [row["event_type"] for row in events] == ["fact_conflict_detector_unavailable"]


@pytest.mark.asyncio
async def test_model_detector_output_is_bounded_to_known_pairs():
    from street_story.providers import GeminiClient

    pairs = conflict_candidate_pairs([
        fact("Ворота построены в 1843 году.", fact_id="a", source_urls=("https://a.example/x",)),
        fact("Ворота построены в 1850 году.", fact_id="b", source_urls=("https://b.example/y",)),
    ])
    payload = {
        "conflicts": [{
            "pair_id": pairs[0]["pair_id"],
            "relation": "contradiction",
            "suggested_resolution": "unresolved",
            "confidence": .84,
            "rationale": "Даты расходятся.",
        }, {
            "pair_id": "invented",
            "relation": "contradiction",
            "suggested_resolution": "prefer_left",
            "confidence": 1,
            "rationale": "Should be ignored",
        }],
    }

    async def generate(_key, _timeout, _contents, _config, **_kwargs):
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

    class PassingExecutor:
        async def execute(self, _operation, call):
            return await call("test-key", 2)

    fake = SimpleNamespace(
        _generate=generate,
        research_routes=[("gemini-test", object(), object(), PassingExecutor())],
    )
    records = await GeminiClient.detect_fact_conflicts(fake, pairs, {"place_name": "Test"})
    assert len(records) == 1
    assert records[0]["conflict_id"] == pairs[0]["pair_id"]
    assert records[0]["relation"] == "contradiction"
