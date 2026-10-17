package com.onedayonemasterpiece.streetstory

/** A delayed Live readiness callback cannot carry an owner request into another house. */
internal data class OwnerConversationScope(
    val storyId: String,
    val serverId: String,
    val photoSha256: String,
    val generation: Int,
    val candidateId: String?,
) {
    fun matches(activeStoryId: String?, story: StorySnapshot?, projection: ResearchProjectionSnapshot?): Boolean {
        if (story == null || projection == null) return false
        return activeStoryId == storyId && story.clientStoryId == storyId && story.serverStoryId == serverId &&
            story.photoSha256 == photoSha256 && projection.photoSha256 == photoSha256 &&
            projection.identityGeneration == generation && projection.candidateId == candidateId
    }

    companion object {
        fun capture(story: StorySnapshot?, projection: ResearchProjectionSnapshot?): OwnerConversationScope? {
            if (story == null || story.serverStoryId.isNullOrBlank() || projection == null ||
                projection.photoSha256 != story.photoSha256 || projection.identityGeneration == null) return null
            return OwnerConversationScope(story.clientStoryId, story.serverStoryId, story.photoSha256,
                projection.identityGeneration, projection.candidateId)
        }
    }
}
