from street_story.poi_memory import poi_key, prior_facts


class FakeDB:
    def execute(self, _query, args):
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
