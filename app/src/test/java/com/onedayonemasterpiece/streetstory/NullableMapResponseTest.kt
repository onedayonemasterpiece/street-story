package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonObject
import com.google.gson.JsonParseException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class NullableMapResponseTest {
    private val gson = Gson()

    @Test
    fun `manual photo creation accepts absent and explicitly null map state`() {
        for (fields in listOf("", """
            "place_selection": null, "identity_clarification": null,
            "place_map": {"buildings": null, "streets": null, "selected": null}
        """)) {
            val suffix = if (fields.isEmpty()) "" else ",$fields"
            val wire = gson.fromJson(
                """{"id":"story_104","state":"photo_ready","identity_mode":"manual"$suffix}""",
                StoryWire::class.java,
            )
            assertEquals("story_104", wire.id)
            assertEquals("manual", wire.identityMode)
            assertNull(wire.placeSelection)
            assertNull(wire.identityClarification)
            assertNull(wire.placeMap?.buildings)
            assertNull(wire.placeMap?.streets)
            assertNull(wire.placeMap?.selected)
        }
    }

    @Test
    fun `selected house and map geometry survive wire and projection round trips`() {
        val selected = """{"candidate_id":"osm:relation:3665416","generation":1}"""
        val clarification = """{"question":"Какой корпус?","generation":1}"""
        val features = """{"type":"FeatureCollection","features":[]}"""
        val map = """{"buildings":$features,"streets":null,"selected":$selected}"""
        val wire = gson.fromJson(
            """{"place_selection":$selected,"identity_clarification":$clarification,"place_map":$map}""",
            StoryWire::class.java,
        )
        val reloadedWire = gson.fromJson(gson.toJson(wire), StoryWire::class.java)
        assertEquals(gson.fromJson(selected, JsonObject::class.java), reloadedWire.placeSelection)
        assertEquals(gson.fromJson(clarification, JsonObject::class.java), reloadedWire.identityClarification)
        assertEquals(gson.fromJson(features, JsonObject::class.java), reloadedWire.placeMap?.buildings)
        assertEquals(wire.placeMap?.selected, reloadedWire.placeMap?.selected)

        // SharedPreferences stores these property names rather than the backend wire names.
        for (value in listOf("null", selected)) {
            val serializer = GsonBuilder().serializeNulls().create()
            val snapshot = serializer.fromJson(
                """{"placeSelection":$value,"identityClarification":$value,"placeMap":$map}""",
                ResearchProjectionSnapshot::class.java,
            )
            val reopened = serializer.fromJson(serializer.toJson(snapshot), ResearchProjectionSnapshot::class.java)
            assertEquals(snapshot.placeSelection, reopened.placeSelection)
            assertEquals(snapshot.identityClarification, reopened.identityClarification)
            assertEquals(snapshot.placeMap?.buildings, reopened.placeMap?.buildings)
            assertNull(reopened.placeMap?.streets)
        }
    }

    @Test
    fun `wrong shaped map fields remain protocol errors`() {
        for (invalid in listOf("[]", "true", "42", "\"wrong\"")) {
            for (field in listOf("place_selection", "identity_clarification")) {
                assertThrows(JsonParseException::class.java) {
                    gson.fromJson("""{"$field":$invalid}""", StoryWire::class.java)
                }
            }
            for (field in listOf("buildings", "streets", "selected")) {
                assertThrows(JsonParseException::class.java) {
                    gson.fromJson("""{"place_map":{"$field":$invalid}}""", StoryWire::class.java)
                }
            }
            for (field in listOf("placeSelection", "identityClarification")) {
                assertThrows(JsonParseException::class.java) {
                    gson.fromJson("""{"$field":$invalid}""", ResearchProjectionSnapshot::class.java)
                }
            }
        }
    }
}
