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

data class LiveResearchSource(
    val type: String,
    val title: String,
    val url: String,
)

data class LiveResearchProgress(
    val active: Boolean,
    val stage: String,
    val query: String,
    val sourceCount: Int,
    val factCount: Int,
    val batchSourceCount: Int,
    val sources: List<LiveResearchSource>,
)

internal object LiveAudioTransportPolicy {
    const val TARGET_PCM_BYTES = LiveSocketTransport.BATCH_BYTES
    const val OUTBOUND_CAPACITY = LiveSocketTransport.ACK_WINDOW
    const val MAX_AUDIO_AGE_MS = LiveSocketTransport.MAX_AGE_MS
}

data class LiveUiState(
    val storyId: String? = null,
    val active: Boolean = false,
    val connecting: Boolean = false,
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
    val microphone: MicrophoneReading? = null,
    val researchProgress: LiveResearchProgress? = null,
)

/** Product UI/state only. Ordered PCM, WSS framing and ACKs belong to the shared SDK. */
class LiveSessionController(context: Context) {
    private val app = context.applicationContext
    private val config = AppGraph.config(app)
    private val store = AppGraph.store(app)
    private val feed = FeedProjectionStore(app)
    private val gson = Gson()
    private val generation = AtomicInteger(0)
    private val playbackGeneration = AtomicInteger(0)
    private val pendingPlayback = AtomicInteger(0)
    private val playbackDrain = PlaybackDrainTracker()
    private val playbackOutstanding = AtomicBoolean(false)
    private val duplexGate = LiveDuplexGate()
    private val inputFocusGate = LiveInputFocusGate()
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

    @Synchronized internal fun observeMicrophone(storyId: String, reading: MicrophoneReading) {
        if (!state.active || state.storyId != storyId || state.microphone == reading) return
        update(state.copy(microphone = reading))
    }

    fun snapshot(): LiveUiState = state

    @Synchronized
    fun restoreMessages(storyId: String) {
        if (state.active && state.storyId == storyId) return
        val restored = feed.liveMessages(storyId).map { LiveChatMessage(it.role, it.text) }.takeLast(100)
        if (state.storyId == storyId) {
            if (restored.isNotEmpty() && restored != state.messages) update(state.copy(messages = restored))
        } else {
            update(LiveUiState(storyId = storyId, status = "Live выключен", messages = restored))
        }
    }

    private fun persistMessages() {
        val story = state.storyId ?: return
        runCatching { feed.replaceLocalLiveMessages(story, state.messages) }
    }

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

    internal fun microphoneInputSuppression(): LiveInputSuppression {
        val now = SystemClock.elapsedRealtime()
        val hardwarePending = audioTrack?.let { track ->
            runCatching { playbackDrain.pending(track.playbackHeadPosition) > 0 }.getOrDefault(false)
        } ?: false
        val outputPending = pendingPlayback.get() > 0 || hardwarePending
        if (!outputPending && playbackOutstanding.compareAndSet(true, false)) {
            duplexGate.onPlaybackDrained(now)
            diagnostic("playback_drained", mapOf("hardware_queue_empty" to true, "received_pcm_bytes" to receivedPcm.get()))
        }
        val playbackSuppressed = state.active && duplexGate.shouldSuppress(now, if (outputPending) 1 else 0)
        if (playbackSuppressed && playbackSuppressionReported.compareAndSet(false, true)) {
            diagnostic(
                "input_suppressed_playback",
                mapOf("pending_playback_bytes" to pendingPlayback.get()),
            )
        } else if (!playbackSuppressed && playbackSuppressionReported.compareAndSet(true, false)) {
            diagnostic("input_resumed_after_playback")
        }
        return inputFocusGate.snapshot(playbackSuppressed)
    }

    fun shouldSuppressMicrophoneInput(): Boolean = microphoneInputSuppression().active

    private fun setResearchInputFocus(active: Boolean, stage: String): LiveInputSuppression {
        val wasActive = inputFocusGate.isResearchActive()
        val next = inputFocusGate.setResearchActive(active)
        if (wasActive == active) return next

        var closeSpeech = false
        synchronized(this) {
            if (active && inputOpen) {
                inputOpen = false
                closeSpeech = true
            }
        }
        if (closeSpeech) {
            socket?.endSpeech()
            diagnostic("speech_closed_for_research", mapOf("stage" to stage, "input_epoch" to next.epoch))
        }
        diagnostic(
            if (active) "research_input_suppressed" else "research_input_resumed",
            mapOf("stage" to stage, "input_epoch" to next.epoch),
        )
        return next
    }

