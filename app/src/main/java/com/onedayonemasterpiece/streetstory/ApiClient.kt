package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import com.google.gson.annotations.SerializedName
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.UUID

class ApiException(val status: Int, val code: String, override val message: String, val retryable: Boolean) : IOException(message)
class ApiProtocolException(message: String) : IOException(message)

class StoryWire {
    var id: String = ""
    @SerializedName("client_story_id") var clientStoryId: String = ""
    var state: String = ""
    @SerializedName("place_name") var placeName: String? = null
    var summary: String? = null
    @SerializedName("draft_text") var draftText: String? = null
    @SerializedName("processed_image_url") var processedImageUrl: String? = null
    @SerializedName("scheduled_for") var scheduledFor: String? = null
    @SerializedName("published_at") var publishedAt: String? = null
    var revision: Int = 0
    var error: WireError? = null
    var facts: ArrayList<FactWire> = arrayListOf()
    var destinations: ArrayList<DestinationWire> = arrayListOf()
    @SerializedName("voice_messages") var voiceMessages: ArrayList<VoiceMessageWire> = arrayListOf()
}

class VoiceMessageWire {
    @SerializedName("session_id") var sessionId: String = ""
    var kind: String = ""
    @SerializedName("raw_transcript") var rawTranscript: String? = null
    @SerializedName("display_text") var displayText: String? = null
    @SerializedName("started_at") var startedAt: String? = null
    @SerializedName("ended_at") var endedAt: String? = null
}

class FactWire {
    @SerializedName("fact_id") var factId: String = ""
    var text: String = ""
    var confidence: Double = 0.0
    @SerializedName("evidence_supported") var evidenceSupported: Boolean = false
    var selected: Boolean = false
    var sources: ArrayList<SourceWire> = arrayListOf()
}

class SourceWire {
    var type: String = ""
    var title: String? = null
    var url: String = ""
}

class DestinationWire {
    var alias: String = ""
    var label: String = ""
    var provider: String = ""
    var status: String = ""
    @SerializedName("capability_status") var capabilityStatus: String? = null
    @SerializedName("scheduled_for") var scheduledFor: String? = null
    var selected: Boolean = false
}

class CapabilitiesWire {
    var destinations: ArrayList<DestinationWire> = arrayListOf()
}

class ReceivedChunkWire {
    var index: Int = -1
    var sha256: String = ""
}

class VoiceReceipt {
    @SerializedName("session_id") var sessionId: String = ""
    @SerializedName("recording_finished") var recordingFinished: Boolean = false
    var received: ArrayList<ReceivedChunkWire> = arrayListOf()
}

class WireError {
    var code: String = ""
    var message: String = ""
}

class ApiClient(private val baseUrl: String, private val token: String) {
    private val gson = Gson()

    fun createStory(story: StorySnapshot): StoryWire {
        val boundary = "street-story-${UUID.randomUUID()}"
        val connection = open("POST", "/v1/stories", newRequestKey("create", story.clientStoryId)).apply {
            setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            setRequestProperty("X-Photo-SHA256", story.photoSha256)
            doOutput = true
            setChunkedStreamingMode(64 * 1024)
        }
        DataOutputStream(BufferedOutputStream(connection.outputStream)).use { out ->
            fun field(name: String, value: String) {
                out.writeBytes("--$boundary\r\n")
                out.writeBytes("Content-Disposition: form-data; name=\"$name\"\r\n\r\n")
                out.write(value.toByteArray(StandardCharsets.UTF_8)); out.writeBytes("\r\n")
            }
            field("client_story_id", story.clientStoryId)
            field("photo_sha256", story.photoSha256)
            field("voice_protocol", "voice-chunks-v2")
            story.latitude?.let { field("lat", it.toString()) }
            story.longitude?.let { field("lon", it.toString()) }
            out.writeBytes("--$boundary\r\n")
            out.writeBytes("Content-Disposition: form-data; name=\"photo\"; filename=\"photo\"\r\n")
            out.writeBytes("Content-Type: ${story.photoMimeType}\r\n\r\n")
            File(story.photoPath).inputStream().use { input -> input.copyTo(out, 64 * 1024) }
            out.writeBytes("\r\n--$boundary--\r\n")
        }
        return readJson(connection, StoryWire::class.java)
    }

    fun getStory(serverStoryId: String): StoryWire = requestJson("GET", "/v1/stories/${segment(serverStoryId)}", null, null, StoryWire::class.java)

    fun capabilities(): CapabilitiesWire = requestJson("GET", "/v1/capabilities", null, null, CapabilitiesWire::class.java)

    fun openVoiceSession(serverStoryId: String, session: VoiceSessionSnapshot): VoiceReceipt {
        val body = linkedMapOf<String, Any?>(
            "session_id" to session.sessionId,
            "kind" to session.kind,
            "started_at" to session.startedAt,
            "timezone" to session.timezone,
            "device_label" to session.deviceLabel,
            "capture_policy" to session.capturePolicy,
            "audio" to mapOf(
                "container" to AudioProfile.CONTAINER,
                "codec" to AudioProfile.CODEC,
                "mime_type" to AudioProfile.MIME_M4A,
                "sample_rate_hz" to AudioProfile.SAMPLE_RATE_HZ,
                "channels" to AudioProfile.CHANNELS,
                "target_bitrate_bps" to AudioProfile.BITRATE_BPS,
            ),
            "vad" to mapOf(
                "engine" to EfficientVad.ENGINE_NAME,
                "engine_version" to EfficientVad.ENGINE_VERSION,
                "config_version" to EfficientVad.CONFIG_VERSION,
                "frame_ms" to EfficientVad.FRAME_MS,
                "mode" to EfficientVad.MODE,
            ),
        )
        return requestJson("POST", "/v1/stories/${segment(serverStoryId)}/voice-sessions", gson.toJson(body),
            newRequestKey("voice", session.sessionId), VoiceReceipt::class.java)
    }

