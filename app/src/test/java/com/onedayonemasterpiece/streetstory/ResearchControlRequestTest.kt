package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ResearchControlRequestTest {
    @Test
    fun `outbox HTTP control retains scope key and unwraps current readback`() {
        val photo = "a".repeat(64)
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).apply { soTimeout = 5_000 }
        val request = FutureTask<Pair<String?, String>> {
            server.accept().use { socket ->
                socket.soTimeout = 5_000
                val input = socket.getInputStream().bufferedReader(Charsets.ISO_8859_1)
                check(input.readLine() == "POST /v1/stories/story_1/research-control HTTP/1.1")
                val headers = mutableMapOf<String, String>()
                while (true) {
                    val line = input.readLine() ?: error("HTTP request ended before headers")
                    if (line.isEmpty()) break
                    val separator = line.indexOf(':')
                    check(separator > 0)
                    headers[line.substring(0, separator).lowercase()] = line.substring(separator + 1).trim()
                }
                val length = headers.getValue("content-length").toInt()
                check(length in 1..4096)
                val body = CharArray(length)
                var offset = 0
                while (offset < length) {
                    val count = input.read(body, offset, length - offset)
                    check(count > 0) { "HTTP request body was truncated" }
                    offset += count
                }
                // An idempotent old Stop returns CURRENT state after a later Resume.
                val result = """{"action":"stop","changed":[],"story":{"id":"story_1","photo_sha256":"$photo","identity_generation":3,"research_controls":{"facts":{"stopped":false,"revision":7,"photo_sha256":"$photo","identity_generation":3}}}}"""
                val bytes = result.toByteArray(Charsets.UTF_8)
                socket.getOutputStream().apply {
                    write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.US_ASCII))
                    write(bytes)
                    flush()
                }
                headers["idempotency-key"] to String(body)
            }
        }
        val thread = Thread(request, "research-control-test-http").apply { isDaemon = true; start() }
        try {
            val api = ApiClient("http://127.0.0.1:${server.localPort}", "fixture-device")
            val payload = Gson().toJson(researchControlPayload("stop", "facts", photo, 3))
            val story = api.mutate("story_1", "research-control", payload, "stable-outbox-key")
            val (header, body) = request.get(5, TimeUnit.SECONDS)
            assertEquals("stable-outbox-key", header)
            val sent = Gson().fromJson(body, com.google.gson.JsonObject::class.java)
            assertEquals(photo, sent["expected_photo_sha256"].asString)
            assertEquals(3, sent["expected_identity_generation"].asInt)
            assertEquals("facts", sent["purpose"].asString)
            assertFalse(story.researchControls.getValue("facts").stopped)
            assertEquals(7, story.researchControls.getValue("facts").revision)
            assertEquals("story_1", story.id)
        } finally {
            server.close()
            request.cancel(true)
            thread.join(1_000)
        }
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

    @Test
    fun `caption writing in review keeps polling until completion`() {
        val story = Gson().fromJson(
            """{"state":"review","draft_writing":{"started_at":123}}""", StoryWire::class.java)
        assertTrue("writer must keep background sync alive", shouldPollStory(story))
        story.draftWriting = null
        story.draftWritingLast = DraftWritingWire().apply { ok = true }
        story.draftText = "Готовый текст"
        assertFalse("completed writer should stop polling", shouldPollStory(story))
        val previous = ResearchProjectionSnapshot(
            identityStatus = null, candidateId = null, candidateName = null,
            observations = emptyList(), candidates = emptyList(), sources = emptyList(),
            sourceCount = 0, researchRevision = null, researchVoiceIds = emptyList(),
            draftWriting = true,
        )
        assertTrue("newly completed caption is announced", newlyCompletedDraft(previous, null, story))
        assertFalse("ordinary re-sync does not announce again",
            newlyCompletedDraft(previous.copy(draftWriting = false, draftAvailable = true), "Готовый текст", story))
        assertFalse("old story at first sync does not produce an alert", newlyCompletedDraft(null, null, story))
        story.draftWritingLast!!.ok = false
        assertFalse("failed captions are not announced", newlyCompletedDraft(previous, null, story))
    }

    @Test
    fun `finished rewrite between polls announces once and failure never announces`() {
        val story = Gson().fromJson(
            """{"state":"review","revision":19,"draft_text":"Новый текст","draft_composed_by":"writer:gpt-6.1-sol","draft_writing_last":{"ok":true}}""",
            StoryWire::class.java,
        )
        val previous = ResearchProjectionSnapshot(
            identityStatus = null, candidateId = null, candidateName = null,
            observations = emptyList(), candidates = emptyList(), sources = emptyList(),
            sourceCount = 0, researchRevision = null, researchVoiceIds = emptyList(),
            backendRevision = 18, draftWriting = false, draftAvailable = true,
            draftComposedBy = "writer:previous",
        )
        assertTrue("fast rewrite of a ready draft was missed", newlyCompletedDraft(previous, "Старый текст", story))
        assertFalse("same finished revision must not notify twice",
            newlyCompletedDraft(previous.copy(backendRevision = 19), "Новый текст", story))
        story.revision = 20
        assertFalse("a later unrelated revision of the same text does not repeat notification",
            newlyCompletedDraft(previous.copy(backendRevision = 19), "Новый текст", story))
        story.revision = 21
        story.draftText = "Ручная правка Миры"
        story.draftComposedBy = "mira_live"
        assertFalse("manual Mira edit must not notify on stale success",
            newlyCompletedDraft(previous.copy(backendRevision = 20), "Новый текст", story))
        story.revision = 22
        story.draftText = "Правка владельца"
        story.draftComposedBy = "owner_editor"
        assertFalse("owner edit must not notify on stale success",
            newlyCompletedDraft(previous.copy(backendRevision = 21), "Ручная правка Миры", story))
        story.draftWritingLast!!.ok = false
        assertFalse("failed rewrite retains old draft without notification",
            newlyCompletedDraft(previous, "Старый текст", story))
    }
}
