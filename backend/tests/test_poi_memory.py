from street_story.db import Store
from street_story.poi_memory import persist_research_memory, poi_key, prior_facts, processed_sources


class FakeDB:
    def execute(self, _query, args):
        if (
            "FROM poi_aliases" in _query
            or "FROM poi_research_facts" in _query
            or "FROM poi_research_assertions" in _query
        ):
            return []
        assert args[0] == "current"
        assert args[1] == "wiki:1"
        return [
            {
                "fact_id": "architect", "text": "Архитектор — автор проекта.",
                "confidence": .9, "evidence_supported": 1, "selected": 0,
                "sources_json": '[{"type":"official","url":"https://official.example/history"}]',
            },
            {
                "fact_id": "built", "text": "Построен в начале XX века.",
                "confidence": .8, "evidence_supported": 1, "selected": 1,
                "sources_json": '[{"type":"web","url":"https://example.com/history"}]',
            },
            {
                "fact_id": "built", "text": "Дубликат.",
                "confidence": .8, "evidence_supported": 1, "selected": 1,
                "sources_json": "[]",
            },
        ]


def test_poi_key_uses_stable_candidate_id():
    assert poi_key({"candidate_id": "wiki:1", "candidate_name": "Объект"}) == "wiki:1"
    assert poi_key({"candidate_name": "Объект"}) is None


def test_prior_facts_are_unique():
    facts = prior_facts(FakeDB(), {"candidate_id": "wiki:1"}, "current")
    assert [item["fact_id"] for item in facts] == ["architect", "built"]
    assert facts[0]["sources"][0]["type"] == "official"


def test_poi_research_memory_is_independent_of_story_rows(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    source = {
        "type": "web_search",
        "title": "Museum history",
        "url": "https://museum.example/royal-gate",
        "supports": [{"kind": "search_snippet", "source_url": "https://museum.example/royal-gate", "text": "Три скульптуры находятся на фасаде."}],
    }
    fact = {
        "fact_id": "claim-sculptures",
        "claim_key": "royal-gate-facade-sculptures",
        "text": "На фасаде находятся три исторические скульптуры.",
        "confidence": .95,
        "evidence_supported": True,
        "selected": True,
        "sources": [source],
    }
    with store.tx() as db:
        persist_research_memory(db, identity, [fact], [source], "скульптуры Королевских ворот", store.now())
    with store.connection() as db:
        facts = prior_facts(db, identity, "story-that-does-not-exist")
        sources = processed_sources(db, identity)
    assert facts[0]["fact_id"] == "claim-sculptures"
    assert facts[0]["claim_key"] == "royal-gate-facade-sculptures"
    assert facts[0]["origin"] == "poi_research"
    assert facts[0]["selected"] is False
    assert sources[0]["url"] == "https://museum.example/royal-gate"
    assert sources[0]["last_query"] == "скульптуры Королевских ворот"


def test_same_semantic_key_different_values_survive_canonical_poi_memory(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    source_a = {
        "type": "web",
        "title": "A",
        "url": "https://a.example/gate",
        "supports": [{
            "kind": "verified_page_span",
            "source_url": "https://a.example/gate",
            "text": "Строительство началось в 1843 году.",
        }],
    }
    source_b = {
        "type": "web",
        "title": "B",
        "url": "https://b.example/gate",
        "supports": [{
            "kind": "verified_page_span",
            "source_url": "https://b.example/gate",
            "text": "Строительство завершилось в 1850 году.",
        }],
    }
    facts = [
        {
            "fact_id": "claim-start-1843",
            "claim_key": "construction-year",
            "text": "Строительство началось в 1843 году.",
            "confidence": .95,
            "evidence_supported": True,
            "sources": [source_a],
        },
        {
            "fact_id": "claim-finish-1850",
            "claim_key": "construction-year",
            "text": "Строительство завершилось в 1850 году.",
            "confidence": .96,
            "evidence_supported": True,
            "sources": [source_b],
        },
    ]
    with store.tx() as db:
        persist_research_memory(
            db,
            identity,
            facts,
            [source_a, source_b],
            "история строительства",
            store.now(),
            research_run_id="run-poi-two-values",
        )
    with store.connection() as db:
        rows = list(db.execute(
            "SELECT assertion_id,semantic_key,text FROM poi_research_assertions "
            "WHERE poi_key=? ORDER BY assertion_id",
            ("wiki:403645",),
        ))
        observations = list(db.execute(
            "SELECT assertion_id,research_run_id FROM poi_research_observations "
            "WHERE poi_key=? ORDER BY assertion_id",
            ("wiki:403645",),
        ))
        legacy = list(db.execute(
            "SELECT claim_key,fact_id,text FROM poi_research_facts WHERE poi_key=?",
            ("wiki:403645",),
        ))
        hydrated = prior_facts(db, identity, "new-story")

    assert len(rows) == 2
    assert {row["semantic_key"] for row in rows} == {"construction-year"}
    assert {row["assertion_id"] for row in rows} == {"claim-start-1843", "claim-finish-1850"}
    assert len(observations) == 2
    assert {row["research_run_id"] for row in observations} == {"run-poi-two-values"}
    # Legacy compatibility snapshot remains lossy by design but never overwrites.
    assert len(legacy) == 1
    assert len([item for item in hydrated if item.get("origin") == "poi_research"]) == 2


def test_poi_source_passages_merge_instead_of_replace(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    url = "https://same.example/gate"
    first = {
        "type": "web",
        "title": "Same",
        "url": url,
        "supports": [{
            "kind": "search_snippet",
            "source_url": url,
            "text": "На фасаде находятся три фигуры.",
        }],
    }
    second = {
        "type": "web",
        "title": "Same",
        "url": url,
        "supports": [{
            "kind": "verified_page_span",
            "source_url": url,
            "text": "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
        }],
    }
    fact_id = "claim-three-figures"
    with store.tx() as db:
        persist_research_memory(
            db,
            identity,
            [{
                "fact_id": fact_id,
                "claim_key": "facade-figures",
                "text": "На фасаде находятся три фигуры.",
                "confidence": .8,
                "evidence_supported": True,
                "sources": [first],
            }],
            [first],
            "первый проход",
            store.now(),
            research_run_id="run-1",
        )
        persist_research_memory(
            db,
            identity,
            [{
                "fact_id": fact_id,
                "claim_key": "facade-figures",
                "text": "На фасаде находятся три фигуры.",
                "confidence": .9,
                "evidence_supported": True,
                "sources": [second],
            }],
            [second],
            "второй проход",
            store.now() + 1,
            research_run_id="run-2",
        )
    with store.connection() as db:
        remembered = processed_sources(db, identity)
        assertion = db.execute(
            "SELECT sources_json FROM poi_research_assertions "
            "WHERE poi_key=? AND assertion_id=?",
            ("wiki:403645", fact_id),
        ).fetchone()
    assert {
        item["text"] for item in remembered[0]["supports"]
    } == {
        "На фасаде находятся три фигуры.",
        "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
    }
    assertion_sources = __import__("json").loads(assertion["sources_json"])
    assert len(assertion_sources[0]["supports"]) == 2
