package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FactPresentationTest {
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
