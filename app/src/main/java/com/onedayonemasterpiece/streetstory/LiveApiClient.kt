package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.annotations.SerializedName
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

class LiveStartWire {
    @SerializedName("session_id") var sessionId: String = ""
    var model: String = ""
    @SerializedName("story_id") var storyId: String = ""
    @SerializedName("text_revision") var textRevision: Int = 0
    var revision: Int = 0
}

class LiveEventWire {
    var seq: Int = 0
    var type: String = ""
    var text: String? = null
    var data: String? = null
    @SerializedName("mime_type") var mimeType: String? = null
    var code: String? = null
    var message: String? = null
    var status: String? = null
    var stage: String? = null
    var active: Boolean? = null
    var state: JsonObject? = null
    @SerializedName("confirmation_id") var confirmationId: String? = null
    @SerializedName("image_url") var imageUrl: String? = null
    @SerializedName("visual_revision") var visualRevision: String? = null
    @SerializedName("scheduled_for") var scheduledFor: String? = null
    var timezone: String? = null
    var destinations: ArrayList<String> = arrayListOf()
}

class LiveEventsWire {
    @SerializedName("session_id") var sessionId: String = ""
    var events: ArrayList<LiveEventWire> = arrayListOf()
    var cursor: Int = 0
    @SerializedName("has_more") var hasMore: Boolean = false
    var gap: Boolean = false
    var closed: Boolean = false
}

class LiveAckWire {
    var ok: Boolean = false
    @SerializedName("session_id") var sessionId: String = ""
}

internal class LiveApiClient(private val baseUrl: String, private val token: <redacted> {
    private val gson = Gson()

    fun start(serverStoryId: String): LiveStartWire =
        request("POST", "/v1/stories/${segment(serverStoryId)}/live-sessions", "{}", LiveStartWire::class.java, 35_000)

    fun inputAudio(serverStoryId: String, sessionId: String, base64Pcm: String): LiveAckWire =
        request(
            "POST",
            "/v1/stories/${segment(serverStoryId)}/live-sessions/${segment(sessionId)}/input",
            gson.toJson(mapOf("audio_base64" to base64Pcm)),
            LiveAckWire::class.java,
            7_000,
        )

    fun inputText(serverStoryId: String, sessionId: String, text: String): LiveAckWire =
        request(
            "POST",
            "/v1/stories/${segment(serverStoryId)}/live-sessions/${segment(sessionId)}/input",
            gson.toJson(mapOf("text" to text)),
            LiveAckWire::class.java,
            7_000,
        )

    fun endAudio(serverStoryId: String, sessionId: String): LiveAckWire =
        request(
            "POST",
            "/v1/stories/${segment(serverStoryId)}/live-sessions/${segment(sessionId)}/input",
            """{"audio_stream_end":true}""",
            LiveAckWire::class.java,
            7_000,
        )

    fun events(serverStoryId: String, sessionId: String, after: Int): LiveEventsWire =
        request(
            "GET",
            "/v1/stories/${segment(serverStoryId)}/live-sessions/${segment(sessionId)}/events?after=${after.coerceAtLeast(0)}",
            null,
            LiveEventsWire::class.java,
            10_000,
        )

    fun stop(serverStoryId: String, sessionId: String): LiveAckWire =
        request(
            "POST",
            "/v1/stories/${segment(serverStoryId)}/live-sessions/${segment(sessionId)}/stop",
            "{}",
            LiveAckWire::class.java,
            7_000,
        )

    private fun <T> request(method: String, path: String, body: String?, type: Class<T>, readTimeoutMs: Int): T {
        val connection = (URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 10_000
            readTimeout = readTimeoutMs
            useCaches = false
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/json")
            setRequestProperty("User-Agent", "StreetStory-Android/${BuildConfig.VERSION_NAME}")
            if (body != null) {
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                doOutput = true
            }
        }
        if (body != null) connection.outputStream.use { it.write(body.toByteArray(StandardCharsets.UTF_8)) }
        val status = connection.responseCode
        if (status !in 200..299) {
            val raw = runCatching { connection.errorStream?.bufferedReader(StandardCharsets.UTF_8)?.use { it.readText() } }.getOrNull().orEmpty()
            val envelope = runCatching { gson.fromJson(raw, LiveErrorEnvelope::class.java) }.getOrNull()
            val code = envelope?.error?.code?.takeIf { it.isNotBlank() } ?: "http_$status"
            val message = envelope?.error?.message?.takeIf { it.isNotBlank() } ?: "Street Story Live returned HTTP $status"
            throw ApiException(status, code, message, status == 408 || status == 425 || status == 429 || status >= 500)
        }
        val text = connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }
        return try { gson.fromJson(text, type) } catch (exc: Exception) {
            throw ApiProtocolException("Malformed Live response: ${exc.message}")
        }
    }

    private fun segment(value: String): String {
        require(value.matches(Regex("[A-Za-z0-9._:-]{1,160}"))) { "unsafe path identifier" }
        return value
    }

    private class LiveErrorEnvelope { var error: WireError? = null }
}