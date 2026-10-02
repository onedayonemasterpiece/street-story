package com.onedayonemasterpiece.streetstory

import java.net.URI
import java.util.Locale

internal object FactPresentation {
    private const val MAX_FACT_CHARS = 160

    fun concise(raw: String): String {
        val text = raw.replace(Regex("\\s+"), " ").trim()
        if (text.length <= MAX_FACT_CHARS) return text
        val boundary = listOf('.', ';').map { text.lastIndexOf(it, MAX_FACT_CHARS - 1) }.maxOrNull() ?: -1
        val end = if (boundary >= 70) boundary + 1 else text.lastIndexOf(' ', MAX_FACT_CHARS - 1)
        return if (end >= 70) text.take(end).trimEnd() + "…" else text.take(MAX_FACT_CHARS - 1).trimEnd() + "…"
    }

    fun sourceHost(source: SourceWire): String =
        runCatching { URI(source.url).host.orEmpty().lowercase(Locale.ROOT) }
            .getOrDefault("")
            .removePrefix("www.")

    fun sourceLabel(source: SourceWire): String {
        if (source.type == "official") return "Официальный сайт"
        val host = sourceHost(source)
        return when {
            host == "wikipedia.org" || host.endsWith(".wikipedia.org") -> "Википедия"
            host == "klops.ru" || host.endsWith(".klops.ru") -> "Клопс"
            host == "aif.ru" || host.endsWith(".aif.ru") -> "АиФ"
            host == "newkaliningrad.ru" || host.endsWith(".newkaliningrad.ru") -> "Новый Калининград"
            host == "culture.ru" || host.endsWith(".culture.ru") -> "Культура.РФ"
            host == "gov39.ru" || host.endsWith(".gov39.ru") -> "Правительство Калининградской области"
            host == "museum-ocean.ru" || host.endsWith(".museum-ocean.ru") -> "Музей Мирового океана"
            host == "visit-kaliningrad.ru" || host.endsWith(".visit-kaliningrad.ru") -> "Visit Kaliningrad"
            host.isNotBlank() -> host.split('.').let { parts ->
                val core = parts.getOrNull((parts.size - 2).coerceAtLeast(0)).orEmpty()
                core.replace('-', ' ').replaceFirstChar { ch ->
                    if (ch.isLowerCase()) ch.titlecase(Locale.forLanguageTag("ru")) else ch.toString()
                }.ifBlank { host }
            }
            else -> "Источник"
        }
    }
}