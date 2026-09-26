package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

data class LiveConfirmation(
    val confirmationId: String,
    val text: String?,
    val imageUrl: String?,
    val destinations: List<String>,
    val scheduledFor: String?,
    val timezone: String?,
)

data class LiveUiState(
    val storyId: String? = null,
    val active: Boolean = false,
    val status: String = "Микрофон выключен",
    val literalMode: Boolean = false,
    val assistantText: String? = null,
    val lastChange: String? = null,
    val confirmation: LiveConfirmation? = null,
    val error: String? = null,
)

class LiveSessionController(context: Context) {
    private val app = context.applicationContext
    private val config = AppGraph.config(app)
    private val store = AppGraph.store(app)
    private val generation = AtomicInteger(0)
    private val listeners = CopyOnWriteArrayList<(LiveUiState) -> Unit>()
    private val network = Executors.newCachedThreadPool()
    private val sender = Executors.newSingleThreadExecutor()
    private val playback = Executors.newSingleThreadExecutor()
    private val outbound = ArrayBlockingQueue<Outbound>(6)
    private val batchLock = Any()
    private var batch = ByteArrayOutputStream(TARGET_PCM_BYTES + 1024)
    @Volatile private var state = LiveUiState()
    @Volatile private var serverStoryId: String? = null
    @Volatile private var sessionId: String? = null
    @Volatile private var audioTrack: AudioTrack? = null

    fun snapshot(): LiveUiState = state
    fun addListener(listener: (LiveUiState) -> Unit) { listeners.add(listener); listener(state) }
    fun removeListener(listener: (LiveUiState) -> Unit) { listeners.remove(listener) }
    fun isActiveFor(storyId: String): Boolean = state.active && state.storyId == storyId && !sessionId.isNullOrBlank()

    fun start(storyId: String, onReady: (Boolean, String?) -> Unit = { _, _ -> }) {
        val local = store.story(storyId)
        val server = local?.serverStoryId
        val base = config.backendUrl
        val token = config.deviceToken
        if (local == null || server.isNullOrBlank() || base.isNullOrBlank() || token.isNullOrBlank()) {
            SyncScheduler.enqueue(app)
            update(LiveUiState(storyId = storyId, status = "Синхронизирую тему", error = "Live пока нельзя запустить"))
            onReady(false, "Тема ещё не синхронизирована с backend")
            return
        }
        stopLocal(sendRemote = true)
        val gen = generation.incrementAndGet()
        update(LiveUiState(storyId = storyId, status = "Подключаю Live…"))
        network.execute {
            try {
                val api = LiveApiClient(base, token)
                val started = api.start(server)
                if (generation.get() != gen) {
                    runCatching { api.stop(server, started.sessionId) }
                    return@execute
                }
                serverStoryId = server
                sessionId = started.sessionId
                outbound.clear()
                synchronized(batchLock) { batch.reset() }
                update(LiveUiState(storyId = storyId, active = true, status = "Слушаю"))
                startSender(gen, api, server, started.sessionId)
                startPoller(gen, api, server, started.sessionId)
                onReady(true, null)
            } catch (exc: Exception) {
                if (generation.get() == gen) {
                    fail(gen, "Live недоступен: ${safeMessage(exc)}")
                    onReady(false, safeMessage(exc))
                }
            }
        }
    }

    fun submitPcm(samples: ShortArray) {
        if (!state.active) return
        val gen = generation.get()
        synchronized(batchLock) {
            if (generation.get() != gen || !state.active) return
            for (sample in samples) {
                val v = sample.toInt()
                batch.write(v and 0xff)
                batch.write((v ushr 8) and 0xff)
            }
            if (batch.size() >= TARGET_PCM_BYTES) flushBatchLocked(gen)
        }
    }

    fun endSpeech() {
        if (!state.active) return
        val gen = generation.get()
        synchronized(batchLock) { flushBatchLocked(gen) }
        if (!outbound.offer(Outbound(null, true, System.currentTimeMillis(), gen))) {
            fail(gen, "Live audio queue переполнена")
        }
    }

