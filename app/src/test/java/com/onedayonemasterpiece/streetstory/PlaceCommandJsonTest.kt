package com.onedayonemasterpiece.streetstory

import com.google.gson.JsonParser
import org.junit.Assert.*
import org.junit.Test

class PlaceCommandJsonTest {
    @Test fun fresh_story_states_no_expected_candidate_explicitly() {
        val body = JsonParser.parseString(PlaceCommandJson.encode(linkedMapOf(
            "command_id" to "ss-place-1", "mode" to "exact", "expected_identity_generation" to 0,
            "expected_candidate_id" to null))).asJsonObject
        assertTrue(body.has("expected_candidate_id"))
        assertTrue(body["expected_candidate_id"].isJsonNull)
        assertEquals("exact", body["mode"].asString)
    }

    @Test fun known_candidate_is_kept() {
        val body = JsonParser.parseString(PlaceCommandJson.encode(mapOf("expected_candidate_id" to "osm:way:A"))).asJsonObject
        assertEquals("osm:way:A", body["expected_candidate_id"].asString)
    }
}
