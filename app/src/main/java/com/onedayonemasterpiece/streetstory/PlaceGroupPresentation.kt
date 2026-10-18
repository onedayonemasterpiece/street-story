package com.onedayonemasterpiece.streetstory

import com.google.gson.JsonObject

/** A rough choice is a group, even after a member has been proved. */
data class PlaceGroupSnapshot(
    val groupId: String,
    val candidateIds: List<String>,
    val status: String,
    val narrowedCandidateId: String?,
    val narrowedName: String?,
    val features: JsonObject?,
    val question: String? = null,
) {
    val notice: String get() = when (status) {
        "resolved" -> "Определён дом: ${narrowedName ?: narrowedCandidateId} · внутри выбранной группы"
        "needs_review" -> question ?: "Дом в группе пока не определён. Выберите точный дом или измените группу."
        else -> "Примерно · группа из ${candidateIds.size} домов. Уточняем дом внутри группы."
    }
}

object PlaceGroupPresentation {
    fun current(selection: JsonObject?, photoSha256: String?, generation: Int?, clarification: JsonObject? = null): PlaceGroupSnapshot? {
        return runCatching {
            if (selection == null || photoSha256.isNullOrBlank() || generation == null) return null
            fun string(key: String): String? = selection.get(key)?.takeIf { it.isJsonPrimitive }?.asString
            if (string("mode") != "rough" || string("photo_sha256") != photoSha256 ||
                selection.get("generation")?.asInt != generation) return null
            val id = string("group_id")?.takeIf { it.isNotBlank() } ?: return null
            val ids = selection.getAsJsonArray("candidate_ids")?.map { it.asString } ?: return null
            if (ids.size !in 2..16 || ids.any(String::isBlank) || ids.distinct().size != ids.size) return null
            val narrowed = string("narrowed_candidate_id")
            val rawStatus = string("status")
            // A foreign ID can never become the displayed proved member of this group.
            val proved = narrowed?.takeIf { it in ids && rawStatus == "resolved" }
            val status = if (rawStatus !in setOf("identifying", "resolved", "needs_review") || (rawStatus == "resolved" && proved == null)) "needs_review" else rawStatus!!
            val name = selection.getAsJsonArray("candidates")?.firstOrNull {
                val candidate = it.asJsonObject
                (candidate.get("candidate_id") ?: candidate.get("poi_id"))?.asString == proved
            }?.asJsonObject?.get("name")?.takeIf { it.isJsonPrimitive }?.asString
            val question = clarification?.takeIf {
                it["photo_sha256"]?.asString == photoSha256 && it["generation"]?.asInt == generation
            }?.get("question")?.takeIf { it.isJsonPrimitive }?.asString?.takeIf(String::isNotBlank)
            PlaceGroupSnapshot(id, ids, status, proved, name, selection.getAsJsonObject("features"), question)
        }.getOrNull()
    }
}
