package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class OwnerVisualRequestTest {
    private val story = StorySnapshot("story", "server", 0, "photo.jpg", "sha", "image/jpeg", null, null,
        StoryStage.REVIEW, "Дом A", null, "Авторский черновик", null, null, null, null, null, 3)
    private val projection = ResearchProjectionSnapshot("owner_confirmed", "A", "Дом A", emptyList(), emptyList(),
        emptyList(), 1, "r1", emptyList(), photoSha256 = "sha", identityGeneration = 2, sourceAvailable = true)
    private val facts = listOf(FactSnapshot("fact", "Подтверждённый факт", 1.0, true, true, "[]"))

    @Test fun live_ready_callback_cannot_generate_for_changed_house_draft_or_fact_selection() {
        val request = requireNotNull(OwnerVisualRequest.capture(story, projection, facts))
        assertTrue(request.matches("story", story, projection, facts))
        assertFalse(request.matches("another-story", story, projection, facts))
        assertFalse(request.matches("story", story, projection.copy(identityGeneration = 3, candidateId = "B"), facts))
        assertFalse(request.matches("story", story.copy(draftText = "Новый текст"), projection, facts))
        assertFalse(request.matches("story", story, projection, facts.map { it.copy(selected = false) }))
        assertFalse(request.matches("story", story, projection, facts.map { it.copy(text = "Изменённый факт") }))
        assertNull(OwnerVisualRequest.capture(story.copy(stage = StoryStage.VISUAL_PROCESSING), projection, facts))
        assertNull(OwnerVisualRequest.capture(story.copy(processedImageUrl = "https://example/image"), projection, facts))
        assertNull(OwnerVisualRequest.capture(story, projection.copy(photoSha256 = "old"), facts))
    }

    @Test fun rough_visual_requires_a_proved_member_inside_the_current_group() {
        val group = com.google.gson.JsonParser.parseString("""{"mode":"rough","group_id":"group","generation":2,
            "photo_sha256":"sha","candidate_ids":["A","B"],"status":"identifying"}""").asJsonObject
        assertNull(OwnerVisualRequest.capture(story, projection.copy(placeSelection = group), facts))
        group.addProperty("status", "resolved"); group.addProperty("narrowed_candidate_id", "B")
        assertNull(OwnerVisualRequest.capture(story, projection.copy(placeSelection = group), facts))
        group.addProperty("narrowed_candidate_id", "A")
        assertNotNull(OwnerVisualRequest.capture(story, projection.copy(placeSelection = group), facts))
    }

    @Test fun reopened_server_original_is_usable_after_the_local_grant_is_lost() {
        val request = requireNotNull(OwnerVisualRequest.capture(story, projection, facts, localSourceReadable = false))
        assertTrue(request.matches("story", story, projection, facts, localSourceReadable = false))
        assertNull(OwnerVisualRequest.capture(story, projection.copy(sourceAvailable = false), facts, localSourceReadable = false))
        assertNotNull(OwnerVisualRequest.capture(story, projection.copy(sourceAvailable = false), facts, localSourceReadable = true))
        assertNull(OwnerVisualRequest.capture(story, projection.copy(photoSha256 = "old"), facts, localSourceReadable = false))
    }
}
