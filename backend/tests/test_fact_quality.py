from street_story.fact_quality import atomic_fact_text, merge_fact_inventory, semantic_fact_key


def test_article_titles_and_media_metadata_are_not_facts():
    for text in (
        "Королевские ворота вечером",
        "История создания Королевских ворот",
        "Фото: Amber Bracket. Лицензия CC BY-SA",
        "Интересные факты о Королевских воротах",
    ):
        assert atomic_fact_text(text) is None


def test_atomic_historical_facts_survive_and_are_compact():
    assert atomic_fact_text("Построены в 1843–1850 годах.") == "Построены в 1843–1850 годах."
    assert atomic_fact_text("Архитектор — Фридрих Август Штюлер.") == "Архитектор — Фридрих Август Штюлер."
    assert atomic_fact_text("Пётр I посещал объект в 1697 году.") == "Пётр I посещал объект в 1697 году."


def test_same_construction_fact_from_different_sources_merges_evidence():
    items = [
        {
            "claim_key": "construction-2005",
            "text": "Восстановлены к юбилею города в 2005 году.",
            "confidence": .91,
            "selected": True,
            "sources": [{"type": "web", "url": "https://a.example/fact"}],
        },
        {
            "claim_key": "jubilee-restoration",
            "text": "В 2005 году завершена реставрация к юбилею города.",
            "confidence": .97,
            "selected": False,
            "sources": [{"type": "official", "url": "https://official.example/history"}],
        },
    ]
    merged = merge_fact_inventory(items)
    assert len(merged) == 1
    assert merged[0]["selected"] is True
    assert merged[0]["confidence"] == .97
    assert [source["type"] for source in merged[0]["sources"]] == ["official", "web"]


def test_semantic_key_uses_event_type_and_year_not_model_wording():
    left = semantic_fact_key("one", "Построены в 1843 году.")
    right = semantic_fact_key("different", "Строительство завершили в 1843 году.")
    assert left == right
