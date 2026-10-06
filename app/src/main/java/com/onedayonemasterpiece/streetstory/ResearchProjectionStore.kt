package com.onedayonemasterpiece.streetstory

import android.content.Context
import com.google.gson.Gson

data class ResearchCandidateSnapshot(
    val candidateId: String,
    val name: String,
    val type: String,
    val url: String,
)

data class ResearchSourceSnapshot(
    val type: String,
    val title: String,
    val url: String,
)

data class ResearchProjectionSnapshot(
    val identityStatus: String?,
    val candidateId: String?,
    val candidateName: String?,
    val observations: List<String>,
    val candidates: List<ResearchCandidateSnapshot>,
    val sources: List<ResearchSourceSnapshot>,
    val sourceCount: Int,
    val researchRevision: String?,
    val researchVoiceIds: List<String>,
    val identityProgress: IdentityProgressWire? = null,
    val publicationConcept: String? = null,
    val publicationState: String? = null,
    val publicationChannels: List<String> = emptyList(),
    val publicationScheduledFor: String? = null,
    val photoSha256: String? = null,
    val identityGeneration: Int? = null,
    val researchControls: Map<String, ResearchControlWire>? = null,
    val researchPending: Map<String, Boolean> = emptyMap(),
)

class ResearchProjectionStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val gson = Gson()

    fun replace(storyId: String, wire: StoryWire) {
        val identity = wire.visualIdentity
        val snapshot = ResearchProjectionSnapshot(
            identityStatus = identity?.status?.takeIf { it.isNotBlank() },
            candidateId = identity?.candidateId?.takeIf { !it.isNullOrBlank() },
            candidateName = identity?.candidateName?.takeIf { !it.isNullOrBlank() },
            observations = identity?.observations?.filter { it.isNotBlank() } ?: emptyList(),
            candidates = identity?.candidates?.filter { it.candidateId.isNotBlank() && it.name.isNotBlank() }?.map {
                ResearchCandidateSnapshot(it.candidateId, it.name, it.type, it.url)
            } ?: emptyList(),
            sources = wire.sources.filter { it.url.isNotBlank() }.map {
                ResearchSourceSnapshot(it.type, it.title?.takeIf(String::isNotBlank) ?: it.url, it.url)
            },
            sourceCount = wire.sourceCount.coerceAtLeast(0),
            researchRevision = wire.researchRevision?.takeIf { it.isNotBlank() },
            researchVoiceIds = wire.researchVoiceIds.filter { it.isNotBlank() },
            identityProgress = wire.identityProgress,
            publicationConcept = wire.publicationConcept?.takeIf { it.isNotBlank() },
            publicationState = wire.publication?.state?.takeIf { it.isNotBlank() },
            publicationChannels = wire.publication?.destinations?.filter { it.isNotBlank() } ?: emptyList(),
            publicationScheduledFor = wire.publication?.scheduledFor?.takeIf { !it.isNullOrBlank() },
            photoSha256 = wire.photoSha256,
            identityGeneration = wire.identityGeneration,
            researchControls = wire.researchControls,
            researchPending = wire.researchPending,
        )
        check(prefs.edit().putString(key(storyId), gson.toJson(snapshot)).commit()) {
            "research projection was not persisted"
        }
    }

    fun get(storyId: String): ResearchProjectionSnapshot? {
        val raw = prefs.getString(key(storyId), null) ?: return null
        return runCatching { gson.fromJson(raw, ResearchProjectionSnapshot::class.java) }.getOrNull()
    }

    fun clear(storyId: String) {
        prefs.edit().remove(key(storyId)).apply()
    }

    private fun key(storyId: String) = "story:$storyId"

    companion object {
        private const val PREFS = "street_story_research_projection_v1"
    }
}
