package com.onedayonemasterpiece.streetstory

import java.net.URI
import java.util.Locale

internal object FactPresentation {
    private const val MAX_FACT_CHARS = 160

    fun statusLabel(fact: FactSnapshot): String = when {
        fact.evidenceSupported -> "Готов к выбору"
        fact.eligibilityReason == "accepted_basis_invalid" -> "Опора изменилась · нужна проверка"
        fact.eligibilityReason == "own_evidence_missing" -> "Нет собственной опоры в источнике"
        fact.eligibilityReason == "assessment_withheld" -> "Проверка не подтвердила утверждение"
        fact.reviewStatus == "disputed" -> "Есть противоречие"
        fact.reviewStatus in setOf("withheld", "quarantined") -> "Недостаточная опора"
        else -> "Ожидает проверки"
    }

    fun researchActive(projection: ResearchProjectionSnapshot?, photoSha: String): Boolean {
        val control = projection?.researchControls?.get("facts") ?: return false
        return projection.photoSha256 == photoSha && projection.identityGeneration != null &&
            control.photoSha256 == photoSha && control.identityGeneration == projection.identityGeneration &&
            !control.stopped && projection.researchPending["facts"] == true
    }

    fun researchOutcomeNotice(projection: ResearchProjectionSnapshot?, photoSha: String): String? {
        val result = projection?.researchOutcome ?: return null
        if (photoSha.isBlank() || projection.photoSha256 != photoSha ||
            projection.identityGeneration == null || result.photoSha256 != photoSha ||
            result.identityGeneration != projection.identityGeneration || result.purpose != "facts" ||
            projection.researchPending["facts"] == true || result.coverageComplete != false) return null
        return when {
            result.reason == "source_selection_unavailable" ->
                "Исследование не завершено · временная проблема с выбором источников"
            result.reason in setOf("research_output_exhausted", "research_empty_response",
                "research_fact_routes_exhausted", "research_fact_executor_unavailable",
                "research_provider_failed", "research_transport_unavailable") ->
                "Не удалось обработать найденные источники · исследование не завершено"
            result.outcome == "resource_blocked" ->
                "Исследование не завершено · временная ошибка обработки"
            result.outcome == "useful_partial" ->
                "Исследование не завершено · найдена часть подтверждённых фактов"
            else -> "Исследование не завершено"
        }
    }

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

    fun rkbOrigin(source: SourceWire): Boolean = source.origin == "rkb" || source.type == "rkb"

    fun sourceDetails(sources: List<SourceWire>): String = buildList {
        sources.forEach { source ->
            source.title?.takeIf { it.isNotBlank() }?.let { add(it) }
            add(source.url)
            if (rkbOrigin(source)) add("RKB · происхождение записи, не подтверждение утверждения")
            source.rkbEntityId?.takeIf { it.isNotBlank() }?.let { add("Запись: $it") }
            source.revision?.takeIf { it.isNotBlank() }?.let { add("Редакция: $it") }
            source.sourceVersionId?.takeIf { it.isNotBlank() }?.let { add("Версия источника: $it") }
            source.sourceRef?.takeIf { it.isNotBlank() }?.let { add("Ссылка на запись: $it") }
            source.supports.orEmpty().forEach spanLoop@ { span ->
                if (span.text.isBlank()) return@spanLoop
                val page = span.pageNumber ?: span.page
                val heading = listOfNotNull(page?.let { "Страница $it" },
                    (span.sourceVersionId ?: source.sourceVersionId)?.let { "Версия $it" }).joinToString(" · ")
                add(listOf(heading, "«${span.text}»").filter { it.isNotBlank() }.joinToString("\n"))
            }
            source.bibliography?.takeUnless { it.isJsonNull }?.let { bibliography ->
                val entries = if (bibliography.isJsonArray) bibliography.asJsonArray.toList() else listOf(bibliography)
                entries.forEach { entry ->
                    val citation = if (entry.isJsonPrimitive) entry.asString else if (entry.isJsonObject) {
                        listOf("author", "title", "publication", "year", "pages", "citation", "url")
                            .mapNotNull { key -> entry.asJsonObject.get(key)?.takeIf { it.isJsonPrimitive }?.asString }
                            .joinToString(" · ")
                    } else ""
                    if (citation.isNotBlank()) add("Библиография: $citation")
                }
            }
        }
    }.distinct().joinToString("\n\n")

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