    fun uploadChunk(serverStoryId: String, session: VoiceSessionSnapshot, chunk: ChunkRecord): VoiceReceipt {
        val connection = open("PUT", "/v1/stories/${segment(serverStoryId)}/voice-sessions/${segment(session.sessionId)}/chunks/${chunk.chunkIndex}",
            newRequestKey("chunk", "${session.sessionId}-${chunk.chunkIndex}-${chunk.sha256.take(16)}")).apply {
            setRequestProperty("Content-Type", chunk.mimeType)
            setRequestProperty("X-Content-SHA256", chunk.sha256)
            setRequestProperty("X-Audio-Start-Ms", chunk.startMs.toString())
            setRequestProperty("X-Audio-End-Ms", chunk.endMs.toString())
            setRequestProperty("X-Wall-Start-Ms", chunk.wallStartMs.toString())
            setRequestProperty("X-Wall-End-Ms", chunk.wallEndMs.toString())
            doOutput = true
            val file = File(chunk.path)
            setFixedLengthStreamingMode(file.length())
            BufferedOutputStream(outputStream).use { out -> file.inputStream().use { it.copyTo(out, 64 * 1024) } }
        }
        return readJson(connection, VoiceReceipt::class.java)
    }

    fun completeVoice(serverStoryId: String, session: VoiceSessionSnapshot, chunks: List<ChunkRecord>): VoiceReceipt {
        val body = linkedMapOf<String, Any?>(
            "session_id" to session.sessionId,
            "kind" to session.kind,
            "ended_at" to session.endedAt,
            "duration_ms" to session.durationMs,
            "wall_elapsed_ms" to session.wallElapsedMs,
            "manual_pause_ms" to session.manualPauseMs,
            "auto_silence_skipped_ms" to session.autoSilenceSkippedMs,
            "chunk_count" to chunks.size,
            "chunks" to chunks.map { mapOf(
                "index" to it.chunkIndex, "sha256" to it.sha256,
                "start_ms" to it.startMs, "end_ms" to it.endMs,
                "wall_start_ms" to it.wallStartMs, "wall_end_ms" to it.wallEndMs,
                "mime_type" to it.mimeType,
            ) },
        )
        return requestJson("POST", "/v1/stories/${segment(serverStoryId)}/voice-sessions/${segment(session.sessionId)}/complete",
            gson.toJson(body), newRequestKey("complete", session.sessionId), VoiceReceipt::class.java)
    }

    fun mutate(serverStoryId: String, endpoint: String, payloadJson: String, requestKey: String): StoryWire {
        require(endpoint in setOf("facts", "refinements", "visual", "publish", "cancel"))
        return requestJson("POST", "/v1/stories/${segment(serverStoryId)}/$endpoint", payloadJson, requestKey, StoryWire::class.java)
    }

    fun downloadAsset(relativePath: String, target: File): File {
        require(relativePath.startsWith("/")) { "asset URL must be backend-relative" }
        require(!relativePath.contains("..")) { "asset URL traversal is forbidden" }
        val connection = open("GET", relativePath, null)
        val status = connection.responseCode
        if (status !in 200..299) throw apiError(connection, status)
        target.parentFile?.mkdirs()
        val part = File(target.parentFile, target.name + ".part")
        part.delete()
        BufferedInputStream(connection.inputStream).use { input ->
            FileOutputStream(part).use { out -> input.copyTo(out, 64 * 1024); out.fd.sync() }
        }
        check(part.renameTo(target) || runCatching { part.copyTo(target, overwrite = true); part.delete(); true }.getOrDefault(false))
        return target
    }

    private fun <T> requestJson(method: String, path: String, body: String?, key: String?, type: Class<T>): T {
        val connection = open(method, path, key)
        if (body != null) {
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.doOutput = true
            connection.outputStream.use { it.write(body.toByteArray(StandardCharsets.UTF_8)) }
        }
        return readJson(connection, type)
    }

    private fun open(method: String, path: String, key: String?): HttpURLConnection {
        val url = URL(baseUrl.trimEnd('/') + path)
        return (url.openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 35_000
            useCaches = false
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/json")
            setRequestProperty("User-Agent", "StreetStory-Android/${BuildConfig.VERSION_NAME}")
            if (!key.isNullOrBlank()) setRequestProperty("Idempotency-Key", key)
        }
    }

    private fun <T> readJson(connection: HttpURLConnection, type: Class<T>): T {
        val status = connection.responseCode
        if (status !in 200..299) throw apiError(connection, status)
        val text = connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }
        return try { gson.fromJson(text, type) } catch (exc: Exception) { throw ApiProtocolException("Malformed backend response: ${exc.message}") }
    }

    private fun apiError(connection: HttpURLConnection, status: Int): ApiException {
        val raw = runCatching { connection.errorStream?.bufferedReader(StandardCharsets.UTF_8)?.use { it.readText() } }.getOrNull().orEmpty()
        val wire = runCatching { gson.fromJson(raw, ErrorEnvelope::class.java) }.getOrNull()
        val code = wire?.error?.code?.ifBlank { null } ?: "http_$status"
        val message = wire?.error?.message?.ifBlank { null } ?: "Street Story backend returned HTTP $status"
        return ApiException(status, code, message, status == 408 || status == 425 || status == 429 || status >= 500)
    }

    private fun segment(value: String): String {
        require(value.matches(Regex("[A-Za-z0-9._:-]{1,160}"))) { "unsafe path identifier" }
        return value
    }

    private class ErrorEnvelope { var error: WireError? = null }
}
