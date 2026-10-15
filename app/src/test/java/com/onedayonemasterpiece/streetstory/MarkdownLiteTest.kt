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

    @Test fun draft_map_links_show_labels_not_raw_urls() {
        val html = MarkdownLite.toHtml("#Калининград\n\n" +
            "[ЯндексКарты](https://yandex.ru/maps/?whatshere%5Bpoint%5D=20.514350%2C54.722708&whatshere%5Bzoom%5D=18)" +
            " · [2ГИС](https://2gis.ru/geo/20.514350%2C54.722708)")
        assertTrue(html.contains("\">ЯндексКарты</a> · <a href=\"https://2gis.ru/geo/20.514350%2C54.722708\">2ГИС</a>"))
        assertTrue(!html.contains("]("))
    }
}
