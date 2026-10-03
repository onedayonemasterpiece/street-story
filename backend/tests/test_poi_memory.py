from street_story.db import Store
from street_story.poi_memory import persist_research_memory, poi_key, prior_facts, processed_sources


class FakeDB:
    def execute(self, _query, args):
        if "FROM poi_aliases" in _query or "FROM poi_research_facts" in _query:
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
