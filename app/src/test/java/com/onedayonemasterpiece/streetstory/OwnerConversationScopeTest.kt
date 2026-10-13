package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class OwnerConversationScopeTest {
    private val story = StorySnapshot("story", "server", 0, "photo.jpg", "photo", "image/jpeg", 54.7, 20.5,
        StoryStage.REVIEW, "A", null, "Черновик", null, null, null, null, null, 3)
    private val projection = ResearchProjectionSnapshot("owner_confirmed", "A", "A", emptyList(), emptyList(),
        emptyList(), 0, null, emptyList(), photoSha256 = "photo", identityGeneration = 1)

    @Test fun delayed_voice_request_is_bound_to_the_story_photo_generation_and_house() {
        val request = requireNotNull(OwnerConversationScope.capture(story, projection))
        assertTrue(request.matches("story", story, projection))
        assertFalse(request.matches("other-story", story, projection))
        assertFalse(request.matches("story", story.copy(photoSha256 = "other-photo"), projection))
        assertFalse(request.matches("story", story, projection.copy(identityGeneration = 2, candidateId = "B")))
        assertFalse(request.matches("story", story, projection.copy(candidateId = "B")))
        assertFalse(request.matches("story", story.copy(serverStoryId = "other-server-story"), projection))
        assertNull(OwnerConversationScope.capture(story, projection.copy(photoSha256 = "old-photo")))
        assertNull(OwnerConversationScope.capture(story.copy(serverStoryId = null), projection))
    }

    @Test fun gps_gate_does_not_accept_missing_or_invalid_coordinates() {
        val photo = ImportedPhoto("s", "original", "photo", "image/jpeg", 54.7, 20.5)
        PhotoIntake.requireGps(photo)
        for (invalid in listOf(photo.copy(latitude = null), photo.copy(longitude = null),
            photo.copy(latitude = Double.NaN), photo.copy(longitude = 181.0))) {
            try { PhotoIntake.requireGps(invalid); fail("invalid GPS passed intake") }
            catch (error: IllegalArgumentException) { assertEquals(PhotoIntake.GPS_REQUIRED_MESSAGE, error.message) }
        }
    }

    @Test fun map_focus_accepts_remote_area_without_changing_photo_coordinates() {
        val focus = PlaceMapFocusWire().apply { lat = 54.71; lon = 20.52; zoom = 17.0; revision = 4; areaId = "area" }
        assertTrue(focus.valid())
        assertEquals(54.7, story.latitude!!, 0.0)
        assertFalse(PlaceMapFocusWire().apply { lat = Double.NaN; lon = 20.5 }.valid())
        assertFalse(PlaceMapFocusWire().apply { lat = 54.7; lon = 20.5; zoom = 22.0 }.valid())
    }
}
