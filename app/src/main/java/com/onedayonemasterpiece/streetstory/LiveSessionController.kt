package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import com.google.gson.Gson
import com.google.gson.JsonObject
import okhttp3.OkHttpClient
import org.onedayonemasterpiece.live.LiveSocketTransport
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

data class LiveConfirmation(
    val confirmationId: String,
    val text: String?,
    val imageUrl: String?,
    val destinations: List<String>,
    val scheduledFor: String?,
    val timezone: String?,
)

internal object LiveRole {
    const val USER = "user"
    const val ASSISTANT = "assistant"
    const val SYSTEM = "system"
}

data class LiveChatMessage(
    val role: String,
    val text: String,
)

internal object LiveAudioTransportPolicy {
    const val TARGET_PCM_BYTES = LiveSocketTransport.BATCH_BYTES
    const val OUTBOUND_CAPACITY = LiveSocketTransport.ACK_WINDOW
    const val MAX_AUDIO_AGE_MS = LiveSocketTransport.MAX_AGE_MS
}

data class LiveUiState(
    val storyId: String? = null,
    val active: Boolean = false,
    val status: String = "Микрофон выключен",
    val literalMode: Boolean = false,
    val inputActive: Boolean = false,
    val assistantText: String? = null,
    val messages: List<LiveChatMessage> = emptyList(),
    val lastChange: String? = null,
    val confirmation: LiveConfirmation? = null,
    val error: String? = null,
    val completedTurns: Int = 0,
    val transport: String? = null,
)

/** Product UI/state only. Ordered PCM, WSS framing and ACKs belong to the shared SDK. */
class LiveSessionController(context: Context) {
    private val app = context.applicationContext
    private val config = AppGraph.config(app)
    private val store = AppGraph.store(app)
    private val gson = Gson()
    private val generation = AtomicInteger(0)
    private val playbackGeneration = AtomicInteger(0)
    private val pendingPlayback = AtomicInteger(0)
    private val duplexGate = LiveDuplexGate()
    private val playbackSuppressionReported = AtomicBoolean(false)
    private val receivedPcm = AtomicLong(0)
    private val outputAudioChunks = AtomicLong(0)
    private val listeners = CopyOnWriteArrayList<(LiveUiState) -> Unit>()
    private val network = Executors.newCachedThreadPool()
    private val playback = Executors.newSingleThreadExecutor()
    private val clocks = Executors.newSingleThreadScheduledExecutor()
    private val http = OkHttpClient.Builder().connectTimeout(8, TimeUnit.SECONDS).readTimeout(0, TimeUnit.MILLISECONDS).build()
    @Volatile private var state = LiveUiState()
    @Volatile private var serverStoryId: String? = null
    @Volatile private var sessionId: String? = null
    @Volatile private var socket: LiveSocketTransport? = null
    @Volatile private var audioTrack: AudioTrack? = null
    @Volatile private var waitStarted = 0L
    @Volatile private var waitStage = "provider"
    @Volatile private var inputOpen = false
    @Volatile private var userTranscriptIndex = -1
    @Volatile private var assistantTranscriptIndex = -1

    init {
        clocks.scheduleAtFixedRate({
            val start = waitStarted
            if (state.active && start > 0) {
                val elapsed = (SystemClock.elapsedRealtime() - start) / 1000
                if (elapsed >= 15) {
                    val label = when (waitStage) {
                        "tool" -> "Выполняю действие"
                        "transport" -> "Передаю голос"
                        "resource" -> "Ожидаю доступный лимит"
                        else -> "Жду ответа модели"
                    }
                    update(state.copy(status = "$label · ${elapsed / 60}:${(elapsed % 60).toString().padStart(2, '0')}"))
                }
            }
        }, 1, 1, TimeUnit.SECONDS)
    }

