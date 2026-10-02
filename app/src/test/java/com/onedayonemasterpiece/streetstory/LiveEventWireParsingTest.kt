package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveEventWireParsingTest {
    @Test
    fun `mixed live event state shapes remain parseable`() {
        val page = Gson().fromJson(
            """
            {
              "session_id": "live_1",
              "cursor": 2,
              "events": [
                {
                  "seq": 1,
                  "type": "product_state",
                  "state": {"last_change": "Черновик обновлён"}
                },
                {
                  "seq": 2,
                  "type": "publication_confirmation",
                  "confirmation_id": "confirm_1",
                  "state": "prepared",
                  "destinations": ["street_story_e2e_20260928_tg"]
                }
              ]
            }
            """.trimIndent(),
            LiveEventsWire::class.java,
        )

        assertTrue(page.events[0].state?.isJsonObject == true)
        assertEquals("Черновик обновлён", page.events[0].state!!.asJsonObject["last_change"].asString)
        assertTrue(page.events[1].state?.isJsonPrimitive == true)
        assertEquals("prepared", page.events[1].state!!.asString)
        assertEquals("confirm_1", page.events[1].confirmationId)
    }
}