    fun sendText(text: String) {
        val server = serverStoryId
        val session = sessionId
        val base = config.backendUrl
        val token = config.deviceToken
        val gen = generation.get()
        val value = text.trim()
        if (!state.active || server.isNullOrBlank() || session.isNullOrBlank() || base.isNullOrBlank() || token.isNullOrBlank() || value.isBlank()) return
        update(state.copy(status = "Жду ответа", error = null))
        network.execute {
            try {
                LiveApiClient(base, token).inputText(server, session, value.take(4000))
            } catch (exc: Exception) {
                if (generation.get() == gen) fail(gen, "Live command: ${safeMessage(exc)}")
            }
        }
    }

    fun stopLocal(sendRemote: Boolean = true) {
        val oldServer = serverStoryId
        val oldSession = sessionId
        generation.incrementAndGet()
        serverStoryId = null
        sessionId = null
        outbound.clear()
        synchronized(batchLock) { batch.reset() }
        stopPlayback()
        val oldStory = state.storyId
        update(LiveUiState(storyId = oldStory, status = "Микрофон выключен"))
        if (sendRemote && !oldServer.isNullOrBlank() && !oldSession.isNullOrBlank()) {
            val base = config.backendUrl
            val token = config.deviceToken
            if (!base.isNullOrBlank() && !token.isNullOrBlank()) {
                network.execute { runCatching { LiveApiClient(base, token).stop(oldServer, oldSession) } }
            }
        }
    }

    private fun flushBatchLocked(gen: Int) {
        if (batch.size() == 0) return
        val bytes = batch.toByteArray()
        batch.reset()
        if (!outbound.offer(Outbound(bytes, false, System.currentTimeMillis(), gen))) {
            fail(gen, "Live audio queue переполнена")
        }
    }

    private fun startSender(gen: Int, api: LiveApiClient, server: String, session: String) {
        sender.execute {
            while (generation.get() == gen && state.active) {
                val item = outbound.poll(250, TimeUnit.MILLISECONDS) ?: continue
                if (item.generation != gen || generation.get() != gen) continue
                if (System.currentTimeMillis() - item.queuedAtMs > MAX_AUDIO_AGE_MS) {
                    fail(gen, "Live не успевает принимать звук")
                    return@execute
                }
                try {
                    if (item.end) api.endAudio(server, session)
                    else api.inputAudio(server, session, Base64.encodeToString(requireNotNull(item.pcm), Base64.NO_WRAP))
                } catch (exc: Exception) {
                    fail(gen, "Ошибка передачи Live: ${safeMessage(exc)}")
                    return@execute
                }
            }
        }
    }