    fun snapshot(): LiveUiState = state
    fun addListener(listener: (LiveUiState) -> Unit) { listeners.add(listener); listener(state) }
    fun removeListener(listener: (LiveUiState) -> Unit) { listeners.remove(listener) }
    fun isActiveFor(storyId: String): Boolean = state.active && state.storyId == storyId && !sessionId.isNullOrBlank()
    fun transportEvidence(): Map<String, Any> = mapOf(
        "transport" to (state.transport ?: "off"),
        "shared_native_version" to LiveSocketTransport.VERSION,
        "received_pcm_bytes" to receivedPcm.get(),
        "output_audio_chunks" to outputAudioChunks.get(),
        "event_cursor" to (socket?.cursor() ?: 0),
        "http_audio_fallback" to false,
        "event_polling" to false,
    )

    fun shouldSuppressMicrophoneInput(): Boolean {
        val suppressed = state.active && duplexGate.shouldSuppress(
            SystemClock.elapsedRealtime(),
            pendingPlayback.get(),
        )
        if (suppressed && playbackSuppressionReported.compareAndSet(false, true)) {
            diagnostic(
                "input_suppressed_playback",
                mapOf("pending_playback_bytes" to pendingPlayback.get()),
            )
        } else if (!suppressed && playbackSuppressionReported.compareAndSet(true, false)) {
            diagnostic("input_resumed_after_playback")
        }
        return suppressed
    }

    fun diagnostic(event: String, fields: Map<String, Any?> = emptyMap()) {
        val server = serverStoryId ?: return
        val session = sessionId ?: return
        val base = config.backendUrl ?: return
        val token = config.deviceToken ?: return
        val payload = linkedMapOf<String, Any?>(
            "event" to event.take(80),
            "app_version" to BuildConfig.VERSION_NAME,
            "source_sha" to BuildConfig.SOURCE_SHA.take(40),
            "elapsed_ms" to SystemClock.elapsedRealtime(),
        )
        fields.entries.take(32).forEach { (key, value) ->
            if (key.matches(Regex("[a-zA-Z0-9_.-]{1,64}"))) payload[key] = value
        }
        network.execute {
            runCatching { LiveApiClient(base, token).diagnostic(server, session, payload) }
                .onFailure { Log.w("StreetStoryLive", "diagnostic_send_failed event=$event type=${it.javaClass.simpleName}") }
        }
    }

