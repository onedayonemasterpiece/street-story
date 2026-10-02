from street_story.fact_quality import atomic_fact_text, atomic_fact_texts, merge_fact_inventory, semantic_fact_key


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


def test_noisy_multi_sentence_source_is_reduced_to_one_atomic_fact():
    text = (
        "Королевские ворота снесли, а вместо них в 1843 году решили построить новые. "
        "На закладке первого камня присутствовал король. Далее следует длинное описание страницы."
    )
    assert atomic_fact_text(text) == "В 1843 году решили построить новые Королевские ворота."


def test_personal_review_with_year_is_not_promoted_to_fact():
    assert atomic_fact_text(
        "Королевские ворота — достопримечательность Калининграда. "
        "Я побывала здесь в октябре 2012 года и советую зайти внутрь."
    ) is None


def test_source_heading_does_not_override_clean_later_sentence():
    text = (
        "История создания Королевских ворот. "
        "Сохранившееся здание заложили в 1843 году и завершили в 1850 году."
    )
    assert atomic_fact_text(text) == (
        "Сохранившееся здание заложили в 1843 году и завершили в 1850 году."
    )


def test_date_only_and_navigation_phrases_are_not_facts():
    for text in (
        "Во время Семилетней войны 1756-1763 гг.",
        "Где находятся на карте и как до них добраться.",
        "А с 2005 года началась их новая жизнь.",
    ):
        assert atomic_fact_text(text) is None


def test_location_and_status_still_require_a_real_predicate():
    assert atomic_fact_text(
        "Расположены на пересечении улицы Фрунзе и Литовского вала."
    ) == "Расположены на пересечении улицы Фрунзе и Литовского вала."
    assert atomic_fact_text(
        "В 2005 году Королевские ворота были символом празднования 750-летия Калининграда."
    ) == "В 2005 году Королевские ворота были символом празднования 750-летия Калининграда."


def test_legacy_heading_glued_to_fact_is_stripped_not_dropped():
    assert atomic_fact_text(
        "История создания Королевские ворота были построены в 1843-1850 годах как часть второго вального кольца."
    ) == "Королевские ворота были построены в 1843-1850 годах как часть второго вального кольца."


def test_legacy_interesting_facts_heading_can_salvage_arrival_fact():
    assert atomic_fact_text(
        "Интересные факты Великое посольство прибыло в Кёнигсберг в 1697 году."
    ) == "Великое посольство прибыло в Кёнигсберг в 1697 году."


def test_current_institutional_status_is_a_fact():
    assert atomic_fact_text(
        "Сейчас Королевские ворота — одно из зданий Музея Мирового океана."
    ) == "Сейчас Королевские ворота — одно из зданий Музея Мирового океана."


def test_legacy_item_can_yield_multiple_atomic_facts_with_same_evidence():
    raw = (
        "В 2005 году Королевские ворота были символом юбилея города. "
        "С того же года в воротах размещается центр «Великое посольство», являющийся филиалом музея."
    )
    facts = atomic_fact_texts(raw)
    assert facts == [
        "В 2005 году Королевские ворота были символом юбилея города.",
        "С 2005 года в воротах размещается центр «Великое посольство», являющийся филиалом музея.",
    ]


def test_architect_clause_is_extracted_as_its_own_fact():
    raw = (
        "Королевские ворота были возведены в 1850 году по проекту архитектора "
        "Фридриха Августа Штюлера, известного своими работами."
    )
    facts = atomic_fact_texts(raw)
    assert "Проект архитектора Фридриха Августа Штюлера." in facts
    assert any("возведены в 1850 году" in fact for fact in facts)


def test_bad_caption_prefix_can_be_discarded_while_later_fact_survives():
    raw = (
        "Королевские ворота вечером, фото Wikimedia. "
        "Интересные факты: Великое посольство прибыло в Кёнигсберг в 1697 году."
    )
    facts = atomic_fact_texts(raw)
    assert facts == ["Великое посольство прибыло в Кёнигсберг в 1697 году."]


def test_documented_presence_event_is_a_fact():
    assert atomic_fact_text(
        "На закладке первого камня присутствовал король Фридрих-Вильгельм IV."
    ) == "На закладке первого камня присутствовал король Фридрих-Вильгельм IV."


def test_anniversary_number_is_not_reused_as_calendar_year():
    raw = (
        "В 2005 году Королевские ворота были символом празднования 750-летия Калининграда. "
        "С того же года в воротах размещается центр «Великое посольство»."
    )
    assert atomic_fact_texts(raw)[1].startswith("С 2005 года ")


def test_long_photo_caption_before_arrival_does_not_hide_fact():
    raw = (
        "Королевские ворота вечером (Amber bracelet, CC BY-SA 4.0, via Wikimedia Commons) "
        "Интересные факты Великое посольство, именем которого назван музейный центр, "
        "прибыло в Кёнигсберг в 1697 году."
    )
    facts = atomic_fact_texts(raw)
    assert facts == ["Великое посольство прибыло в Кёнигсберг в 1697 году."]


def test_vague_anniversary_copy_is_not_fact():
    assert atomic_fact_text(
        "Именно они, отреставрированные к 750-у дню рождения города, и украшают собой Калининград."
    ) is None


def test_compound_demolition_and_new_construction_become_separate_facts():
    facts = atomic_fact_texts(
        "Королевские ворота снесли, а вместо них в 1843 году решили построить новые."
    )
    assert facts == [
        "Королевские ворота снесли",
        "В 1843 году решили построить новые Королевские ворота.",
    ]


def test_old_name_and_later_dismantling_are_separate_facts():
    facts = atomic_fact_texts(
        "История Самые ранние ворота имели название Кальтхофские, "
        "однако в начале XVIII века их разобрали."
    )
    assert facts == [
        "Самые ранние ворота имели название Кальтхофские",
        "В начале XVIII века их разобрали",
    ]


def test_reference_markers_are_removed_from_display_fact():
    assert atomic_fact_text(
        "В 2005 году Королевские ворота были символом празднования 750-летия Калининграда [1]."
    ) == "В 2005 году Королевские ворота были символом празднования 750-летия Калининграда."


def test_incomplete_ellipsis_fact_is_rejected():
    assert atomic_fact_text(
        "С 2005 года в воротах размещается Историко-культурный центр «Великое посольство», являющийся филиалом ..."
    ) is None


def test_temporal_prefix_is_kept_when_caption_prefix_is_removed():
    raw = (
        "Королевские ворота в Кёнигсберге в 1928 году "
        "В начале XX столетия ворота потеряли оборонительную функцию и стали городской аркой."
    )
    assert atomic_fact_text(raw) == (
        "В начале XX столетия ворота потеряли оборонительную функцию и стали городской аркой."
    )


def test_demolition_fact_drops_trailing_non_atomic_consequence():
    assert atomic_fact_text(
        "В начале XVIII века их разобрали, и почти четыре десятилетия здесь оставалось пустое место."
    ) == "В начале XVIII века их разобрали"


def test_reason_clause_after_existing_fact_is_not_kept_as_second_fact():
    facts = atomic_fact_texts(
        "На этом месте существовали более ранние ворота, поэтому история самого прохода старше нынешней постройки."
    )
    assert facts == ["На этом месте существовали более ранние ворота"]
