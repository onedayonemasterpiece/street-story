package com.onedayonemasterpiece.streetstory

/** Binds a deferred Live-ready callback to the authoring state that requested it. */
data class OwnerVisualRequest(
    val storyId: String,
    val photoSha256: String,
    val generation: Int,
    val candidateId: String,
    val draft: String,
    val selectedFacts: List<Pair<String, String>>,
) {
    fun matches(activeStoryId: String?, story: StorySnapshot?, projection: ResearchProjectionSnapshot?, facts: List<FactSnapshot>, localSourceReadable: Boolean = false): Boolean =
        activeStoryId == storyId && capture(story, projection, facts, localSourceReadable) == this

    companion object {
        fun capture(story: StorySnapshot?, projection: ResearchProjectionSnapshot?, facts: List<FactSnapshot>, localSourceReadable: Boolean = false): OwnerVisualRequest? {
            if (story == null || projection == null) return null
            if (projection.photoSha256 != story.photoSha256 ||
                projection.identityGeneration == null || projection.identityStatus !in setOf("owner_confirmed", "match") ||
                story.stage in setOf(StoryStage.VISUAL_PROCESSING, StoryStage.SCHEDULING, StoryStage.SCHEDULED, StoryStage.PUBLISHED) ||
                !story.processedImageUrl.isNullOrBlank() || !story.processedImagePath.isNullOrBlank()) return null
            if (projection.sourceAvailable != true && !localSourceReadable) return null
            val candidate = projection.candidateId?.takeIf(String::isNotBlank) ?: return null
            if (projection.placeSelection?.get("mode")?.asString == "rough") {
                val group = PlaceGroupPresentation.current(projection.placeSelection, story.photoSha256, projection.identityGeneration) ?: return null
                if (group.status != "resolved" || group.narrowedCandidateId != candidate) return null
            }
            val draft = story.draftText?.takeIf(String::isNotBlank) ?: return null
            val selected = facts.filter { it.selected && it.evidenceSupported && it.reviewStatus == "eligible" }
                .map { it.factId to it.text }.sortedBy { it.first }
            if (selected.isEmpty()) return null
            return OwnerVisualRequest(story.clientStoryId, story.photoSha256, projection.identityGeneration, candidate, draft, selected)
        }
    }
}
