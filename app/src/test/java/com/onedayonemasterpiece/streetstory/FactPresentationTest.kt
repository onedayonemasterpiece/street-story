package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FactPresentationTest {
    @Test fun reader_failure_does_not_describe_successfully_found_sources_as_unavailable() {
        val outcome = ResearchOutcomeWire().apply {
            this.outcome = "resource_blocked"; reason = "research_output_exhausted"
            purpose = "facts"; photoSha256 = "source"; identityGeneration = 3; coverageComplete = false
        }
        val projection = ResearchProjectionSnapshot(null, null, null, emptyList(), emptyList(), emptyList(),
            0, null, emptyList(), photoSha256 = "source", identityGeneration = 3, researchOutcome = outcome)
        val expected = "Не удалось обработать найденные источники · исследование не завершено"
        assertEquals(expected, FactPresentation.researchOutcomeNotice(projection, "source"))
        outcome.reason = "research_empty_response"
        outcome.outcome = "useful_partial"
        assertEquals(expected, FactPresentation.researchOutcomeNotice(projection, "source"))
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection.copy(identityGeneration = 4), "source"))
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection, "other-source"))
    }

    @Test fun draft_status_shows_writing_and_the_model_that_wrote_the_text() {
        val base = ResearchProjectionSnapshot(null, null, null, emptyList(), emptyList(), emptyList(), 0, null, emptyList())
        assertEquals("Пишу текст…", FactPresentation.draftStatus(base.copy(draftWriting = true)))
        assertEquals("Текст написала модель Gemini Flash-Lite 3.1",
            FactPresentation.draftStatus(base.copy(draftComposedBy = "writer:gemini-3.1-flash-lite")))
        assertEquals("Текст написала модель Gemini Flash 3.5",
            FactPresentation.draftStatus(base.copy(draftComposedBy = "writer:gemini-3.5-flash")))
        assertEquals(null, FactPresentation.draftStatus(base.copy(draftComposedBy = "mira_live")))
        assertTrue(FactPresentation.draftStatus(base.copy(draftWritingFailed = true))!!.startsWith("Текст написать не удалось"))
    }

    @Test fun partial_run_with_enough_facts_reads_as_ready() {
        val outcome = ResearchOutcomeWire().apply {
            this.outcome = "useful_partial"; reason = "research_agent_original_pending"; eligibleCount = 34
            purpose = "facts"; photoSha256 = "source"; identityGeneration = 3; coverageComplete = false
        }
        val projection = ResearchProjectionSnapshot(null, null, null, emptyList(), emptyList(), emptyList(),
            0, null, emptyList(), photoSha256 = "source", identityGeneration = 3, researchOutcome = outcome)
        assertEquals("Найдено 34 подтверждённых фактов · поиск не закончен, можно попросить Миру найти ещё", FactPresentation.researchOutcomeNotice(projection, "source"))
        outcome.eligibleCount = 2
        assertEquals("Поиск не закончен · найдена часть фактов, попросите Миру найти ещё",
            FactPresentation.researchOutcomeNotice(projection, "source"))
    }

    @Test fun unfinished_outcome_requires_current_photo_generation_and_facts_purpose() {
        val wire = com.google.gson.Gson().fromJson("""{"photo_sha256":"source","identity_generation":3,
            "research_outcome":{"outcome":"resource_blocked","reason":"source_selection_unavailable",
            "coverage_complete":false,"eligible_count":0,"purpose":"facts",
            "photo_sha256":"source","identity_generation":3}}""", StoryWire::class.java)
        val projection = ResearchProjectionSnapshot(null, null, null, emptyList(), emptyList(), emptyList(),
            0, null, emptyList(), photoSha256 = wire.photoSha256, identityGeneration = wire.identityGeneration,
            researchOutcome = wire.researchOutcome)
        assertEquals("Исследование не завершено · временная проблема с выбором источников",
            FactPresentation.researchOutcomeNotice(projection, "source"))
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection, "other-source"))
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection.copy(identityGeneration = 4), "source"))
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection.copy(researchPending = mapOf("facts" to true)), "source"))
        wire.researchOutcome!!.purpose = "identity"
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection, "source"))
        wire.researchOutcome!!.purpose = "facts"
        wire.researchOutcome!!.photoSha256 = "other-source"
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection, "source"))
        wire.researchOutcome!!.photoSha256 = "source"
        wire.researchOutcome!!.coverageComplete = true
        assertEquals(null, FactPresentation.researchOutcomeNotice(projection, "source"))
    }

    @Test fun provenance_preserves_same_url_versions_and_does_not_infer_rkb_truth() {
        val gson = com.google.gson.Gson()
        val wire = gson.fromJson("""{"type":"web","url":"https://archive.example/article","title":"Архив",
            "origin":"rkb","rkb_entity_id":"entity-17","revision":"rev-2","source_ref":"knowledge://chunks/own",
            "supports":[{"kind":"verified_page_span","source_version_id":"version-A","page_number":17,
                "span_start":20,"span_end":38,"text":"В 2009 году планировали ремонт."}],
            "bibliography":[{"author":"Автор описания","title":"Каталог","year":2005,"pages":"17–18"}]}""", SourceWire::class.java)
        val later = gson.fromJson("""{"type":"web","url":"https://archive.example/article",
            "supports":[{"source_version_id":"version-B","text":"Работы ещё не начались."}]}""", SourceWire::class.java)
        val retained = gson.fromJson(gson.toJson(listOf(wire, later)), Array<SourceWire>::class.java).toList()
        val details = FactPresentation.sourceDetails(retained)
        listOf("version-A", "version-B", "Страница 17", "планировали ремонт", "ещё не начались", "rev-2", "entity-17", "Автор описания", "17–18").forEach {
            assertTrue(details.contains(it))
        }
        assertTrue(FactPresentation.rkbOrigin(retained.first()))
        assertEquals(false, FactPresentation.rkbOrigin(later))
        val mereHost = SourceWire().apply { type = "web"; url = "https://rkb.example/record" }
        assertEquals(false, FactPresentation.rkbOrigin(mereHost))
        assertEquals("Ожидает проверки", FactPresentation.statusLabel(FactSnapshot("f", "claim", .9, false, false, gson.toJson(retained))))
    }

    @Test fun durable_running_requires_current_source_generation_and_no_stop() {
        val control = ResearchControlWire().apply { photoSha256 = "source"; identityGeneration = 3 }
        val projection = ResearchProjectionSnapshot(null, null, null, emptyList(), emptyList(), emptyList(),
            2, null, emptyList(), photoSha256 = "source", identityGeneration = 3,
            researchControls = mapOf("facts" to control), researchPending = mapOf("facts" to true))
        assertTrue(FactPresentation.researchActive(projection, "source"))
        assertEquals(false, FactPresentation.researchActive(projection, "other-source"))
        assertEquals(false, FactPresentation.researchActive(projection.copy(identityGeneration = 4), "source"))
        control.stopped = true
        assertEquals(false, FactPresentation.researchActive(projection, "source"))
    }

    @Test fun reasons_distinguish_missing_evidence_changed_basis_and_negative_assessment() {
        val fact = FactSnapshot("f", "Текст", .9, false, false, "[]", "withheld")
        assertEquals("Нет собственной опоры в источнике", FactPresentation.statusLabel(fact.copy(eligibilityReason = "own_evidence_missing")))
        assertEquals("Опора изменилась · нужна проверка", FactPresentation.statusLabel(fact.copy(eligibilityReason = "accepted_basis_invalid")))
        assertEquals("Проверка не подтвердила утверждение", FactPresentation.statusLabel(fact.copy(eligibilityReason = "assessment_withheld")))
        assertEquals("Готов к выбору", FactPresentation.statusLabel(fact.copy(evidenceSupported = true)))
    }
    @Test fun source_labels_use_publishers_not_page_titles() {
        val wikipedia = SourceWire().apply {
            url = "https://ru.wikipedia.org/wiki/Test"
            title = "Длинное название страницы"
        }
        val klops = SourceWire().apply {
            url = "https://klops.ru/news/123"
            title = "Заголовок новости"
        }
        val official = SourceWire().apply {
            url = "https://museum.example.org/history"
            type = "official"
        }
        assertEquals("Википедия", FactPresentation.sourceLabel(wikipedia))
        assertEquals("Клопс", FactPresentation.sourceLabel(klops))
        assertEquals("Официальный сайт", FactPresentation.sourceLabel(official))
        assertEquals("ru.wikipedia.org", FactPresentation.sourceHost(wikipedia))
    }

    @Test fun long_fact_is_presented_as_one_compact_thought() {
        val raw = "Первое короткое утверждение. " + "Подробность ".repeat(30)
        val compact = FactPresentation.concise(raw)
        assertTrue(compact.length <= 161)
        assertTrue(compact.startsWith("Первое короткое утверждение"))
    }

    @Test fun strongest_fact_card_shows_only_a_verified_strongest_fact() {
        val plain = FactSnapshot("a", "В 1932 году дом горел.", .9, true, true, "[]")
        val strongest = plain.copy(factId = "b", strongest = true)
        assertEquals("b", FactPresentation.strongest(listOf(plain, strongest))?.factId)
        assertEquals(null, FactPresentation.strongest(listOf(plain)))
        assertEquals(null, FactPresentation.strongest(listOf(strongest.copy(evidenceSupported = false))))
    }
}
