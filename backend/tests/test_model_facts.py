from street_story.model_facts import merge_model_fact_inventory, validated_model_fact_text


def test_model_fact_boundary_is_not_semantic_regex_filter():
    unusual = "Под фигурами размещены их родовые гербы; выше ниш изображены гербы Замланда и Натангии."
    assert validated_model_fact_text(unusual) == unusual


def test_model_fact_boundary_keeps_long_but_bounded_model_fact():
    text = "Факт " + ("очень подробный " * 40)
    assert 500 < len(text) < 1200
    assert validated_model_fact_text(text) == " ".join(text.split())


def test_model_inventory_does_not_drop_facts_for_vocabulary():
    facts = [
        {
            "fact_id": f"f-{i}",
            "text": text,
            "confidence": .8,
            "evidence_supported": True,
            "selected": True,
            "sources": [{"url": f"https://e{i}.example"}],
        }
        for i, text in enumerate([
            "Слева изображён Оттокар II.",
            "В центре изображён Фридрих I.",
            "Справа изображён герцог Альбрехт I.",
            "Под фигурами размещены родовые гербы.",
            "Выше ниш находятся гербы Замланда и Натангии.",
        ])
    ]
    assert len(merge_model_fact_inventory(facts)) == len(facts)