    private fun researchStatus(progress: LiveResearchProgress?): String = when (progress?.stage) {
        "searching" -> "Ищу источники…"
        "extracting" -> "Извлекаю и сверяю факты…"
        else -> if (progress?.active == true) "Обрабатываю факты…" else "Факты обновлены"
    }

    fun diagnostic(event: String, fields: Map<String, Any?> = emptyMap()) {
        val server = serverStoryId ?: state.storyId?.let { store.story(it)?.serverStoryId } ?: return
        val session = sessionId
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
            runCatching {
                if (session != null) LiveApiClient(base, token).diagnostic(server, session, payload)
                else if (event in setOf("live_start_requested", "stop_requested", "live_start_cancelled"))
                    ApiClient(base, token).photoDiagnostic(server, gson.toJson(payload))
            }
                .onFailure { Log.w("StreetStoryLive", "diagnostic_send_failed event=$event type=${it.javaClass.simpleName}") }
        }
    }

    fun start(storyId: String, onReady: (Boolean, String?) -> Unit = { _, _ -> }) {
        if (state.active || state.connecting) { onReady(false, null); return }
        val server = store.story(storyId)?.serverStoryId
        val base = config.backendUrl
        val token = config.deviceToken
        if (server.isNullOrBlank() || base.isNullOrBlank() || token.isNullOrBlank()) {
            SyncScheduler.enqueue(app)
            update(LiveUiState(storyId = storyId, status = "Синхронизирую тему", error = "Live пока нельзя запустить"))
            onReady(false, "Тема ещё не синхронизирована с backend")
            return
        }
        val gen = synchronized(this) {
            if (state.active || state.connecting) { onReady(false, null); return }
            val epoch = generation.incrementAndGet()
            val restored = if (state.storyId == storyId && state.messages.isNotEmpty()) {
                state.messages
            } else {
                feed.liveMessages(storyId).map { LiveChatMessage(it.role, it.text) }.takeLast(100)
            }
            update(LiveUiState(storyId = storyId, connecting = true, status = "Подключаю Live…", messages = restored))
            epoch
        }
        playbackGeneration.incrementAndGet()
        stopPlayback()
        val notified = AtomicBoolean(false)
        fun ready(ok: Boolean, error: String?) { if (notified.compareAndSet(false, true)) onReady(ok, error) }
        diagnostic("live_start_requested", mapOf("generation" to gen))
        network.execute {
            val api = LiveApiClient(base, token)
            try {
                val attempt = "attempt_" + UUID.randomUUID().toString().replace("-", "")
                val admission = LiveStartAdmissionPolicy()
                var started: LiveStartWire? = null
                var retry = 0
                while (started == null) {
                    try {
                        started = api.start(server, if (retry == 0) attempt else attempt + "_retry_" + retry)
                    } catch (exc: ApiException) {
                        val decision = admission.next(exc.status, exc.code) ?: throw exc
                        retry = decision.attempt
                        waitStage = "resource"
                        if (waitStarted == 0L) waitStarted = SystemClock.elapsedRealtime()
                        updateForGeneration(
                            gen,
                            state.copy(
                                connecting = true,
                                status = "Ожидаю доступный лимит · " + decision.attempt,
                                error = null,
                            ),
                        )
                        diagnostic(
                            "live_start_admission_wait",
                            mapOf("attempt" to decision.attempt, "delay_ms" to decision.delayMs, "status" to exc.status, "code" to exc.code),
                        )
                        Thread.sleep(decision.delayMs)
                        if (generation.get() != gen) { ready(false, null); return@execute }
                    }
                }
                waitStarted = 0
                val startedSession = requireNotNull(started)
                if (generation.get() != gen) {
                    runCatching { api.stop(server, startedSession.sessionId) }
                    ready(false, null)
                    return@execute
                }
                synchronized(this@LiveSessionController) {
                    if (generation.get() != gen) {
                        remoteStop(server, startedSession.sessionId)
                        ready(false, null)
                        return@execute
                    }
                    serverStoryId = server
                    sessionId = startedSession.sessionId
                }
                if (startedSession.transportProtocol != LiveSocketTransport.PROTOCOL || startedSession.socketTicket.isBlank() || startedSession.socketUrl.isBlank()) {
                    throw ApiProtocolException("Backend не поддерживает согласованный WSS-протокол")
                }
                receivedPcm.set(0)
                outputAudioChunks.set(0)
                duplexGate.reset()
                inputFocusGate.reset()
                playbackSuppressionReported.set(false)
                userTranscriptIndex = -1
                assistantTranscriptIndex = -1

                val reconnectPolicy = LiveReconnectPolicy()
                val connectionGeneration = AtomicInteger(1)
                val reconnecting = AtomicBoolean(false)
                lateinit var connectTransport: (String, String, Long, Boolean) -> Unit
                connectTransport = connect@ { socketUrl, socketTicket, cursor, initial ->
                    if (generation.get() != gen) return@connect
                    lateinit var transport: LiveSocketTransport
                    transport = LiveSocketTransport(http, object : LiveSocketTransport.Listener {
                        override fun onEvent(event: JsonObject) {
                            if (generation.get() == gen && socket === transport) {
                                handleEvent(gen, gson.fromJson(event, LiveEventWire::class.java))
                            }
                        }

                        override fun onAudio(sequence: Long, pcm: ByteArray, sampleRate: Int) {
                            if (generation.get() == gen && socket === transport) playPcm(gen, pcm, sampleRate)
                        }

                        override fun onFailure(code: String) {
                            if (generation.get() != gen || socket !== transport) return
                            val resumeCursor = transport.cursor()
                            diagnostic(
                                "transport_failure",
                                mapOf(
                                    "code" to code.take(80),
                                    "cursor" to resumeCursor,
                                    "connection_generation" to connectionGeneration.get(),
                                ),
                            )
                            val decision = reconnectPolicy.next(code)
                            if (decision != null && reconnecting.compareAndSet(false, true)) {
                                inputOpen = false
                                updateForGeneration(gen,
                                    state.copy(
                                        connecting = true,
                                        status = "Восстанавливаю соединение с Мирой…",
                                        inputActive = false,
                                        error = null,
                                    )
                                )
                                diagnostic(
                                    "transport_reconnect_scheduled",
                                    mapOf("attempt" to decision.attempt, "delay_ms" to decision.delayMs),
                                )
                                network.execute {
                                    try {
                                        Thread.sleep(decision.delayMs)
                                        if (generation.get() != gen) return@execute
                                        val renewed = api.renewSocketTicket(server, startedSession.sessionId)
                                        if (
                                            renewed.transportProtocol != LiveSocketTransport.PROTOCOL ||
                                            renewed.socketTicket.isBlank() ||
                                            renewed.socketUrl.isBlank()
                                        ) {
                                            throw ApiProtocolException("Backend вернул несовместимый WSS ticket")
                                        }
                                        connectionGeneration.incrementAndGet()
                                        connectTransport(
                                            renewed.socketUrl,
                                            renewed.socketTicket,
                                            resumeCursor,
                                            false,
                                        )
                                    } catch (exc: Exception) {
                                        reconnecting.set(false)
                                        if (generation.get() == gen) {
                                            diagnostic(
                                                "transport_reconnect_failed",
                                                mapOf(
                                                    "attempt" to decision.attempt,
                                                    "type" to exc.javaClass.simpleName,
                                                ),
                                            )
                                            val message = humanLiveError("LIVE_CONNECTION_FAILED")
                                            fail(gen, message)
                                            ready(false, message)
                                        }
                                    }
                                }
                                return
                            }
                            val message = humanLiveError(code)
                            fail(gen, message)
                            ready(false, message)
                        }

                        override fun onDiagnostic(fields: Map<String, Any>) {
                            if (generation.get() == gen && socket === transport) {
                                Log.i(
                                    "StreetStoryLive",
                                    gson.toJson(
                                        fields + mapOf(
                                            "session_id" to startedSession.sessionId,
                                            "attempt_id" to startedSession.attemptId,
                                            "connection_generation" to connectionGeneration.get(),
                                        )
                                    ),
                                )
                                diagnostic("wss_transport", fields)
                            }
                        }
                    })
                    synchronized(this@LiveSessionController) {
                        if (generation.get() != gen) { transport.close(false); return@connect }
                        socket = transport
                    }
                    try {
                        transport.connect(
                            base,
                            socketUrl,
                            socketTicket,
                            startedSession.attemptId,
                            connectionGeneration.get(),
                            cursor,
                        ).get(12, TimeUnit.SECONDS)
                    } catch (exc: Exception) {
                        if (generation.get() != gen || reconnecting.get()) return@connect
                        throw exc
                    }
                    if (generation.get() != gen) {
                        transport.close(true)
                        return@connect
                    }
                    reconnecting.set(false)
                    synchronized(this@LiveSessionController) {
                    if (generation.get() != gen) { transport.close(true); return@connect }
                    update(
                        state.copy(
                            storyId = storyId,
                            active = true,
                            connecting = false,
                            status = "Слушаю",
                            inputActive = false,
                            transport = "wss",
                            error = null,
                        )
                    )
                    diagnostic(
                        if (initial) "live_ready" else "transport_reconnected",
                        mapOf(
                            "transport" to "wss",
                            "attempt_id" to startedSession.attemptId,
                            "connection_generation" to connectionGeneration.get(),
                            "cursor" to cursor,
                        ),
                    )
                    ready(true, null)
                    }
                }
                connectTransport(startedSession.socketUrl, startedSession.socketTicket, 0L, true)
            } catch (exc: Exception) {
                if (generation.get() == gen) fail(gen, "Live недоступен: ${safeMessage(exc)}")
                ready(false, safeMessage(exc))
            }
        }
    }

    fun submitPcm(samples: ShortArray) {
        if (!state.active || state.connecting) return
        val suppression = microphoneInputSuppression()
        if (suppression.active) {
            if (inputOpen) {
                inputOpen = false
                socket?.endSpeech()
                diagnostic(
                    if (suppression.reason == "research") "speech_closed_for_research" else "speech_closed_for_playback",
                    mapOf("input_epoch" to suppression.epoch),
                )
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
        userTranscriptIndex = mergeMessage(LiveRole.USER, value.take(4000), -1)
        update(state.copy(inputActive = false))
        waitStage = "provider"
        waitStarted = SystemClock.elapsedRealtime()
        update(state.copy(status = "Думаю", error = null))
        socket?.sendText(value.take(4000))
    }

    fun stopLocal(sendRemote: Boolean = true) {
        val stopped = synchronized(this) {
            diagnostic("live_client_summary", transportEvidence() + mapOf("completed_turns" to state.completedTurns, "pending_playback_bytes" to pendingPlayback.get()))
            diagnostic("stop_requested", mapOf("active" to state.active, "connecting" to state.connecting, "generation" to generation.get()))
            val prior = Triple(serverStoryId, sessionId, socket)
            generation.incrementAndGet()
            playbackGeneration.incrementAndGet()
            serverStoryId = null; sessionId = null; socket = null
            inputOpen = false; waitStarted = 0
            duplexGate.reset(); inputFocusGate.reset(); playbackSuppressionReported.set(false)
            userTranscriptIndex = -1; assistantTranscriptIndex = -1
            update(state.copy(
                active = false,
                connecting = false,
                status = "Микрофон выключен",
                inputActive = false,
                microphone = null,
                researchProgress = null,
                error = null,
            ))
            prior
        }
        // Never call transport callbacks while holding the controller state lock.
        stopped.third?.close(sendRemote)
        stopPlayback()
        if (sendRemote) remoteStop(stopped.first, stopped.second)
    }

    @Synchronized private fun updateForGeneration(epoch: Int, next: LiveUiState): Boolean {
        if (generation.get() != epoch) return false
        update(next)
        return true
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
                    updateForGeneration(gen, state.copy(status = "Мира отвечает", assistantText = text, error = null))
                }
            }
            "input_transcript" -> {
                waitStage = "provider"
                val text = event.text?.trim().orEmpty()
                if (text.isNotEmpty()) {
                    userTranscriptIndex = mergeMessage(LiveRole.USER, text, userTranscriptIndex)
                    updateForGeneration(gen, state.copy(status = "Думаю", inputActive = false, error = null))
                } else if (waitStarted > 0) {
                    updateForGeneration(gen, state.copy(status = "Думаю", inputActive = false))
                }
            }
            "input_timing" -> {
                waitStage = "provider"
                if (waitStarted > 0) updateForGeneration(gen, state.copy(status = "Думаю", inputActive = false))
            }
            "turn_complete" -> {
                waitStarted = 0
                userTranscriptIndex = -1
                assistantTranscriptIndex = -1
                val status = if (inputFocusGate.isResearchActive()) researchStatus(state.researchProgress) else "Слушаю"
                updateForGeneration(gen, state.copy(status = status, inputActive = false, completedTurns = state.completedTurns + 1, error = null))
                persistMessages()
                SyncScheduler.enqueue(app)
            }
            "tool_call" -> { waitStage = "tool"; if (waitStarted == 0L) waitStarted = SystemClock.elapsedRealtime(); updateForGeneration(gen, state.copy(status = "Выполняю действие…")) }
            "budget_wait" -> { waitStage = "resource"; waitStarted = SystemClock.elapsedRealtime(); updateForGeneration(gen, state.copy(status = "Ожидаю доступный лимит")) }
            "reconnecting" -> updateForGeneration(gen, state.copy(status = "Восстанавливаю соединение с моделью…"))
            "research_progress" -> {
                val payload = event.state?.takeIf { it.isJsonObject }?.asJsonObject
                val sourceRows = payload?.getAsJsonArray("sources")?.mapNotNull { element ->
                    val item = element.takeIf { it.isJsonObject }?.asJsonObject ?: return@mapNotNull null
                    val url = item.get("url")?.asString.orEmpty()
                    if (!url.startsWith("https://")) return@mapNotNull null
                    LiveResearchSource(
                        item.get("type")?.asString.orEmpty(),
                        item.get("title")?.asString.orEmpty().ifBlank { url },
                        url,
                    )
                } ?: emptyList()
                val progress = LiveResearchProgress(
                    active = payload?.get("active")?.asBoolean ?: (event.status == "working"),
                    stage = payload?.get("stage")?.asString ?: event.stage.orEmpty(),
                    query = payload?.get("query")?.asString.orEmpty(),
                    sourceCount = payload?.get("source_count")?.asInt ?: 0,
                    factCount = payload?.get("fact_count")?.asInt ?: 0,
                    batchSourceCount = payload?.get("batch_source_count")?.asInt ?: 0,
                    sources = sourceRows,
                )
                setResearchInputFocus(progress.active, progress.stage)
                updateForGeneration(
                    gen,
                    state.copy(
                        status = researchStatus(progress),
                        inputActive = if (progress.active) false else state.inputActive,
                        microphone = if (progress.active) MicrophoneReading(0, researchSuppressed = true) else null,
                        researchProgress = progress,
                        error = null,
                    ),
                )
            }
            "literal_mode" -> updateForGeneration(
                gen,
                state.copy(
                    literalMode = event.active == true,
                    status = if (event.active == true) {
                        "Дословная диктовка"
                    } else if (inputFocusGate.isResearchActive()) {
                        researchStatus(state.researchProgress)
                    } else {
                        "Слушаю"
                    },
                ),
            )
            "product_state" -> {
                SyncScheduler.enqueue(app)
                val product = event.state?.takeIf { it.isJsonObject }?.asJsonObject
                val last = product?.get("last_change")?.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.isNotBlank() }
                updateForGeneration(gen, state.copy(lastChange = last ?: state.lastChange, error = null))
            }
            "tool_result" -> {
                SyncScheduler.enqueue(app)
                waitStage = "provider"
                if (event.status == "error" && inputFocusGate.isResearchActive()) {
                    setResearchInputFocus(false, "tool_error")
                }
                updateForGeneration(
                    gen,
                    state.copy(
                        status = if (event.status == "error") "Действие не выполнено" else "Обновляю результат…",
                        inputActive = false,
                        microphone = if (inputFocusGate.isResearchActive()) state.microphone else null,
                        researchProgress = if (event.status == "error") {
                            state.researchProgress?.copy(active = false, stage = "error")
                        } else {
                            state.researchProgress
                        },
                        lastChange = if (event.status == "error") event.code ?: "Ошибка действия" else state.lastChange,
                    ),
                )
            }
            "publication_confirmation" -> {
                val id = event.confirmationId.orEmpty()
                if (id.isNotBlank()) updateForGeneration(gen, state.copy(
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
                if (inputFocusGate.isResearchActive()) {
                    updateForGeneration(gen, state.copy(status = researchStatus(state.researchProgress), inputActive = false))
                } else {
                    updateForGeneration(gen, state.copy(status = "Слышу вас", inputActive = true))
                }
            }
            "error" -> {
                diagnostic("provider_error", mapOf("code" to (event.code ?: "LIVE_PROVIDER_ERROR")))
                fail(gen, humanLiveError(event.code ?: "LIVE_PROVIDER_ERROR"))
            }
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
        playbackOutstanding.set(true)
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
                    playbackOutstanding.set(true)
                    playbackDrain.wrote(count.toLong() / 2)
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
                pendingPlayback.addAndGet(-bytes.size)
                // AudioTrack.write accepts queued PCM; the capture clock waits for playbackHeadPosition.
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
                .also { playbackDrain.reset(); it.play(); audioTrack = it }
        }.getOrNull()
    }

    @Synchronized private fun stopPlayback() {
        val track = audioTrack ?: return
        audioTrack = null
        playbackDrain.reset()
        playbackOutstanding.set(false)
        runCatching { track.pause() }; runCatching { track.flush() }; runCatching { track.stop() }; runCatching { track.release() }
    }

    private fun fail(gen: Int, message: String) {
        val stopped = synchronized(this) {
            if (!generation.compareAndSet(gen, gen + 1)) return
            diagnostic("live_failed", mapOf("message" to message.take(240), "pending_playback_bytes" to pendingPlayback.get()))
            val prior = Triple(serverStoryId, sessionId, socket)
            serverStoryId = null; sessionId = null; socket = null
            waitStarted = 0; inputOpen = false
            duplexGate.reset(); inputFocusGate.reset(); playbackSuppressionReported.set(false)
            userTranscriptIndex = -1; assistantTranscriptIndex = -1
            // Keep already received PCM; only explicit user Stop flushes playback.
            update(state.copy(
                active = false,
                connecting = false,
                status = "Live остановлен",
                inputActive = false,
                microphone = null,
                researchProgress = null,
                error = message,
            ))
            persistMessages()
            prior
        }
        stopped.third?.close(false)
        remoteStop(stopped.first, stopped.second)
        RecordingService.command(app, RecordingService.ACTION_TRANSPORT_FINISH)
    }

    private fun mergeMessage(role: String, fragment: String, preferredIndex: Int): Int {
        val clean = fragment.trim()
        if (clean.isEmpty()) return preferredIndex
        val messages = state.messages.toMutableList()
        val index = preferredIndex.takeIf { it in messages.indices && messages[it].role == role }
        if (index == null) {
            messages.add(LiveChatMessage(role, clean.take(4000)))
            while (messages.size > 100) messages.removeAt(0)
            update(state.copy(messages = messages.toList()))
            persistMessages()
            return messages.lastIndex
        }
        messages[index] = messages[index].copy(text = mergeTranscript(messages[index].text, clean).take(4000))
        update(state.copy(messages = messages.toList()))
        persistMessages()
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
        val messages = (state.messages + LiveChatMessage(LiveRole.SYSTEM, text.take(500))).takeLast(100)
        update(state.copy(messages = messages))
    }

    private fun humanLiveError(code: String): String = when (code.uppercase(java.util.Locale.ROOT)) {
        "RESOURCE_CAPACITY", "LIVE_BUSY", "HTTP_429", "HTTP_503" ->
            "Мира сейчас занята. Нажмите кнопку ещё раз через несколько секунд."
        "RESOURCE_TOKEN_BUDGET" ->
            "Голосовой лимит временно исчерпан. Подождите около минуты и включите Live снова."
        "LIVE_SOCKET_ACK_TIMEOUT", "LIVE_SOCKET_HEARTBEAT_TIMEOUT", "LIVE_SOCKET_IO", "LIVE_CONNECTION_FAILED",
        "LIVE_AUDIO_BACKPRESSURE", "LIVE_AUDIO_STALE" ->
            "Связь с Мирой прервалась. Результат сохранён — включите Live снова."
        "LIVE_PLAYBACK_BACKPRESSURE", "LIVE_PLAYBACK_WRITE", "LIVE_PLAYBACK_UNAVAILABLE" ->
            "Не удалось полностью воспроизвести голос Миры."
        "LIVE_PROVIDER_ERROR" ->
            "Модель завершила голосовую сессию. Тема сохранена — включите Live снова."
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
