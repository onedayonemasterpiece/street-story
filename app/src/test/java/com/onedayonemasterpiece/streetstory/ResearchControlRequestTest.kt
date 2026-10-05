package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ResearchControlRequestTest {
    @Test
    fun `outbox HTTP control retains scope key and unwraps current readback`() {
        val photo = "a".repeat(64)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        var header: String? = null
        var body = ""
        server.createContext("/v1/stories/story_1/research-control") { exchange ->
            header = exchange.requestHeaders.getFirst("Idempotency-Key")
            body = exchange.requestBody.bufferedReader().use { it.readText() }
            // An idempotent old Stop returns CURRENT state after a later Resume.
            val result = """{"action":"stop","changed":[],"story":{"id":"story_1","photo_sha256":"$photo","identity_generation":3,"research_controls":{"facts":{"stopped":false,"revision":7,"photo_sha256":"$photo","identity_generation":3}}}}"""
            val bytes = result.toByteArray()
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        try {
            val api = ApiClient("http://127.0.0.1:${server.address.port}", "fixture-device")
            val payload = Gson().toJson(researchControlPayload("stop", "facts", photo, 3))
            val story = api.mutate("story_1", "research-control", payload, "stable-outbox-key")
            assertEquals("stable-outbox-key", header)
            val sent = Gson().fromJson(body, com.google.gson.JsonObject::class.java)
            assertEquals(photo, sent["expected_photo_sha256"].asString)
            assertEquals(3, sent["expected_identity_generation"].asInt)
            assertEquals("facts", sent["purpose"].asString)
            assertFalse(story.researchControls.getValue("facts").stopped)
            assertEquals(7, story.researchControls.getValue("facts").revision)
            assertEquals("story_1", story.id)
        } finally { server.stop(0) }
    }

    @Test
    fun `legacy topic scope stays unknown until canonical readback`() {
        val story = Gson().fromJson("""{"id":"old","identity_progress":{"generation":2}}""", StoryWire::class.java)
        assertEquals(null, story.photoSha256)
        assertEquals(null, story.identityGeneration)
        assertTrue(story.researchControls.isEmpty())
    }

    @Test
    fun `uncertain visual queue polls without an active microphone`() {
        val story = Gson().fromJson("""{"state":"needs_review","research_pending":{"identity":true,"facts":false},"research_controls":{"identity":{"stopped":false},"facts":{"stopped":false}}}""", StoryWire::class.java)
        assertTrue(shouldPollStory(story))
        story.researchPending = mapOf("identity" to false, "facts" to false)
        assertFalse(shouldPollStory(story))
    }

    @Test
    fun `paused identifying does not poll but independent facts still can`() {
        val story = Gson().fromJson("""{"state":"identifying","research_pending":{"identity":false,"facts":false},"research_controls":{"identity":{"stopped":true},"facts":{"stopped":false}}}""", StoryWire::class.java)
        assertFalse(shouldPollStory(story))
        story.researchPending = mapOf("identity" to false, "facts" to true)
        assertTrue(shouldPollStory(story))
    }
}