    fun start(storyId: String, onReady: (Boolean, String?) -> Unit = { _, _ -> }) {
        val server = store.story(storyId)?.serverStoryId
        val base = config.backendUrl
        val token = config.deviceToken
        if (server.isNullOrBlank() || base.isNullOrBlank() || token.isNullOrBlank()) {
            SyncScheduler.enqueue(app)
            update(LiveUiState(storyId = storyId, status = "Синхронизирую тему", error = "Live пока нельзя запустить"))
            onReady(false, "Тема ещё не синхронизирована с backend")
            return
        }
        stopLocal(sendRemote = true)
        val gen = generation.incrementAndGet()
        val notified = AtomicBoolean(false)
        fun ready(ok: Boolean, error: String?) { if (notified.compareAndSet(false, true)) onReady(ok, error) }
        update(LiveUiState(storyId = storyId, status = "Подключаю Live…"))
        network.execute {
            val api = LiveApiClient(base, token)
            try {
                val attempt = "attempt_" + UUID.randomUUID().toString().replace("-", "")
                val started = try {
                    api.start(server, attempt)
                } catch (first: ApiException) {
                    if (first.status !in setOf(429, 503)) throw first
                    update(state.copy(status = "Live занят · повторное подключение…", error = null))
                    Thread.sleep(900)
                    api.start(server, attempt + "_retry")
                }
                if (generation.get() != gen) {
                    runCatching { api.stop(server, started.sessionId) }
                    ready(false, "Подключение отменено")
                    return@execute
                }
                serverStoryId = server
                sessionId = started.sessionId
                if (started.transportProtocol != LiveSocketTransport.PROTOCOL || started.socketTicket.isBlank() || started.socketUrl.isBlank()) {
                    throw ApiProtocolException("Backend не поддерживает согласованный WSS-протокол")
                }
                receivedPcm.set(0)
                outputAudioChunks.set(0)
                duplexGate.reset()
                playbackSuppressionReported.set(false)
                userTranscriptIndex = -1
                assistantTranscriptIndex = -1
                val transport = LiveSocketTransport(http, object : LiveSocketTransport.Listener {
                    override fun onEvent(event: JsonObject) {
                        if (generation.get() == gen) handleEvent(gen, gson.fromJson(event, LiveEventWire::class.java))
                    }
                    override fun onAudio(sequence: Long, pcm: ByteArray, sampleRate: Int) {
                        if (generation.get() == gen) playPcm(gen, pcm, sampleRate)
                    }
                    override fun onFailure(code: String) {
                        val message = humanLiveError(code)
                        fail(gen, message)
                        ready(false, message)
                    }
                    override fun onDiagnostic(fields: Map<String, Any>) {
                        if (generation.get() == gen) {
                            Log.i("StreetStoryLive", gson.toJson(fields + mapOf("session_id" to started.sessionId, "attempt_id" to attempt)))
                            diagnostic("wss_transport", fields)
                        }
                    }
                })
                socket = transport
                transport.connect(base, started.socketUrl, started.socketTicket, started.attemptId, 1, 0).get(12, TimeUnit.SECONDS)
                if (generation.get() != gen) { transport.close(true); return@execute }
                update(
                    state.copy(
                        storyId = storyId,
                        active = true,
                        status = "Слушаю",
                        inputActive = false,
                        transport = "wss",
                        error = null,
                    )
                )
                diagnostic("live_ready", mapOf("transport" to "wss", "attempt_id" to started.attemptId))
                ready(true, null)
            } catch (exc: Exception) {
                if (generation.get() == gen) fail(gen, "Live недоступен: ${safeMessage(exc)}")
                ready(false, safeMessage(exc))
            }
        }
    }

    fun submitPcm(samples: ShortArray) {
        if (!state.active) return
        if (shouldSuppressMicrophoneInput()) {
            if (inputOpen) {
                inputOpen = false
                socket?.endSpeech()
                diagnostic("speech_closed_for_playback")
            }
            return
        }
        if (!inputOpen) {
            inputOpen = true
            waitStarted = 0
            update(state.copy(status = "Слышу вас", inputActive = true, error = null))
            diagnostic("speech_started")
        }
        socket?.submitPcm(samples)
    }

    fun endSpeech() {
        if (!state.active || !inputOpen) return
        inputOpen = false
        socket?.endSpeech()
        waitStage = "transport"
        waitStarted = SystemClock.elapsedRealtime()
        update(state.copy(status = "Передаю реплику", inputActive = false, error = null))
        diagnostic("speech_ended")
    }

    fun sendText(text: String) {
        val value = text.trim()
        if (!state.active || value.isEmpty()) return
        inputOpen = false
        update(state.copy(inputActive = false))
        waitStage = "provider"
        waitStarted = SystemClock.elapsedRealtime()
        update(state.copy(status = "Думаю", error = null))
        socket?.sendText(value.take(4000))
    }

    fun stopLocal(sendRemote: Boolean = true) {
        val server = serverStoryId
        val session = sessionId
        generation.incrementAndGet()
        serverStoryId = null; sessionId = null; inputOpen = false; waitStarted = 0
        duplexGate.reset(); playbackSuppressionReported.set(false)
        userTranscriptIndex = -1; assistantTranscriptIndex = -1
        val old = socket; socket = null; old?.close(sendRemote)
        playbackGeneration.incrementAndGet()
        stopPlayback()
        update(state.copy(active = false, status = "Микрофон выключен", inputActive = false, error = null))
        if (sendRemote) remoteStop(server, session)
    }

