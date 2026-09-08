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
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.text.Editable
import android.text.InputType
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
    private val uiPrefs by lazy { getSharedPreferences("street_story_ui", MODE_PRIVATE) }
    private val gson = Gson()
    private val handler = Handler(Looper.getMainLooper())
    private var selectedStoryId: String? = null
    private var pendingRecordStory: String? = null
    private var pendingRecordKind: String? = null
    private var receiverRegistered = false

    private val changedReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            intent?.getStringExtra(RecordingService.EXTRA_MESSAGE)?.takeIf { it.isNotBlank() }?.let { Toast.makeText(this@MainActivity, it, Toast.LENGTH_SHORT).show() }
            render()
        }
    }

    private val liveTick = object : Runnable {
        override fun run() {
            val active = store.activeVoiceSession()
            if (active != null && active.storyId == selectedStoryId) render()
            handler.postDelayed(this, 700)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        selectedStoryId = savedInstanceState?.getString("story_id") ?: uiPrefs.getString("story_id", null)
        window.statusBarColor = SAGE; window.navigationBarColor = SAGE
        if (Build.VERSION.SDK_INT >= 23) window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        if (Build.VERSION.SDK_INT >= 26) window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        render()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter().apply { addAction(RecordingService.ACTION_STATE_CHANGED); addAction(SyncWorker.ACTION_STATE_CHANGED) }
        ContextCompat.registerReceiver(this, changedReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        receiverRegistered = true
        handler.post(liveTick)
        SyncScheduler.enqueue(this)
    }

    override fun onStop() {
        handler.removeCallbacks(liveTick)
        if (receiverRegistered) { unregisterReceiver(changedReceiver); receiverRegistered = false }
        super.onStop()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("story_id", selectedStoryId)
        super.onSaveInstanceState(outState)
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (selectedStoryId != null) showFeed() else super.onBackPressed()
    }

    private fun selectStory(id: String?) {
        selectedStoryId = id
        uiPrefs.edit().apply { if (id == null) remove("story_id") else putString("story_id", id) }.apply()
    }

    private fun showFeed() { selectStory(null); render() }

    private fun render() {
        val selected = selectedStoryId?.let { store.story(it) }
        if (selectedStoryId != null && selected == null) selectStory(null)
        val root = ScrollView(this).apply { isFillViewport = true; setBackgroundColor(SAGE); clipToPadding = false }
        val page = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(18), dp(22), dp(18), dp(36)) }
        root.addView(page)
        if (selected == null) renderFeed(page) else renderStory(page, selected)
        setContentView(root)
    }

    private fun renderFeed(page: LinearLayout) {
        page.addView(micro("STREET STORY", INK).apply { setPadding(dp(3), 0, 0, dp(10)) })
        page.addView(label("Истории города", 40, INK, display).apply { letterSpacing = -0.025f })
        page.addView(label("Фото, голос, проверенные факты — и публикация уже ждёт своего часа.", 18, GRAPHITE_SOFT, bodyLight).apply { setPadding(0, dp(7), 0, dp(18)); setLineSpacing(0f, 1.12f) })

        if (!config.configured) {
            val setup = surface(PAPER, 26).apply { setPadding(dp(18), dp(16), dp(18), dp(17)) }
            setup.addView(micro("СЕРВЕР", ORANGE))
            setup.addView(label("Можно снимать офлайн", 25, INK, display).apply { setPadding(0, dp(8), 0, dp(5)) })
            setup.addView(label("Фото и голос сохранятся на телефоне. Для исследования и публикации один раз укажи HTTPS-адрес Street Story backend на DevCoveer.", 16, MUTED, bodyLight))
            setup.addView(action("Настроить backend", GRAPHITE, PAPER, GRAPHITE, 0, ::showSettings), margins(0, 14, 0, 0))
            page.addView(setup, margins(0, 0, 0, 12))
        }

        val active = store.activeVoiceSession()
        val primaryText = if (active != null) "Продолжить голосовую" else "Новая история"
        page.addView(action(primaryText, ORANGE, WHITE, ORANGE, 0) {
            if (active != null) { selectStory(active.storyId); render() } else launchPhotoPicker()
        }, margins(0, 0, 0, 16))

        val stories = store.stories()
        if (stories.isEmpty()) {
            val empty = surface(GRAPHITE, 30).apply { setPadding(dp(20), dp(20), dp(20), dp(22)) }
            empty.addView(micro("ПЕРВАЯ ИСТОРИЯ", ORANGE))
            empty.addView(label("Увидел — сохранил", 34, PAPER, display).apply { setPadding(0, dp(12), 0, dp(7)) })
            empty.addView(label("Сначала фотография надёжно останется на телефоне. Потом можно спокойно наговорить даже длинную заметку и закрыть приложение.", 17, 0xffcbd0cb, editorial))
            page.addView(empty)
        } else {
            page.addView(micro("ИСТОРИИ", MUTED), margins(3, 0, 0, 10))
            stories.forEach { page.addView(feedCard(it), margins(0, 0, 0, 11)) }
        }
        page.addView(action("Настройка  ↗", SAGE, MUTED, MUTED, 0, ::showSettings), margins(0, 4, 0, 0))
    }

    private fun feedCard(story: StorySnapshot): View {
        val card = surface(PAPER, 26).apply { setPadding(dp(12), dp(12), dp(14), dp(12)); isClickable = true; isFocusable = true; setOnClickListener { selectStory(story.clientStoryId); render() } }
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        row.addView(imageFrame(story.processedImagePath ?: story.photoPath, 92, 92), LinearLayout.LayoutParams(dp(92), dp(92)))
        val copy = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(14), 0, 0, 0) }
        copy.addView(micro(statusText(story.stage).uppercase(Locale.getDefault()), if (story.stage == StoryStage.NEEDS_REVIEW || story.stage == StoryStage.VISUAL_BLOCKED) ORANGE else MUTED))
        copy.addView(label(story.placeName ?: "Новая городская история", 21, INK, display).apply { setPadding(0, dp(5), 0, dp(4)); maxLines = 2 })
        val destinations = store.destinations(story.clientStoryId).filter { it.selected }.joinToString(" · ") { it.provider.uppercase(Locale.US) }
        val meta = listOf(dateText(story.createdAt), destinations.takeIf { it.isNotBlank() }, scheduleText(story.scheduledFor)).filterNotNull().joinToString(" · ")
        copy.addView(label(meta, 13, MUTED, body))
        row.addView(copy, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)); card.addView(row)
        return card
    }

    private fun renderStory(page: LinearLayout, story: StorySnapshot) {
        page.addView(micro("←  STREET STORY", MUTED).apply { isClickable = true; setPadding(dp(3), dp(2), 0, dp(14)); setOnClickListener { showFeed() } })
        val active = store.activeVoiceSession()
        if (active != null && active.storyId == story.clientStoryId) {
            renderVoice(page, story, active); return
        }
        when (story.stage) {
            StoryStage.PHOTO_READY, StoryStage.RECORDING -> renderVoice(page, story, null)
            StoryStage.QUEUED, StoryStage.RESEARCHING, StoryStage.SCHEDULING -> renderProcessing(page, story)
            StoryStage.REVIEW, StoryStage.VISUAL_PROCESSING, StoryStage.VISUAL_BLOCKED, StoryStage.READY_TO_PUBLISH, StoryStage.NEEDS_REVIEW -> renderReview(page, story)
            StoryStage.SCHEDULED, StoryStage.PUBLISHED -> renderFinal(page, story)
            else -> renderProcessing(page, story)
        }
    }

    private fun renderVoice(page: LinearLayout, story: StorySnapshot, active: VoiceSessionSnapshot?) {
        page.addView(imageFrame(story.photoPath, -1, 270), margins(0, 0, 0, 14))
        val kind = active?.kind ?: if (store.latestVoiceSession(story.clientStoryId)?.kind == RecordingKind.INITIAL && story.stage != StoryStage.PHOTO_READY) RecordingKind.REFINEMENT else RecordingKind.INITIAL
        page.addView(micro(if (kind == RecordingKind.REFINEMENT) "УТОЧНЕНИЕ" else "ГОЛОС", ORANGE))
        page.addView(label(if (kind == RecordingKind.REFINEMENT) "Что уточнить?" else "Что здесь интересно?", 38, INK, display).apply { letterSpacing = -0.025f; setPadding(0, dp(9), 0, dp(7)) })
        page.addView(label("Говори как есть. Тишина автоматически пропускается, длинная запись складывается в durable M4A-чанки — закрывать экран и приложение можно.", 17, GRAPHITE_SOFT, bodyLight).apply { setLineSpacing(dp(1).toFloat(), 1.08f); setPadding(0, 0, 0, dp(16)) })

        if (active == null) {
            page.addView(action("Начать запись", ORANGE, WHITE, ORANGE, 0) { requestRecording(story.clientStoryId, kind) })
        } else {
            val live = runtime.snapshotFor(active.sessionId)
            val duration = live?.durationMs ?: active.durationMs
            val activity = live?.captureActivity ?: active.captureActivity
            val hero = surface(GRAPHITE, 30).apply { setPadding(dp(20), dp(18), dp(20), dp(20)) }
            hero.addView(micro(captureStatus(activity), ORANGE))
            hero.addView(label(formatDuration(duration), 48, PAPER, display).apply { setPadding(0, dp(10), 0, dp(5)); letterSpacing = -0.03f })
            hero.addView(label("${active.chunkCount} сохранённых чанков · ${formatDuration(live?.autoSilenceSkippedMs ?: active.autoSilenceSkippedMs)} тишины пропущено", 15, 0xffcbd0cb, body))
            page.addView(hero, margins(0, 0, 0, 12))
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            val paused = active.captureState == CaptureState.PAUSED
            row.addView(action(if (paused) "Продолжить" else "Пауза", PAPER, INK, INK, 1) { RecordingService.command(this, if (paused) RecordingService.ACTION_RESUME else RecordingService.ACTION_PAUSE) }, LinearLayout.LayoutParams(0, dp(58), 1f))
            row.addView(action("Готово", ORANGE, WHITE, ORANGE, 0) { RecordingService.command(this, RecordingService.ACTION_FINISH) }, LinearLayout.LayoutParams(0, dp(58), 1f).apply { setMargins(dp(8), 0, 0, 0) })
            page.addView(row)
        }
    }

    private fun renderProcessing(page: LinearLayout, story: StorySnapshot) {
        page.addView(imageFrame(story.photoPath, -1, 270), margins(0, 0, 0, 14))
        val hero = surface(GRAPHITE, 30).apply { setPadding(dp(20), dp(19), dp(20), dp(20)) }
        hero.addView(micro("СЕЙЧАС", ORANGE))
        hero.addView(label(statusText(story.stage), 36, PAPER, display).apply { setPadding(0, dp(12), 0, dp(7)); letterSpacing = -0.025f })
        hero.addView(label(if (config.configured) "Можно закрыть приложение. WorkManager продолжит отправку и сверку с сервером." else "Сервер пока не настроен. Фото и голос остаются локально и никуда не пропадут.", 17, 0xffcbd0cb, editorial))
        page.addView(hero, margins(0, 0, 0, 12))
        story.lastError?.let { page.addView(messageSurface(it), margins(0, 0, 0, 12)) }
        page.addView(action(if (config.configured) "Проверить сейчас" else "Настроить backend", PAPER, INK, INK, 1) { if (config.configured) SyncScheduler.enqueue(this) else showSettings() })
    }

    private fun renderReview(page: LinearLayout, story: StorySnapshot) {
        page.addView(imageFrame(story.processedImagePath ?: story.photoPath, -1, 290), margins(0, 0, 0, 14))
        page.addView(micro("${statusText(story.stage).uppercase(Locale.getDefault())}", if (story.stage == StoryStage.VISUAL_BLOCKED || story.stage == StoryStage.NEEDS_REVIEW) ORANGE else MUTED))
        page.addView(label(story.placeName ?: "Что удалось узнать", 36, INK, display).apply { setPadding(0, dp(8), 0, dp(7)); letterSpacing = -0.025f })
        story.summary?.takeIf { it.isNotBlank() }?.let { page.addView(label(it, 17, GRAPHITE_SOFT, bodyLight).apply { setLineSpacing(dp(1).toFloat(), 1.08f); setPadding(0, 0, 0, dp(15)) }) }
        story.lastError?.let { page.addView(messageSurface(it), margins(0, 0, 0, 12)) }

        val facts = store.facts(story.clientStoryId)
        if (facts.isNotEmpty()) {
            val factSurface = surface(PAPER, 26).apply { setPadding(dp(17), dp(16), dp(17), dp(12)) }
            factSurface.addView(micro("ФАКТЫ", MUTED))
            facts.forEach { fact ->
                val wrap = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(10), 0, dp(10)) }
                val check = CheckBox(this).apply {
                    text = fact.text; textSize = 17f; setTextColor(INK); typeface = body; isChecked = fact.selected && fact.evidenceSupported
                    buttonTintList = ColorStateList.valueOf(ORANGE); isEnabled = fact.evidenceSupported
                    setOnCheckedChangeListener { _, checked -> store.setFactSelected(story.clientStoryId, fact.factId, checked) }
                }
                wrap.addView(check)
                val sourceCount = runCatching { JsonParser.parseString(fact.sourcesJson).asJsonArray.size() }.getOrDefault(0)
                wrap.addView(label(if (fact.evidenceSupported) "$sourceCount источн. · confidence ${"%.2f".format(Locale.US, fact.confidence)}" else "Нет внешнего evidence — в публикацию не включается", 13, if (fact.evidenceSupported) MUTED else ORANGE, body).apply { setPadding(dp(34), dp(3), 0, 0) })
                factSurface.addView(wrap)
            }
            page.addView(factSurface, margins(0, 0, 0, 12))
        }

        if (story.stage == StoryStage.VISUAL_BLOCKED) {
            page.addView(messageSurface("Imagegen runtime VibePublish сейчас недоступен. Исследование, факты, голос и исходное фото сохранены; новую story создавать не нужно."), margins(0, 0, 0, 12))
        }

        val latest = store.latestVoiceSession(story.clientStoryId)
        if (latest?.captureState != CaptureState.RECORDING && latest?.captureState != CaptureState.PAUSED && story.stage != StoryStage.SCHEDULING) {
            page.addView(action("Уточнить голосом", PAPER, INK, INK, 1) { requestRecording(story.clientStoryId, RecordingKind.REFINEMENT) }, margins(0, 0, 0, 9))
        }

        when (story.stage) {
            StoryStage.REVIEW, StoryStage.VISUAL_BLOCKED, StoryStage.NEEDS_REVIEW -> page.addView(action(if (story.stage == StoryStage.VISUAL_BLOCKED) "Повторить изображение" else "Готовить изображение", ORANGE, WHITE, ORANGE, 0) { queueVisual(story) })
            StoryStage.VISUAL_PROCESSING -> page.addView(action("Проверить обработку", PAPER, INK, INK, 1) { SyncScheduler.enqueue(this) })
            StoryStage.READY_TO_PUBLISH -> renderPublishControls(page, story)
        }
    }

    private fun renderPublishControls(page: LinearLayout, story: StorySnapshot) {
        val copySurface = surface(PAPER, 26).apply { setPadding(dp(17), dp(16), dp(17), dp(17)) }
        copySurface.addView(micro("ТЕКСТ", MUTED))
        val edit = EditText(this).apply {
            setText(story.draftText.orEmpty()); setTextSize(17f); setTextColor(INK); typeface = body; background = null; minLines = 5; gravity = Gravity.TOP
            setPadding(0, dp(9), 0, 0); addTextChangedListener(object : TextWatcher { override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}; override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}; override fun afterTextChanged(s: Editable?) { store.setDraftText(story.clientStoryId, s?.toString().orEmpty()) } })
        }
        copySurface.addView(edit); page.addView(copySurface, margins(0, 0, 0, 12))

        val destinations = store.destinations(story.clientStoryId)
        val destSurface = surface(PAPER, 26).apply { setPadding(dp(17), dp(16), dp(17), dp(12)) }
        destSurface.addView(micro("КУДА", MUTED))
        if (destinations.isEmpty()) {
            destSurface.addView(label("VibePublish пока не вернул доступных destination. Ничего не придумываем локально.", 16, MUTED, bodyLight).apply { setPadding(0, dp(9), 0, dp(8)) })
        } else destinations.forEach { destination ->
            val allowed = destination.status !in setOf("unsupported", "unavailable", "needs_auth")
            destSurface.addView(CheckBox(this).apply {
                text = "${destination.label} · ${destination.provider.uppercase(Locale.US)}${if (destination.status == "supported") "" else " · ${destination.status}"}"
                textSize = 16f; setTextColor(if (allowed) INK else MUTED); isChecked = destination.selected && allowed; isEnabled = allowed; buttonTintList = ColorStateList.valueOf(ORANGE)
                setOnCheckedChangeListener { _, checked -> store.setDestinationSelected(story.clientStoryId, destination.alias, checked) }
            })
        }
        page.addView(destSurface, margins(0, 0, 0, 12))
        page.addView(action("В отложенные · через 1 час", ORANGE, WHITE, ORANGE, 0) { queuePublish(requireNotNull(store.story(story.clientStoryId))) })
    }

    private fun renderFinal(page: LinearLayout, story: StorySnapshot) {
        page.addView(imageFrame(story.processedImagePath ?: story.photoPath, -1, 310), margins(0, 0, 0, 14))
        page.addView(micro(if (story.stage == StoryStage.PUBLISHED) "ОПУБЛИКОВАНО" else "В ОТЛОЖЕННЫХ", ORANGE))
        page.addView(label(story.placeName ?: "Городская история", 38, INK, display).apply { setPadding(0, dp(9), 0, dp(7)); letterSpacing = -0.025f })
        val scheduled = scheduleText(story.scheduledFor)
        page.addView(label(if (story.stage == StoryStage.PUBLISHED) "Публикация подтверждена сервером." else "Provider-native публикация запланирована${scheduled?.let { " · $it" } ?: ""}.", 17, GRAPHITE_SOFT, bodyLight).apply { setPadding(0, 0, 0, dp(16)) })
        val destinations = store.destinations(story.clientStoryId).filter { it.selected }.joinToString(" · ") { "${it.label} / ${it.provider.uppercase(Locale.US)}" }
        if (destinations.isNotBlank()) page.addView(messageSurface(destinations), margins(0, 0, 0, 12))
        page.addView(action("К историям", PAPER, INK, INK, 1, ::showFeed))
    }

    private fun queueVisual(story: StorySnapshot) {
        if (!config.configured) { showSettings(); return }
        if (store.pendingOperations(story.clientStoryId).any { it.kind == "visual" }) { SyncScheduler.enqueue(this); return }
        val selectedFacts = store.facts(story.clientStoryId).filter { it.selected && it.evidenceSupported }.map { it.factId }
        val payload = gson.toJson(mapOf("selected_fact_ids" to selectedFacts))
        store.enqueueOperation(story.clientStoryId, "visual", newRequestKey("visual", "${story.clientStoryId}-${System.currentTimeMillis()}"), payload)
        store.setStage(story.clientStoryId, StoryStage.VISUAL_PROCESSING)
        SyncScheduler.enqueue(this); render()
    }

    private fun queuePublish(story: StorySnapshot) {
        if (!config.configured) { showSettings(); return }
        if (store.pendingOperations(story.clientStoryId).any { it.kind == "publish" }) { SyncScheduler.enqueue(this); return }
        val aliases = store.destinations(story.clientStoryId).filter { it.selected && it.status !in setOf("unsupported", "unavailable", "needs_auth") }.map { it.alias }
        if (aliases.isEmpty()) { Toast.makeText(this, "Выбери хотя бы один фактически доступный destination", Toast.LENGTH_LONG).show(); return }
        val payload = gson.toJson(mapOf("destinations" to aliases, "delay_minutes" to 60, "text_override" to story.draftText.orEmpty()))
        store.enqueueOperation(story.clientStoryId, "publish", newRequestKey("publish", "${story.clientStoryId}-${System.currentTimeMillis()}"), payload)
        store.setStage(story.clientStoryId, StoryStage.SCHEDULING)
        SyncScheduler.enqueue(this); render()
    }

    private fun requestRecording(storyId: String, kind: String) {
        if (store.activeVoiceSession() != null) { Toast.makeText(this, "Сначала закончи текущую запись", Toast.LENGTH_SHORT).show(); return }
        pendingRecordStory = storyId; pendingRecordKind = kind
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) startPendingRecording()
        else {
            val permissions = if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS) else arrayOf(Manifest.permission.RECORD_AUDIO)
            requestPermissions(permissions, REQUEST_AUDIO)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_AUDIO && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) startPendingRecording()
        else if (requestCode == REQUEST_AUDIO) Toast.makeText(this, "Без микрофона голосовую записать нельзя", Toast.LENGTH_LONG).show()
    }

    private fun startPendingRecording() {
        val story = pendingRecordStory ?: return; val kind = pendingRecordKind ?: RecordingKind.INITIAL
        pendingRecordStory = null; pendingRecordKind = null; RecordingService.start(this, story, kind); render()
    }

    @Suppress("DEPRECATION")
    private fun launchPhotoPicker() {
        val intent = if (Build.VERSION.SDK_INT >= 33) Intent(MediaStore.ACTION_PICK_IMAGES).apply { type = "image/*" }
        else Intent(Intent.ACTION_OPEN_DOCUMENT).apply { type = "image/*"; addCategory(Intent.CATEGORY_OPENABLE) }
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
            }.onSuccess { id -> runOnUiThread { selectStory(id); render() } }
                .onFailure { exc -> runOnUiThread { Toast.makeText(this, "Не удалось сохранить фото: ${exc.message}", Toast.LENGTH_LONG).show() } }
        }.start()
    }

    private fun showSettings() {
        val wrap = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), 0, dp(20), 0) }
        val url = EditText(this).apply { hint = "https://street-story…"; setText(config.backendUrl.orEmpty()); inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI; setSingleLine() }
        val token = EditText(this).apply { hint = if (config.deviceToken.isNullOrBlank()) "Device token" else "Device token сохранён · оставь пустым, чтобы не менять"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD; setSingleLine() }
        wrap.addView(url); wrap.addView(token)
        val dialog = AlertDialog.Builder(this).setTitle("Street Story backend · DevCoveer")
            .setMessage("Только HTTPS. Gemini и VibePublish credentials остаются на сервере и никогда не попадают в APK.")
            .setView(wrap).setPositiveButton("Сохранить") { _, _ ->
                val value = url.text.toString().trim().trimEnd('/')
                if (!value.startsWith("https://")) { Toast.makeText(this, "Нужен HTTPS-адрес backend", Toast.LENGTH_LONG).show(); return@setPositiveButton }
                config.backendUrl = value
                if (token.text.toString().isNotBlank()) config.deviceToken = token.text.toString()
                if (!config.configured) Toast.makeText(this, "Нужен device token", Toast.LENGTH_LONG).show() else { SyncScheduler.enqueue(this); Toast.makeText(this, "Синхронизация запущена", Toast.LENGTH_SHORT).show() }
                render()
            }.setNegativeButton("Отмена", null).create()
        dialog.show(); dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
    }

    private fun imageFrame(path: String, widthDp: Int, heightDp: Int): FrameLayout {
        val frame = FrameLayout(this).apply { background = rounded(PAPER, 26); clipToOutline = true }
        val image = ImageView(this).apply { scaleType = ImageView.ScaleType.CENTER_CROP; decodeSampled(path, if (widthDp > 0) widthDp * 3 else 1200, heightDp * 3)?.let { setImageBitmap(it) } }
        frame.addView(image, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        if (widthDp > 0) frame.layoutParams = LinearLayout.LayoutParams(dp(widthDp), dp(heightDp)) else frame.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(heightDp))
        return frame
    }

    private fun decodeSampled(path: String, targetWidth: Int, targetHeight: Int): Bitmap? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }; BitmapFactory.decodeFile(path, bounds)
        var sample = 1; while (bounds.outWidth / sample > targetWidth * 2 || bounds.outHeight / sample > targetHeight * 2) sample *= 2
        BitmapFactory.decodeFile(path, BitmapFactory.Options().apply { inSampleSize = sample.coerceAtLeast(1) })
    }.getOrNull()

    private fun messageSurface(text: String): LinearLayout = surface(0xffffeee8, 22).apply { setPadding(dp(16), dp(14), dp(16), dp(14)); addView(label(text, 15, INK, body).apply { setLineSpacing(dp(1).toFloat(), 1.08f) }) }
    private fun statusText(stage: String): String = when (stage) {
        StoryStage.PHOTO_READY -> "Нужно наговорить"
        StoryStage.RECORDING -> "Записываем"
        StoryStage.QUEUED -> "Отправляем"
        StoryStage.RESEARCHING -> "Исследуем"
        StoryStage.REVIEW -> "Нужно выбрать факты"
        StoryStage.VISUAL_PROCESSING -> "Готовим изображение"
        StoryStage.VISUAL_BLOCKED -> "Изображение ждёт runtime"
        StoryStage.READY_TO_PUBLISH -> "Готово к публикации"
        StoryStage.SCHEDULING -> "Отправляем в отложенные"
        StoryStage.SCHEDULED -> "Запланировано"
        StoryStage.PUBLISHED -> "Опубликовано"
        else -> "Нужна проверка"
    }
    private fun captureStatus(activity: String): String = when (activity) { CaptureActivity.VOICE -> "ЗАПИСЫВАЮ ГОЛОС"; CaptureActivity.AUTO_SILENCE -> "ТИШИНА · НЕ ЗАПИСЫВАЕТСЯ"; CaptureActivity.FALLBACK_CONTINUOUS -> "ЗАПИСЫВАЮ ВСЁ · VAD FAIL-OPEN"; CaptureActivity.MANUAL_PAUSE -> "ПАУЗА"; else -> "ЗАПИСЬ" }
    private fun dateText(epoch: Long): String = DateTimeFormatter.ofPattern("d MMM", Locale.forLanguageTag("ru-RU")).format(Instant.ofEpochMilli(epoch).atZone(ZoneId.systemDefault()))
    private fun scheduleText(value: String?): String? = value?.let { runCatching { DateTimeFormatter.ofPattern("HH:mm", Locale.US).format(OffsetDateTime.parse(it).atZoneSameInstant(ZoneId.systemDefault())) }.getOrElse { it } }

    private fun dp(value: Int) = Math.round(value * resources.displayMetrics.density)
    private val display = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    private val body = Typeface.create("sans-serif", Typeface.NORMAL)
    private val bodyLight = Typeface.create("sans-serif-light", Typeface.NORMAL)
    private val editorial = Typeface.create("serif", Typeface.ITALIC)
    private fun label(value: String, sp: Int, color: Int, face: Typeface) = TextView(this).apply { text = value; textSize = sp.toFloat(); setTextColor(color); typeface = face; includeFontPadding = false }
    private fun micro(value: String, color: Int) = label(value, 11, color, display).apply { letterSpacing = .16f }
    private fun surface(color: Int, radius: Int) = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; background = rounded(color, radius); clipToOutline = true; elevation = dp(1).toFloat() }
    private fun rounded(color: Int, radius: Int) = GradientDrawable().apply { setColor(color); cornerRadius = dp(radius).toFloat() }
    private fun margins(left: Int, top: Int, right: Int, bottom: Int) = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { setMargins(dp(left), dp(top), dp(right), dp(bottom)) }
    private fun buttonBackground(fill: Int, stroke: Int, strokeDp: Int, radius: Int) = RippleDrawable(ColorStateList.valueOf(0x22000000), GradientDrawable().apply { setColor(fill); cornerRadius = dp(radius).toFloat(); if (strokeDp > 0) setStroke(dp(strokeDp), stroke) }, null)
    private fun action(textValue: String, fill: Int, textColor: Int, stroke: Int, strokeDp: Int, action: () -> Unit) = label(textValue, 17, textColor, display).apply { gravity = Gravity.CENTER; minHeight = dp(58); setPadding(dp(16), 0, dp(16), 0); background = buttonBackground(fill, stroke, strokeDp, 22); isClickable = true; isFocusable = true; setOnClickListener { action() } }

    companion object {
        private const val REQUEST_PHOTO = 61
        private const val REQUEST_AUDIO = 71
        private const val SAGE = 0xffd9e4df.toInt(); private const val GRAPHITE = 0xff292b29.toInt(); private const val GRAPHITE_SOFT = 0xff3b3e3a.toInt(); private const val PAPER = 0xfff6f3ed.toInt(); private const val INK = 0xff202220.toInt(); private const val MUTED = 0xff6d746f.toInt(); private const val ORANGE = 0xffef4b23.toInt(); private const val WHITE = 0xffffffff.toInt()
    }
}