    private fun startPoller(gen: Int, api: LiveApiClient, server: String, session: String) {
        network.execute {
            var cursor = 0
            try {
                while (generation.get() == gen && state.active) {
                    val page = api.events(server, session, cursor)
                    if (generation.get() != gen) return@execute
                    if (page.gap) {
                        fail(gen, "Live потерял часть событий")
                        return@execute
                    }
                    page.events.forEach { handleEvent(gen, it) }
                    cursor = page.cursor
                    if (page.closed) {
                        fail(gen, "Live-сессия завершилась")
                        return@execute
                    }
                    if (!page.hasMore) Thread.sleep(POLL_MS)
                }
            } catch (exc: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (exc: Exception) {
                if (generation.get() == gen) fail(gen, "Live connection: ${safeMessage(exc)}")
            }
        }
    }

    private fun handleEvent(gen: Int, event: LiveEventWire) {
        if (generation.get() != gen) return
        when (event.type) {
            "audio" -> event.data?.let { playAudio(gen, it, event.mimeType) }
            "output_transcript" -> {
                val text = event.text?.trim().orEmpty()
                if (text.isNotEmpty()) update(state.copy(status = "Слушаю", assistantText = text, error = null))            }
            "input_transcript" -> update(state.copy(status = "Жду ответа", error = null))
            "interaction_status" -> {
                val label = when (event.status?.uppercase()) {
                    "IDLE" -> "Слушаю"
                    else -> "Жду ответа"
                }
                update(state.copy(status = label, error = null))
            }
            "reconnecting" -> update(state.copy(status = "Восстанавливаю Live…", error = null))
            "literal_mode" -> update(state.copy(literalMode = event.active == true, status = if (event.active == true) "Дословная диктовка" else "Слушаю"))
            "product_state" -> {
                SyncScheduler.enqueue(app)
                val last = event.state?.get("last_change")?.takeIf { !it.isJsonNull }?.asString?.takeIf { it.isNotBlank() }
                update(state.copy(status = "Обновляю результат…", lastChange = last ?: state.lastChange, error = null))
            }
            "tool_result" -> {
                SyncScheduler.enqueue(app)
                update(state.copy(status = "Обновляю результат…", error = null))
            }
            "publication_confirmation" -> {
                val id = event.confirmationId.orEmpty()
                if (id.isNotBlank()) {
                    update(
                        state.copy(
                            confirmation = LiveConfirmation(
                                id,
                                event.text,
                                event.imageUrl,
                                event.destinations.toList(),
                                event.scheduledFor,
                                event.timezone,
                            ),
                            status = "Проверь публикацию",
                            error = null,
                        )
                    )
                }
            }
            "error" -> fail(gen, event.message ?: event.code ?: "Live provider error")
            "closed" -> fail(gen, "Live-сессия завершилась")
        }
    }

    private fun playAudio(gen: Int, encoded: String, mime: String?) {
        val bytes = runCatching { Base64.decode(encoded, Base64.DEFAULT) }.getOrNull() ?: return
        val rate = Regex("rate=(\\d+)").find(mime.orEmpty())?.groupValues?.getOrNull(1)?.toIntOrNull() ?: 24_000
        playback.execute {
            if (generation.get() != gen) return@execute
            val track = ensureAudioTrack(rate) ?: return@execute
            runCatching { track.write(bytes, 0, bytes.size, AudioTrack.WRITE_BLOCKING) }
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
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(maxOf(min * 2, rate))
                .build()
                .also { it.play(); audioTrack = it }
        }.getOrNull()
    }

    @Synchronized private fun stopPlayback() {
        val track = audioTrack ?: return
        audioTrack = null
        runCatching { track.pause() }
        runCatching { track.flush() }
        runCatching { track.stop() }
        runCatching { track.release() }
    }

    private fun fail(gen: Int, message: String) {
        if (generation.get() != gen) return
        val story = state.storyId
        val server = serverStoryId
        val session = sessionId
        generation.incrementAndGet()
        serverStoryId = null
        sessionId = null
        outbound.clear()
        synchronized(batchLock) { batch.reset() }
        stopPlayback()
        update(LiveUiState(storyId = story, status = "Live недоступен", error = message))
        if (!server.isNullOrBlank() && !session.isNullOrBlank()) {
            val base = config.backendUrl
            val token = config.deviceToken
            if (!base.isNullOrBlank() && !token.isNullOrBlank()) {
                network.execute { runCatching { LiveApiClient(base, token).stop(server, session) } }
            }
        }
        RecordingService.command(app, RecordingService.ACTION_FINISH)
    }

    private fun update(next: LiveUiState) {
        state = next
        listeners.forEach { listener -> runCatching { listener(next) } }
    }

    private fun safeMessage(exc: Exception): String =
        (exc.message ?: exc::class.java.simpleName).replace(Regex("AIza[A-Za-z0-9_-]{20,}"), "[redacted]").take(240)

    private data class Outbound(val pcm: ByteArray?, val end: Boolean, val queuedAtMs: Long, val generation: Int)

    companion object {
        private const val TARGET_PCM_BYTES = 8_192
        private const val MAX_AUDIO_AGE_MS = 2_500L
        private const val POLL_MS = 160L
    }
}