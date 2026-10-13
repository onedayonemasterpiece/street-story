package com.onedayonemasterpiece.streetstory

import com.google.gson.JsonParser
import org.junit.Assert.*
import org.junit.Test

class PlaceGroupPresentationTest {
    private fun selection() = JsonParser.parseString("""{"mode":"rough","group_id":"group-A","generation":4,
        "photo_sha256":"photo","candidate_ids":["osm:way:A","osm:way:B"],"status":"resolved",
        "narrowed_candidate_id":"osm:way:B","candidates":[{"candidate_id":"osm:way:B","name":"Дом B"}]}""").asJsonObject

    @Test fun resolution_preserves_group_and_admits_only_a_proved_member() {
        val json = selection()
        val group = requireNotNull(PlaceGroupPresentation.current(json, "photo", 4))
        assertEquals(listOf("osm:way:A", "osm:way:B"), group.candidateIds)
        assertEquals("osm:way:B", group.narrowedCandidateId)
        assertTrue(group.notice.contains("Дом B"))
        json.addProperty("narrowed_candidate_id", "osm:way:foreign")
        val foreign = requireNotNull(PlaceGroupPresentation.current(json, "photo", 4))
        assertEquals("needs_review", foreign.status)
        assertNull(foreign.narrowedCandidateId)
        assertFalse(foreign.notice.contains("foreign"))
    }

    @Test fun another_photo_generation_or_exact_mode_never_restores_the_old_group() {
        val json = selection()
        assertNull(PlaceGroupPresentation.current(json, "new-photo", 4))
        assertNull(PlaceGroupPresentation.current(json, "photo", 5))
        json.addProperty("mode", "exact")
        assertNull(PlaceGroupPresentation.current(json, "photo", 4))
        json.addProperty("mode", "rough")
        json.add("candidate_ids", JsonParser.parseString("""["osm:way:A","osm:way:A"]"""))
        assertNull(PlaceGroupPresentation.current(json, "photo", 4))
    }

    @Test fun ambiguous_question_has_the_same_photo_and_generation_fence() {
        val json = selection().apply { addProperty("status", "needs_review"); remove("narrowed_candidate_id") }
        val question = JsonParser.parseString("""{"question":"Какой из двух домов виден?","photo_sha256":"photo","generation":4}""").asJsonObject
        assertEquals("Какой из двух домов виден?", PlaceGroupPresentation.current(json, "photo", 4, question)?.notice)
        question.addProperty("generation", 3)
        assertFalse(requireNotNull(PlaceGroupPresentation.current(json, "photo", 4, question)).notice.contains("из двух"))
    }
}
