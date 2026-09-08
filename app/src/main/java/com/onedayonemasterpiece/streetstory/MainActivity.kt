package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.text.Editable
import android.text.InputType
import android.text.TextUtils
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.google.gson.Gson
import com.google.gson.JsonParser
import java.io.File
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

class MainActivity : Activity() {
    private val store by lazy { AppGraph.store(this) }
    private val config by lazy { AppGraph.config(this) }
    private val runtime by lazy { RecordingRuntime(this) }
    private val feedProjection by lazy { FeedProjectionStore(this) }
    private val uiPrefs by lazy { getSharedPreferences("street_story_ui", MODE_PRIVATE) }
    private val gson = Gson()
    private val handler = Handler(Looper.getMainLooper())
    private val expandedFacts = linkedSetOf<String>()
    private val factDecisionTouched = linkedSetOf<String>()
    private val expandedDrafts = linkedSetOf<String>()
    private var activeStoryId: String? = null
    private var pendingRecordStory: String? = null
    private var pendingRecordKind: String? = null
    private var receiverRegistered = false
    private var feedScrollY = 0
    private var scrollView: ScrollView? = null
    private var pulsePhase = false

    private val changedReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            intent?.getStringExtra(RecordingService.EXTRA_MESSAGE)?.takeIf { it.isNotBlank() }?.let {
                Toast.makeText(this@MainActivity, it, Toast.LENGTH_SHORT).show()
            }
            feedScrollY = scrollView?.scrollY ?: feedScrollY
            render()
        }
    }

    private val liveTick = object : Runnable {
        override fun run() {
            if (store.activeVoiceSession() != null) {
                pulsePhase = !pulsePhase
                feedScrollY = scrollView?.scrollY ?: feedScrollY
                render()
            }
            handler.postDelayed(this, 700)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        activeStoryId = savedInstanceState?.getString("active_story_id") ?: uiPrefs.getString("active_story_id", null)
        feedScrollY = savedInstanceState?.getInt("feed_scroll_y") ?: 0
        savedInstanceState?.getStringArrayList("expanded_facts")?.let(expandedFacts::addAll)
        savedInstanceState?.getStringArrayList("fact_decision_touched")?.let(factDecisionTouched::addAll)
        savedInstanceState?.getStringArrayList("expanded_drafts")?.let(expandedDrafts::addAll)
        window.statusBarColor = SAGE
        window.navigationBarColor = SAGE
        if (Build.VERSION.SDK_INT >= 23) window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        if (Build.VERSION.SDK_INT >= 26) {
            window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        }
        render()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter().apply {
            addAction(RecordingService.ACTION_STATE_CHANGED)
            addAction(SyncWorker.ACTION_STATE_CHANGED)
        }
        ContextCompat.registerReceiver(this, changedReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        receiverRegistered = true
        handler.post(liveTick)
        SyncScheduler.enqueue(this)
    }

    override fun onStop() {
        feedScrollY = scrollView?.scrollY ?: feedScrollY
        handler.removeCallbacks(liveTick)
        if (receiverRegistered) {
            unregisterReceiver(changedReceiver)
            receiverRegistered = false
        }
        super.onStop()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("active_story_id", activeStoryId)
        outState.putInt("feed_scroll_y", scrollView?.scrollY ?: feedScrollY)
        outState.putStringArrayList("expanded_facts", ArrayList(expandedFacts))
        outState.putStringArrayList("fact_decision_touched", ArrayList(factDecisionTouched))
        outState.putStringArrayList("expanded_drafts", ArrayList(expandedDrafts))
        super.onSaveInstanceState(outState)
    }

    private fun setActiveStory(id: String?) {
        activeStoryId = id?.takeIf { store.story(it) != null }
        uiPrefs.edit().apply {
            if (activeStoryId == null) remove("active_story_id") else putString("active_story_id", activeStoryId)
        }.apply()
    }

    private fun render(scrollToBottom: Boolean = false) {
        if (activeStoryId != null && store.story(requireNotNull(activeStoryId)) == null) setActiveStory(null)
        val preservedY = scrollView?.scrollY ?: feedScrollY
        val root = FrameLayout(this).apply { setBackgroundColor(SAGE) }
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            setBackgroundColor(SAGE)
            contentDescription = "story-feed"
        }
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(14), dp(12), dp(188))
        }
        scroll.addView(page)
        root.addView(scroll, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        renderHeader(page)
        val visible = FeedModel.latest(store.stories()).reversed()
        if (visible.isEmpty()) {
            page.addView(surface(GRAPHITE, 22).apply {
                setPadding(dp(18), dp(18), dp(18), dp(18))
                addView(micro("ПЕРВАЯ ИСТОРИЯ", ORANGE))
                addView(label("Выбери фотографию — она сразу сохранится локально.", 18, PAPER, body).apply { setPadding(0, dp(8), 0, 0) })
            }, margins(0, 8, 0, 8))
        } else {
            visible.forEach { page.addView(storyThread(it), margins(0, 0, 0, 12)) }
        }
        root.addView(recorderDock(), FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM).apply {
            setMargins(dp(10), dp(8), dp(10), dp(10))
        })
        setContentView(root)
        scrollView = scroll
        scroll.post {
            if (scrollToBottom) scroll.fullScroll(View.FOCUS_DOWN) else scroll.scrollTo(0, preservedY.coerceAtLeast(0))
            feedScrollY = scroll.scrollY
        }
    }

    private fun renderHeader(page: LinearLayout) {
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val title = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        title.addView(micro("STREET STORY", ORANGE))
        title.addView(label("Городские истории", 25, INK, display).apply { setPadding(0, dp(3), 0, 0) })
        row.addView(title, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(compactAction("Настройки", PAPER, INK, ::showSettings))
        page.addView(row, margins(2, 0, 2, 12))
        if (!config.configured) {
            page.addView(messageSurface("Backend не настроен: фото и голос остаются локально. Research, VibePublish и publication sync начнутся после HTTPS-настройки."), margins(0, 0, 0, 10))
        }
    }

    private fun storyThread(story: StorySnapshot): View {
        val isActive = activeStoryId == story.clientStoryId || store.activeVoiceSession()?.storyId == story.clientStoryId
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(PAPER, 24, if (isActive) ORANGE else 0, if (isActive) 2 else 0)
            setPadding(dp(12), dp(12), dp(12), dp(14))
            contentDescription = "story-thread-${story.clientStoryId}"
        }
        val head = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val meta = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        meta.addView(micro(dateText(story.createdAt).uppercase(Locale.getDefault()), MUTED))
        meta.addView(label(story.placeName ?: "Новая история", 19, INK, display).apply { maxLines = 2; setPadding(0, dp(4), 0, 0) })
        head.addView(meta, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        head.addView(compactAction(if (isActive) "MIC · ACTIVE" else "Для MIC", if (isActive) ORANGE else SAGE, if (isActive) WHITE else INK) {
            setActiveStory(story.clientStoryId)
            render()
        })
        card.addView(head)
        card.addView(label(statusText(story.stage), 13, if (story.stage == StoryStage.NEEDS_REVIEW || story.stage == StoryStage.VISUAL_BLOCKED) ORANGE else MUTED, body).apply {
            setPadding(0, dp(6), 0, dp(9))
        })
        card.addView(imageFrame(story.photoPath, 190), margins(0, 0, 0, 10))
        renderVoiceMessages(card, story)
        renderResearch(card, story)
        renderFacts(card, story)
        renderVisual(card, story)
        renderDraft(card, story)
        renderDestinations(card, story)
        story.lastError?.takeIf { it.isNotBlank() }?.let { card.addView(messageSurface(it), margins(0, 8, 0, 0)) }
        renderInlineActions(card, story)
        return card
    }

    private fun renderVoiceMessages(card: LinearLayout, story: StorySnapshot) {
        val messages = feedProjection.voiceMessages(story.clientStoryId)
        messages.forEach { message ->
            val text = message.displayText?.trim().takeUnless { it.isNullOrBlank() } ?: "Голос обрабатывается…"
            val bubble = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = rounded(GRAPHITE, 18)
                setPadding(dp(13), dp(11), dp(13), dp(11))
                contentDescription = "voice-message-${message.sessionId}"
            }
            bubble.addView(micro(if (message.kind == RecordingKind.REFINEMENT) "УТОЧНЕНИЕ" else "МОЙ ГОЛОС", ORANGE))
            bubble.addView(label(text, 16, PAPER, body).apply { setPadding(0, dp(6), 0, 0); setLineSpacing(dp(1).toFloat(), 1.05f) })
            card.addView(bubble, margins(0, 0, 24, 8))
        }
        val latest = store.latestVoiceSession(story.clientStoryId)
        if (latest != null && messages.none { it.sessionId == latest.sessionId } && latest.captureState != CaptureState.DISCARDED) {
            val active = store.activeVoiceSession()?.sessionId == latest.sessionId
            val value = if (active) {
                val live = runtime.snapshotFor(latest.sessionId)
                "${captureStatus(live?.captureActivity ?: latest.captureActivity)} · ${formatDuration(live?.durationMs ?: latest.durationMs)}"
            } else {
                "Голос сохранён · ждёт синхронизации/обработки"
            }
            card.addView(label(value, 14, MUTED, body).apply {
                setPadding(dp(12), dp(8), dp(12), dp(8)); background = rounded(SAGE, 16)
            }, margins(0, 0, 30, 8))
        }
    }

    private fun renderResearch(card: LinearLayout, story: StorySnapshot) {
        if (story.stage in setOf(StoryStage.QUEUED, StoryStage.RESEARCHING)) {
            card.addView(inlineStatus("Research · OSM + Wikipedia + grounded Gemini", "Обновится здесь автоматически"), margins(0, 2, 0, 8))
        }
        story.summary?.takeIf { it.isNotBlank() }?.let {
            card.addView(label(it, 15, GRAPHITE_SOFT, body).apply { setLineSpacing(dp(1).toFloat(), 1.05f) }, margins(2, 2, 2, 8))
        }
    }

    private fun renderFacts(card: LinearLayout, story: StorySnapshot) {
        val facts = store.facts(story.clientStoryId)
        if (facts.isEmpty()) return
        val autoExpand = story.stage == StoryStage.REVIEW && activeStoryId == story.clientStoryId && story.clientStoryId !in factDecisionTouched
        val expanded = story.clientStoryId in expandedFacts || autoExpand
        val selected = facts.count { it.selected && it.evidenceSupported }
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(SAGE, 18)
            setPadding(dp(12), dp(11), dp(12), dp(10))
            contentDescription = "facts-${if (expanded) "expanded" else "collapsed"}-${story.clientStoryId}"
        }
        val header = label("Факты · ${facts.size} найдено · $selected выбрано   ${if (expanded) "Свернуть" else "Развернуть"}", 14, INK, display).apply {
            isClickable = true
            isFocusable = true
            setOnClickListener {
                factDecisionTouched.add(story.clientStoryId)
                if (expanded) expandedFacts.remove(story.clientStoryId) else expandedFacts.add(story.clientStoryId)
                render()
            }
        }
        wrap.addView(header)
        if (expanded) {
            facts.forEach { fact ->
                val check = CheckBox(this).apply {
                    text = fact.text
                    textSize = 15f
                    setTextColor(if (fact.evidenceSupported) INK else MUTED)
                    typeface = body
                    isChecked = fact.selected && fact.evidenceSupported
                    isEnabled = fact.evidenceSupported
                    buttonTintList = ColorStateList.valueOf(ORANGE)
                    contentDescription = "fact-${fact.factId}"
                    setOnCheckedChangeListener { _, checked -> store.setFactSelected(story.clientStoryId, fact.factId, checked) }
                }
                wrap.addView(check)
                val sourceCount = runCatching { JsonParser.parseString(fact.sourcesJson).asJsonArray.size() }.getOrDefault(0)
                wrap.addView(label(if (fact.evidenceSupported) "$sourceCount источн. · evidence есть" else "Без evidence · недоступно для выбора", 12,
                    if (fact.evidenceSupported) MUTED else ORANGE, body).apply { setPadding(dp(34), 0, 0, dp(4)) })
            }
        } else {
            facts.take(2).forEach { fact ->
                wrap.addView(label("${if (fact.selected && fact.evidenceSupported) "✓" else "·"} ${fact.text}", 13, MUTED, body).apply {
                    maxLines = 1; ellipsize = TextUtils.TruncateAt.END; setPadding(0, dp(6), 0, 0)
                })
            }
        }
        card.addView(wrap, margins(0, 0, 0, 8))
    }

    private fun renderVisual(card: LinearLayout, story: StorySnapshot) {
        when (story.stage) {
            StoryStage.VISUAL_PROCESSING -> card.addView(inlineStatus("Изображение · обработка VibePublish", "Source ingress, visual operation и verified readback"), margins(0, 0, 0, 8))
            StoryStage.VISUAL_BLOCKED -> card.addView(inlineStatus("Изображение · нужна повторная попытка", "Исходное фото и факты сохранены"), margins(0, 0, 0, 8))
        }
        story.processedImagePath?.takeIf { File(it).isFile }?.let {
            card.addView(micro("ОБРАБОТАННОЕ ИЗОБРАЖЕНИЕ", ORANGE), margins(2, 4, 2, 6))
            card.addView(imageFrame(it, 190), margins(0, 0, 0, 8))
        }
    }

    private fun renderDraft(card: LinearLayout, story: StorySnapshot) {
        val draft = story.draftText?.takeIf { it.isNotBlank() } ?: return
        val expanded = story.clientStoryId in expandedDrafts
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(0xffefede7.toInt(), 18)
            setPadding(dp(12), dp(11), dp(12), dp(11))
            contentDescription = "draft-${if (expanded) "expanded" else "collapsed"}-${story.clientStoryId}"
        }
        wrap.addView(micro("PUBLICATION TEXT", MUTED))
        if (expanded) {
            val edit = EditText(this).apply {
                setText(draft)
                textSize = 15f
                setTextColor(INK)
                typeface = body
                background = null
                gravity = Gravity.TOP
                minLines = 4
                isEnabled = story.stage !in setOf(StoryStage.SCHEDULED, StoryStage.PUBLISHED)
                setPadding(0, dp(7), 0, dp(5))
                addTextChangedListener(object : TextWatcher {
                    override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
                    override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
                    override fun afterTextChanged(s: Editable?) { store.setDraftText(story.clientStoryId, s?.toString().orEmpty()) }
                })
            }
            wrap.addView(edit)
            wrap.addView(compactAction("Свернуть", SAGE, INK) { expandedDrafts.remove(story.clientStoryId); render() })
        } else {
            wrap.addView(label(draft, 15, INK, body).apply {
                setPadding(0, dp(7), 0, dp(7)); maxLines = 4; ellipsize = TextUtils.TruncateAt.END
            })
            wrap.addView(compactAction("Развернуть", SAGE, INK) { expandedDrafts.add(story.clientStoryId); render() })
        }
        card.addView(wrap, margins(0, 0, 0, 8))
    }

    private fun renderDestinations(card: LinearLayout, story: StorySnapshot) {
        val destinations = store.destinations(story.clientStoryId)
        if (destinations.isEmpty()) return
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(SAGE, 18)
            setPadding(dp(12), dp(10), dp(12), dp(10))
            contentDescription = "provider-rows-${story.clientStoryId}"
        }
        wrap.addView(micro("DESTINATIONS · VIBEPUBLISH", MUTED))
        destinations.forEach { destination ->
            val allowed = destination.status !in setOf("unsupported", "unavailable", "needs_auth")
            if (story.stage == StoryStage.READY_TO_PUBLISH) {
                wrap.addView(CheckBox(this).apply {
                    text = "${providerTitle(destination.provider)} · ${destination.label} · ${destinationStatus(destination.status)}"
                    textSize = 14f
                    setTextColor(if (allowed) INK else MUTED)
                    typeface = body
                    isChecked = destination.selected && allowed
                    isEnabled = allowed
                    buttonTintList = ColorStateList.valueOf(ORANGE)
                    setOnCheckedChangeListener { _, checked -> store.setDestinationSelected(story.clientStoryId, destination.alias, checked) }
                })
            } else if (destination.selected || story.stage in setOf(StoryStage.SCHEDULING, StoryStage.SCHEDULED, StoryStage.PUBLISHED)) {
                wrap.addView(label("${providerTitle(destination.provider)} · ${destinationStatus(destination.status)}${scheduleText(story.scheduledFor)?.let { " · $it" } ?: ""}", 14, INK, body).apply {
                    setPadding(0, dp(7), 0, 0)
                })
            }
        }
        card.addView(wrap, margins(0, 0, 0, 8))
    }

    private fun renderInlineActions(card: LinearLayout, story: StorySnapshot) {
        val active = store.activeVoiceSession()
        if (active == null || active.storyId != story.clientStoryId) {
            card.addView(compactAction("Добавить уточнение", PAPER, INK) {
                setActiveStory(story.clientStoryId)
                requestRecording(story.clientStoryId, RecordingKind.REFINEMENT)
            }, margins(0, 2, 0, 6))
        }
        when (story.stage) {
            StoryStage.REVIEW, StoryStage.VISUAL_BLOCKED, StoryStage.NEEDS_REVIEW -> card.addView(
                compactAction(if (story.stage == StoryStage.REVIEW) "Подготовить изображение" else "Повторить изображение", ORANGE, WHITE) { queueVisual(story) },
                margins(0, 0, 0, 4),
            )
            StoryStage.READY_TO_PUBLISH -> card.addView(compactAction("Запланировать · +1 час", ORANGE, WHITE) { queuePublish(requireNotNull(store.story(story.clientStoryId))) })
            StoryStage.SCHEDULED -> card.addView(compactAction("Отменить публикацию", PAPER, ORANGE) { queueCancel(story) })
            StoryStage.VISUAL_PROCESSING, StoryStage.SCHEDULING -> card.addView(compactAction("Проверить сейчас", PAPER, INK) { SyncScheduler.enqueue(this) })
        }
    }

    private fun recorderDock(): View {
        val active = store.activeVoiceSession()
        val targetId = active?.storyId ?: activeStoryId?.takeIf { store.story(it) != null }
        val target = targetId?.let { store.story(it) }
        val dock = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(PAPER, 24, GRAPHITE_SOFT, 1)
            elevation = dp(8).toFloat()
            setPadding(dp(10), dp(9), dp(10), dp(10))
            contentDescription = "fixed-recording-dock"
        }
        dock.addView(micro(if (target == null) "MIC · ВЫБЕРИ ИСТОРИЮ" else "MIC → ${target.placeName ?: dateText(target.createdAt)}", if (active != null) ORANGE else MUTED))
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(0, dp(7), 0, dp(7)) }
        val mic = compactAction(if (active != null) "●  REC" else "●  MIC", if (active != null) ORANGE else GRAPHITE, WHITE) {
            if (active != null) return@compactAction
            if (targetId == null) {
                Toast.makeText(this, "Сначала выбери story с фотографией или создай новую", Toast.LENGTH_SHORT).show()
                return@compactAction
            }
            val kind = if (store.latestVoiceSession(targetId) == null) RecordingKind.INITIAL else RecordingKind.REFINEMENT
            requestRecording(targetId, kind)
        }.apply {
            alpha = if (active != null && pulsePhase) .58f else 1f
            contentDescription = if (active != null) "recording-mic-pulse" else "record-mic"
        }
        row.addView(mic, LinearLayout.LayoutParams(0, dp(50), if (active == null) 1f else .8f))
        if (active != null) {
            val paused = active.captureState == CaptureState.PAUSED
            row.addView(compactAction(if (paused) "Resume" else "Pause", SAGE, INK) {
                RecordingService.command(this, if (paused) RecordingService.ACTION_RESUME else RecordingService.ACTION_PAUSE)
            }.apply { contentDescription = if (paused) "record-resume" else "record-pause" }, LinearLayout.LayoutParams(0, dp(50), 1f).apply { setMargins(dp(6), 0, 0, 0) })
            row.addView(compactAction("Finish", ORANGE, WHITE) {
                RecordingService.command(this, RecordingService.ACTION_FINISH)
            }.apply { contentDescription = "record-finish" }, LinearLayout.LayoutParams(0, dp(50), 1f).apply { setMargins(dp(6), 0, 0, 0) })
        }
        dock.addView(row)
        dock.addView(compactAction("+ Новая история", SAGE, INK, ::launchPhotoPicker).apply { contentDescription = "new-story-action" }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(44)))
        return dock
    }

    private fun queueVisual(story: StorySnapshot) {
        if (!config.configured) { showSettings(); return }
        if (store.pendingOperations(story.clientStoryId).any { it.kind == "visual" }) { SyncScheduler.enqueue(this); return }
        val selectedFacts = store.facts(story.clientStoryId).filter { it.selected && it.evidenceSupported }.map { it.factId }
        val stable = "${story.clientStoryId}-${story.backendRevision}-${selectedFacts.joinToString(".")}"
        store.enqueueOperation(story.clientStoryId, "facts", newRequestKey("facts", stable), gson.toJson(mapOf("selected_fact_ids" to selectedFacts)))
        store.enqueueOperation(story.clientStoryId, "visual", newRequestKey("visual", stable), gson.toJson(mapOf("selected_fact_ids" to selectedFacts)))
        store.setStage(story.clientStoryId, StoryStage.VISUAL_PROCESSING)
        SyncScheduler.enqueue(this)
        render()
    }

    private fun queuePublish(story: StorySnapshot) {
        if (!config.configured) { showSettings(); return }
        if (store.pendingOperations(story.clientStoryId).any { it.kind == "publish" }) { SyncScheduler.enqueue(this); return }
        val aliases = store.destinations(story.clientStoryId).filter {
            it.selected && it.status !in setOf("unsupported", "unavailable", "needs_auth")
        }.map { it.alias }
        if (aliases.isEmpty()) {
            Toast.makeText(this, "Выбери хотя бы один фактически доступный destination", Toast.LENGTH_LONG).show()
            return
        }
        val stable = "${story.clientStoryId}-${story.backendRevision}-${aliases.joinToString(".")}"
        store.enqueueOperation(story.clientStoryId, "publish", newRequestKey("publish", stable), gson.toJson(mapOf(
            "destinations" to aliases,
            "delay_minutes" to 60,
            "text_override" to story.draftText.orEmpty(),
        )))
        store.setStage(story.clientStoryId, StoryStage.SCHEDULING)
        SyncScheduler.enqueue(this)
        render()
    }

    private fun queueCancel(story: StorySnapshot) {
        if (!config.configured) { showSettings(); return }
        if (store.pendingOperations(story.clientStoryId).any { it.kind == "cancel" }) { SyncScheduler.enqueue(this); return }
        store.enqueueOperation(story.clientStoryId, "cancel", newRequestKey("cancel", "${story.clientStoryId}-${story.backendRevision}"), "{}")
        SyncScheduler.enqueue(this)
        render()
    }

    private fun requestRecording(storyId: String, kind: String) {
        if (store.story(storyId) == null) return
        if (store.activeVoiceSession() != null) {
            Toast.makeText(this, "Сначала закончи текущую запись", Toast.LENGTH_SHORT).show()
            return
        }
        setActiveStory(storyId)
        pendingRecordStory = storyId
        pendingRecordKind = kind
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startPendingRecording()
        } else {
            val permissions = if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS) else arrayOf(Manifest.permission.RECORD_AUDIO)
            requestPermissions(permissions, REQUEST_AUDIO)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_AUDIO && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startPendingRecording()
        } else if (requestCode == REQUEST_AUDIO) {
            Toast.makeText(this, "Без микрофона голосовую записать нельзя", Toast.LENGTH_LONG).show()
        }
    }

    private fun startPendingRecording() {
        val story = pendingRecordStory ?: return
        val kind = pendingRecordKind ?: RecordingKind.INITIAL
        pendingRecordStory = null
        pendingRecordKind = null
        RecordingService.start(this, story, kind)
        render()
    }

    @Suppress("DEPRECATION")
    private fun launchPhotoPicker() {
        val intent = if (Build.VERSION.SDK_INT >= 33) {
            Intent(MediaStore.ACTION_PICK_IMAGES).apply { type = "image/*" }
        } else {
            Intent(Intent.ACTION_OPEN_DOCUMENT).apply { type = "image/*"; addCategory(Intent.CATEGORY_OPENABLE) }
        }
        startActivityForResult(intent, REQUEST_PHOTO)
    }

    @Deprecated("Activity result API kept intentionally small for the standalone MVP")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PHOTO || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        Thread {
            runCatching {
                val imported = PhotoImporter.import(this, uri)
                store.createStory(imported)
                imported.clientStoryId
            }.onSuccess { id ->
                runOnUiThread {
                    setActiveStory(id)
                    SyncScheduler.enqueue(this)
                    render(scrollToBottom = true)
                }
            }.onFailure { exc ->
                runOnUiThread { Toast.makeText(this, "Не удалось сохранить фото: ${exc.message}", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    private fun showSettings() {
        val wrap = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), 0, dp(20), 0) }
        val url = EditText(this).apply {
            hint = "https://street-story…"; setText(config.backendUrl.orEmpty())
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI; setSingleLine()
        }
        val token = EditText(this).apply {
            hint = if (config.deviceToken.isNullOrBlank()) "Device token" else "Device token сохранён · пусто = не менять"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD; setSingleLine()
        }
        wrap.addView(url); wrap.addView(token)
        val dialog = AlertDialog.Builder(this)
            .setTitle("Street Story backend · DevCoveer")
            .setMessage("Только HTTPS. Gemini и VibePublish credentials остаются на сервере и никогда не попадают в APK.")
            .setView(wrap)
            .setPositiveButton("Сохранить") { _, _ ->
                val value = url.text.toString().trim().trimEnd('/')
                if (!value.startsWith("https://")) {
                    Toast.makeText(this, "Нужен HTTPS-адрес backend", Toast.LENGTH_LONG).show()
                    return@setPositiveButton
                }
                config.backendUrl = value
                if (token.text.toString().isNotBlank()) config.deviceToken = token.text.toString()
                if (!config.configured) Toast.makeText(this, "Нужен device token", Toast.LENGTH_LONG).show()
                else { SyncScheduler.enqueue(this); Toast.makeText(this, "Синхронизация запущена", Toast.LENGTH_SHORT).show() }
                render()
            }
            .setNegativeButton("Отмена", null)
            .create()
        dialog.show()
        dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
    }

    private fun imageFrame(path: String, heightDp: Int): FrameLayout {
        val frame = FrameLayout(this).apply { background = rounded(SAGE, 18); clipToOutline = true }
        val image = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            decodeSampled(path, 1200, heightDp * 3)?.let { setImageBitmap(it) }
        }
        frame.addView(image, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        frame.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(heightDp))
        return frame
    }

    private fun decodeSampled(path: String, targetWidth: Int, targetHeight: Int): Bitmap? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(path, bounds)
        var sample = 1
        while (bounds.outWidth / sample > targetWidth * 2 || bounds.outHeight / sample > targetHeight * 2) sample *= 2
        BitmapFactory.decodeFile(path, BitmapFactory.Options().apply { inSampleSize = sample.coerceAtLeast(1) })
    }.getOrNull()

    private fun inlineStatus(title: String, detail: String) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; background = rounded(0xffefede7.toInt(), 16); setPadding(dp(12), dp(10), dp(12), dp(10))
        addView(label(title, 14, INK, display)); addView(label(detail, 12, MUTED, body).apply { setPadding(0, dp(3), 0, 0) })
    }

    private fun messageSurface(text: String) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; background = rounded(0xffffeee8.toInt(), 16); setPadding(dp(12), dp(10), dp(12), dp(10))
        addView(label(text, 14, INK, body).apply { setLineSpacing(dp(1).toFloat(), 1.05f) })
    }

    private fun statusText(stage: String): String = when (stage) {
        StoryStage.PHOTO_READY -> "Фото сохранено · можно записывать голос"
        StoryStage.RECORDING -> "Запись"
        StoryStage.QUEUED -> "Голос сохранён · отправляем"
        StoryStage.RESEARCHING -> "Research в фоне"
        StoryStage.REVIEW -> "Нужно выбрать факты"
        StoryStage.VISUAL_PROCESSING -> "VibePublish готовит изображение"
        StoryStage.VISUAL_BLOCKED -> "Изображение требует повтора"
        StoryStage.READY_TO_PUBLISH -> "Готово к публикации"
        StoryStage.SCHEDULING -> "VibePublish планирует публикацию"
        StoryStage.SCHEDULED -> "Запланировано"
        StoryStage.PUBLISHED -> "Опубликовано"
        else -> "Нужна проверка"
    }

    private fun captureStatus(activity: String): String = when (activity) {
        CaptureActivity.VOICE -> "Записываю голос"
        CaptureActivity.AUTO_SILENCE -> "Слушаю · тишина пропускается"
        CaptureActivity.FALLBACK_CONTINUOUS -> "VAD fail-open · записываю всё"
        CaptureActivity.MANUAL_PAUSE -> "Пауза"
        else -> "Запись"
    }

    private fun providerTitle(provider: String): String = when (provider.lowercase(Locale.US)) {
        "telegram" -> "Telegram"; "vk" -> "VK"; "max" -> "MAX"; else -> provider
    }

    private fun destinationStatus(status: String): String = when (status) {
        "supported" -> "доступно"
        "needs_review" -> "нужна проверка"
        "needs_auth" -> "нужна авторизация"
        "scheduled" -> "запланировано"
        "verified" -> "подтверждено"
        "cancelled" -> "отменено"
        "published" -> "опубликовано"
        else -> status.replace('_', ' ')
    }

    private fun dateText(epoch: Long): String = DateTimeFormatter.ofPattern("d MMM · HH:mm", Locale.forLanguageTag("ru-RU"))
        .format(Instant.ofEpochMilli(epoch).atZone(ZoneId.systemDefault()))

    private fun scheduleText(value: String?): String? = value?.let { raw ->
        runCatching { DateTimeFormatter.ofPattern("HH:mm", Locale.US).format(OffsetDateTime.parse(raw).atZoneSameInstant(ZoneId.systemDefault())) }.getOrElse { raw }
    }

    private fun dp(value: Int) = Math.round(value * resources.displayMetrics.density)
    private val display = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    private val body = Typeface.create("sans-serif", Typeface.NORMAL)

    private fun label(value: String, sp: Int, color: Int, face: Typeface) = TextView(this).apply {
        text = value; textSize = sp.toFloat(); setTextColor(color); typeface = face; includeFontPadding = false
    }

    private fun micro(value: String, color: Int) = label(value, 10, color, display).apply { letterSpacing = .12f }

    private fun rounded(color: Int, radius: Int, stroke: Int = 0, strokeDp: Int = 0) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radius).toFloat(); if (strokeDp > 0) setStroke(dp(strokeDp), stroke)
    }

    private fun buttonBackground(fill: Int, stroke: Int = fill) = RippleDrawable(
        ColorStateList.valueOf(0x22000000), rounded(fill, 16, stroke, if (fill == PAPER) 1 else 0), null,
    )

    private fun compactAction(textValue: String, fill: Int, textColor: Int, action: () -> Unit) = label(textValue, 14, textColor, display).apply {
        gravity = Gravity.CENTER; minHeight = dp(42); setPadding(dp(12), 0, dp(12), 0); background = buttonBackground(fill)
        isClickable = true; isFocusable = true; setOnClickListener { action() }
    }

    private fun margins(left: Int, top: Int, right: Int, bottom: Int) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
    ).apply { setMargins(dp(left), dp(top), dp(right), dp(bottom)) }

    companion object {
        private const val REQUEST_PHOTO = 61
        private const val REQUEST_AUDIO = 71
        private const val SAGE = 0xffd9e4df.toInt()
        private const val GRAPHITE = 0xff292b29.toInt()
        private const val GRAPHITE_SOFT = 0xff3b3e3a.toInt()
        private const val PAPER = 0xfff6f3ed.toInt()
        private const val INK = 0xff202220.toInt()
        private const val MUTED = 0xff6d746f.toInt()
        private const val ORANGE = 0xffef4b23.toInt()
        private const val WHITE = 0xffffffff.toInt()
    }
}
