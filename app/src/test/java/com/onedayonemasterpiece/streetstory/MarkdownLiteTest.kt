package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertTrue
import org.junit.Test

class MarkdownLiteTest {
    @Test fun formats_lists_emphasis_links_and_escapes_ampersands() {
        val html = MarkdownLite.toHtml("# Итог\n- **Факт**\n1. пункт\n[Источник](https://example.com)\nA & B")
        assertTrue(html.contains("<b>Итог</b>"))
        assertTrue(html.contains("• <b>Факт</b>"))
        assertTrue(html.contains("Источник"))
        assertTrue(html.contains("https://example.com"))
        assertTrue(html.contains("A &amp; B"))
    }
}