    private fun handleEvent(gen: Int, event: LiveEventWire) {
        if (generation.get() != gen) return
        when (event.type) {
            "audio" -> event.data?.let {
                val bytes = runCatching { Base64.decode(it, Base64.DEFAULT) }.getOrNull()
                if (bytes != null) playPcm(gen, bytes, Regex("rate=(\\d+)").find(event.mimeType.orEmpty())?.groupValues?.getOrNull(1)?.toIntOrNull() ?: 24000)
            }
            "output_transcript" -> {
                waitStarted = 0
                val text = event.text?.trim().orEmpty()
                if (text.isNotEmpty()) {
                    assistantTranscriptIndex = mergeMessage(LiveRole.ASSISTANT, text, assistantTranscriptIndex)
                    update(state.copy(status = "Мира отвечает", assistantText = text, error = null))
                }
            }
            "input_transcript" -> {
                waitStage = "provider"
                val text = event.text?.trim().orEmpty()
                if (text.isNotEmpty()) {
                    userTranscriptIndex = mergeMessage(LiveRole.USER, text, userTranscriptIndex)
                    update(state.copy(status = "Думаю", inputActive = false, error = null))
                } else if (waitStarted > 0) {
                    update(state.copy(status = "Думаю", inputActive = false))
                }
            }
            "input_timing" -> {
                waitStage = "provider"
                if (waitStarted > 0) update(state.copy(status = "Думаю", inputActive = false))
            }
            "turn_complete" -> {
                waitStarted = 0
                userTranscriptIndex = -1
                assistantTranscriptIndex = -1
                update(state.copy(status = "Слушаю", inputActive = false, completedTurns = state.completedTurns + 1, error = null))
            }
            "tool_call" -> { waitStage = "tool"; if (waitStarted == 0L) waitStarted = SystemClock.elapsedRealtime(); update(state.copy(status = "Выполняю действие…")) }
            "budget_wait" -> { waitStage = "resource"; waitStarted = SystemClock.elapsedRealtime(); update(state.copy(status = "Ожидаю доступный лимит")) }
            "reconnecting" -> update(state.copy(status = "Восстанавливаю соединение с моделью…"))
            "literal_mode" -> update(state.copy(literalMode = event.active == true, status = if (event.active == true) "Дословная диктовка" else "Слушаю"))
            "product_state" -> {
                SyncScheduler.enqueue(app)
                val product = event.state?.takeIf { it.isJsonObject }?.asJsonObject
                val last = product?.get("last_change")?.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.isNotBlank() }
                update(state.copy(lastChange = last ?: state.lastChange, error = null))
            }
            "tool_result" -> {
                SyncScheduler.enqueue(app)
                waitStage = "provider"
                update(state.copy(status = if (event.status == "error") "Действие не выполнено" else "Обновляю результат…",
                    lastChange = if (event.status == "error") event.code ?: "Ошибка действия" else state.lastChange))
            }
            "publication_confirmation" -> {
                val id = event.confirmationId.orEmpty()
                if (id.isNotBlank()) update(state.copy(
                    confirmation = LiveConfirmation(id, event.text, event.imageUrl, event.destinations.toList(), event.scheduledFor, event.timezone),
                    status = "Проверь публикацию", error = null,
                ))
            }
            "visual_context" -> {
                if (event.status == "ready") {
                    addSystemMessage("Фото добавлено в визуальный контекст Миры.")
                } else {
                    addSystemMessage("Фото не удалось добавить в Live-контекст. Идентификация всё равно сверит исходник отдельным vision-шагом.")
                }
            }
            "interrupted" -> {
                playbackGeneration.incrementAndGet()
                stopPlayback()
                diagnostic("assistant_interrupted", mapOf("received_pcm_bytes" to receivedPcm.get()))
                update(state.copy(status = "Слышу вас", inputActive = true))
            }
            "error" -> fail(gen, humanLiveError(event.code ?: "LIVE_PROVIDER_ERROR"))
            "closed" -> fail(gen, "Live-сессия завершилась. Результат сохранён — включите Live снова.")
        }
    }

    private fun playPcm(gen: Int, bytes: ByteArray, rate: Int) {
        if (generation.get() != gen || bytes.isEmpty()) return
        val playbackEpoch = playbackGeneration.get()
        if (pendingPlayback.addAndGet(bytes.size) > MAX_PLAYBACK_BYTES) {
            pendingPlayback.addAndGet(-bytes.size)
            fail(gen, "LIVE_PLAYBACK_BACKPRESSURE")
            return
        }
        receivedPcm.addAndGet(bytes.size.toLong())
        val chunkNumber = outputAudioChunks.incrementAndGet()
        if (chunkNumber == 1L || chunkNumber % 24L == 0L) {
            diagnostic(
                "playback_received",
                mapOf("chunk" to chunkNumber, "pcm_bytes_total" to receivedPcm.get(), "sample_rate" to rate),
            )
        }
        playback.execute {
            try {
                if (playbackGeneration.get() != playbackEpoch) return@execute
                val track = ensureAudioTrack(rate) ?: throw IllegalStateException("LIVE_PLAYBACK_UNAVAILABLE")
                var offset = 0
                val started = SystemClock.elapsedRealtime()
                while (offset < bytes.size && playbackGeneration.get() == playbackEpoch) {
                    val count = track.write(bytes, offset, bytes.size - offset, AudioTrack.WRITE_BLOCKING)
                    if (count <= 0) throw IllegalStateException("LIVE_PLAYBACK_WRITE")
                    offset += count
                }
                val writeMs = SystemClock.elapsedRealtime() - started
                if (duplexGate.isUnexpectedlySlowWrite(writeMs, bytes.size, rate)) {
                    diagnostic(
                        "playback_slow_write",
                        mapOf(
                            "duration_ms" to writeMs,
                            "expected_pcm_ms" to duplexGate.pcmDurationMs(bytes.size, rate),
                            "pcm_bytes" to bytes.size,
                            "sample_rate" to rate,
                        ),
                    )
                }
            } catch (_: Exception) {
                if (playbackGeneration.get() == playbackEpoch) {
                    Log.w("StreetStoryLive", "LIVE_PLAYBACK_WRITE")
                    diagnostic("playback_error", mapOf("pcm_bytes" to bytes.size, "sample_rate" to rate))
                    if (generation.get() == gen) fail(gen, "Не удалось воспроизвести голос Миры")
                }
            } finally {
                val remaining = pendingPlayback.addAndGet(-bytes.size)
                if (remaining <= 0) duplexGate.onPlaybackDrained(SystemClock.elapsedRealtime())
            }
        }
    }

    @Synchronized private fun ensureAudioTrack(rate: Int): AudioTrack? {
        val current = audioTrack
        if (current != null && current.state == AudioTrack.STATE_INITIALIZED && current.sampleRate == rate) return current
        stopPlayback()
        val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) return null
        return runCatching {
            AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(maxOf(min * 2, rate)).build()
                .also { it.play(); audioTrack = it }
        }.getOrNull()
    }

    @Synchronized private fun stopPlayback() {
        val track = audioTrack ?: return
        audioTrack = null
        runCatching { track.pause() }; runCatching { track.flush() }; runCatching { track.stop() }; runCatching { track.release() }
    }

    private fun fail(gen: Int, message: String) {
        if (!generation.compareAndSet(gen, gen + 1)) return
        val server = serverStoryId; val session = sessionId
        diagnostic("live_failed", mapOf("message" to message.take(240)))
        serverStoryId = null; sessionId = null; waitStarted = 0; inputOpen = false
        duplexGate.reset(); playbackSuppressionReported.set(false)
        userTranscriptIndex = -1; assistantTranscriptIndex = -1
        val old = socket; socket = null; old?.close(false)
        // Do not increment playbackGeneration here: a provider/socket closure
        // must not truncate PCM already received. Explicit Stop still flushes.
        update(state.copy(active = false, status = "Live остановлен", inputActive = false, error = message))
        remoteStop(server, session)
        RecordingService.command(app, RecordingService.ACTION_TRANSPORT_FINISH)
    }

    private fun mergeMessage(role: String, fragment: String, preferredIndex: Int): Int {
        val clean = fragment.trim()
        if (clean.isEmpty()) return preferredIndex
        val messages = state.messages.toMutableList()
        val index = preferredIndex.takeIf { it in messages.indices && messages[it].role == role }
        if (index == null) {
            messages.add(LiveChatMessage(role, clean.take(4000)))
            while (messages.size > 24) messages.removeAt(0)
            update(state.copy(messages = messages.toList()))
            return messages.lastIndex
        }
        messages[index] = messages[index].copy(text = mergeTranscript(messages[index].text, clean).take(4000))
        update(state.copy(messages = messages.toList()))
        return index
    }

    private fun mergeTranscript(current: String, fragment: String): String {
        if (current.isBlank()) return fragment
        if (fragment.startsWith(current)) return fragment
        if (current.endsWith(fragment)) return current
        var overlap = minOf(current.length, fragment.length)
        while (overlap >= 3 && current.takeLast(overlap) != fragment.take(overlap)) overlap--
        return if (overlap >= 3) current + fragment.drop(overlap) else "$current $fragment"
    }

    private fun addSystemMessage(text: String) {
        if (text.isBlank()) return
        val messages = (state.messages + LiveChatMessage(LiveRole.SYSTEM, text.take(500))).takeLast(24)
        update(state.copy(messages = messages))
    }

    private fun humanLiveError(code: String): String = when (code) {
        "RESOURCE_CAPACITY", "RESOURCE_TOKEN_BUDGET", "LIVE_BUSY", "http_429", "http_503" ->
            "Мира сейчас занята. Нажмите кнопку ещё раз через несколько секунд."
        "LIVE_SOCKET_ACK_TIMEOUT", "LIVE_SOCKET_HEARTBEAT_TIMEOUT", "LIVE_SOCKET_IO", "LIVE_CONNECTION_FAILED",
        "LIVE_AUDIO_BACKPRESSURE", "LIVE_AUDIO_STALE" ->
            "Связь с Мирой прервалась. Результат сохранён — включите Live снова."
        "LIVE_PLAYBACK_BACKPRESSURE", "LIVE_PLAYBACK_WRITE", "LIVE_PLAYBACK_UNAVAILABLE" ->
            "Не удалось полностью воспроизвести голос Миры."
        else -> "Live остановлен: ${code.take(80)}"
    }

    private fun remoteStop(server: String?, session: String?) {
        val base = config.backendUrl; val token = config.deviceToken
        if (!server.isNullOrBlank() && !session.isNullOrBlank() && !base.isNullOrBlank() && !token.isNullOrBlank()) {
            network.execute { runCatching { LiveApiClient(base, token).stop(server, session) } }
        }
    }
    private fun update(next: LiveUiState) { state = next; listeners.forEach { runCatching { it(next) } } }
    private fun safeMessage(exc: Exception): String = when (exc) {
        is ApiException -> humanLiveError(exc.code)
        is ApiProtocolException -> "Backend и приложение используют несовместимый Live-протокол."
        else -> humanLiveError("LIVE_CONNECTION_FAILED")
    }
    companion object { private const val MAX_PLAYBACK_BYTES = 4 * 1024 * 1024 }
}

internal class LiveSpeechBoundary {
    private var open = false
    fun beforeAudio(): Boolean { if (open) return false; open = true; return true }
    fun end(): Boolean { if (!open) return false; open = false; return true }
    fun reset() { open = false }
}
