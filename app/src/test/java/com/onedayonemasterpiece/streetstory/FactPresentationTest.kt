package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FactPresentationTest {
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
