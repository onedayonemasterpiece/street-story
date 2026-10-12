package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FactPresentationTest {
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
}
