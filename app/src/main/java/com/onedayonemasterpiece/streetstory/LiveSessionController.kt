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
    val assistantText: String? = null,
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
    private val receivedPcm = AtomicLong(0)
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
        "event_cursor" to (socket?.cursor() ?: 0),
        "http_audio_fallback" to false,
        "event_polling" to false,
    )

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
                val started = api.start(server, attempt)
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
                val transport = LiveSocketTransport(http, object : LiveSocketTransport.Listener {
                    override fun onEvent(event: JsonObject) {
                        if (generation.get() == gen) handleEvent(gen, gson.fromJson(event, LiveEventWire::class.java))
                    }
                    override fun onAudio(sequence: Long, pcm: ByteArray, sampleRate: Int) {
                        if (generation.get() == gen) playPcm(gen, pcm, sampleRate)
                    }
                    override fun onFailure(code: String) {
                        fail(gen, "Соединение Live прервано: $code")
                        ready(false, code)
                    }
                    override fun onDiagnostic(fields: Map<String, Any>) {
                        if (generation.get() == gen) Log.i("StreetStoryLive", gson.toJson(fields + mapOf("session_id" to started.sessionId, "attempt_id" to attempt)))
                    }
                })
                socket = transport
                transport.connect(base, started.socketUrl, started.socketTicket, started.attemptId, 1, 0).get(12, TimeUnit.SECONDS)
                if (generation.get() != gen) { transport.close(true); return@execute }
                update(state.copy(storyId = storyId, active = true, status = "Слушаю", transport = "wss", error = null))
                ready(true, null)
            } catch (exc: Exception) {
                if (generation.get() == gen) fail(gen, "Live недоступен: ${safeMessage(exc)}")
                ready(false, safeMessage(exc))
            }
        }
    }

    fun submitPcm(samples: ShortArray) {
        if (!state.active) return
        if (!inputOpen) { inputOpen = true; waitStarted = 0; update(state.copy(status = "Слышу вас", error = null)) }
        socket?.submitPcm(samples)
    }

    fun endSpeech() {
        if (!state.active || !inputOpen) return
        inputOpen = false
        socket?.endSpeech()
        waitStage = "transport"
        waitStarted = SystemClock.elapsedRealtime()
        update(state.copy(status = "Передаю реплику", error = null))
    }

    fun sendText(text: String) {
        val value = text.trim()
        if (!state.active || value.isEmpty()) return
        inputOpen = false
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
        val old = socket; socket = null; old?.close(sendRemote)
        playbackGeneration.incrementAndGet()
        stopPlayback()
        update(state.copy(active = false, status = "Микрофон выключен", error = null))
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
                if (text.isNotEmpty()) update(state.copy(status = "Отвечаю", assistantText = text, error = null))
            }
            "turn_complete" -> { waitStarted = 0; update(state.copy(status = "Слушаю", completedTurns = state.completedTurns + 1, error = null)) }
            "input_transcript", "input_timing" -> { waitStage = "provider"; if (waitStarted > 0) update(state.copy(status = "Думаю")) }
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
            "interrupted" -> { playbackGeneration.incrementAndGet(); stopPlayback() }
            "error" -> fail(gen, event.code ?: "LIVE_PROVIDER_ERROR")
            "closed" -> fail(gen, "Live-сессия завершилась. Результат сохранён; можно продолжить новой сессией.")
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
        playback.execute {
            try {
                if (playbackGeneration.get() != playbackEpoch) return@execute
                val track = ensureAudioTrack(rate) ?: throw IllegalStateException("LIVE_PLAYBACK_UNAVAILABLE")
                var offset = 0
                while (offset < bytes.size && playbackGeneration.get() == playbackEpoch) {
                    val count = track.write(bytes, offset, bytes.size - offset, AudioTrack.WRITE_BLOCKING)
                    if (count <= 0) throw IllegalStateException("LIVE_PLAYBACK_WRITE")
                    offset += count
                }
            } catch (_: Exception) {
                if (playbackGeneration.get() == playbackEpoch) {
                    Log.w("StreetStoryLive", "LIVE_PLAYBACK_WRITE")
                    if (generation.get() == gen) fail(gen, "Не удалось воспроизвести ответ")
                }
            } finally { pendingPlayback.addAndGet(-bytes.size) }
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
        serverStoryId = null; sessionId = null; waitStarted = 0; inputOpen = false
        val old = socket; socket = null; old?.close(false)
        // Do not increment playbackGeneration here: a provider/socket closure
        // must not truncate PCM already received. Explicit Stop still flushes.
        update(state.copy(active = false, status = "Live остановлен", error = message))
        remoteStop(server, session)
        RecordingService.command(app, RecordingService.ACTION_TRANSPORT_FINISH)
    }

    private fun remoteStop(server: String?, session: String?) {
        val base = config.backendUrl; val token = config.deviceToken
        if (!server.isNullOrBlank() && !session.isNullOrBlank() && !base.isNullOrBlank() && !token.isNullOrBlank()) {
            network.execute { runCatching { LiveApiClient(base, token).stop(server, session) } }
        }
    }
    private fun update(next: LiveUiState) { state = next; listeners.forEach { runCatching { it(next) } } }
    private fun safeMessage(exc: Exception): String = when (exc) {
        is ApiException -> exc.code
        is ApiProtocolException -> "LIVE_PROTOCOL_MISMATCH"
        else -> "LIVE_CONNECTION_FAILED"
    }
    companion object { private const val MAX_PLAYBACK_BYTES = 4 * 1024 * 1024 }
}

internal class LiveSpeechBoundary {
    private var open = false
    fun beforeAudio(): Boolean { if (open) return false; open = true; return true }
    fun end(): Boolean { if (!open) return false; open = false; return true }
    fun reset() { open = false }
}
