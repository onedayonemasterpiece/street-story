package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.text.InputType
import android.text.method.LinkMovementMethod
import android.text.util.Linkify
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
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
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

class MainActivity : Activity() {
    private val store by lazy { AppGraph.store(this) }
    private val config by lazy { AppGraph.config(this) }
    private val live by lazy { AppGraph.live(this) }
    private val research by lazy { ResearchProjectionStore(this) }
    private val gson = Gson()
    private val prefs by lazy { getSharedPreferences("street_story_topics_v1", MODE_PRIVATE) }

    private lateinit var root: FrameLayout
    private lateinit var contentHost: FrameLayout
    private lateinit var dock: LinearLayout

    private var activeStoryId: String? = null
    private var pendingLiveStoryId: String? = null
    private var receiverRegistered = false

    private var topicTitleView: TextView? = null
    private var topicStatusView: TextView? = null
    private var previewImage: ImageView? = null
    private var previewText: TextView? = null
    private var literalBanner: TextView? = null
    private var lastChangeView: TextView? = null
    private var sourceButton: Button? = null
    private var undoButton: Button? = null
    private var publishButton: Button? = null
    private var confirmationBox: LinearLayout? = null
    private var dockTopic: TextView? = null
    private var dockStatus: TextView? = null
    private var micButton: Button? = null
    private var shownImagePath: String? = null

    private val liveListener: (LiveUiState) -> Unit = { state ->
        runOnUiThread { applyLiveState(state) }
    }

    private val changedReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            intent?.getStringExtra(RecordingService.EXTRA_MESSAGE)
                ?.takeIf { it.isNotBlank() }
                ?.let { Toast.makeText(this@MainActivity, it, Toast.LENGTH_SHORT).show() }
            refreshSurface()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        activeStoryId = savedInstanceState?.getString("active_story_id")
            ?: prefs.getString("active_story_id", null)
        window.statusBarColor = SAGE
        window.navigationBarColor = SAGE
        buildChrome()
        val active = activeStoryId
        if (active != null && store.story(active) != null) showTopic(active) else showTopics()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter().apply {
            addAction(RecordingService.ACTION_STATE_CHANGED)
            addAction(SyncWorker.ACTION_STATE_CHANGED)
        }
        ContextCompat.registerReceiver(this, changedReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        receiverRegistered = true
        live.addListener(liveListener)
        SyncScheduler.enqueue(this)
    }

    override fun onStop() {
        live.removeListener(liveListener)
        if (receiverRegistered) {
            unregisterReceiver(changedReceiver)
            receiverRegistered = false
        }
        super.onStop()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("active_story_id", activeStoryId)
        super.onSaveInstanceState(outState)
    }

    @Deprecated("Single-activity product navigation")
    override fun onBackPressed() {
        if (activeStoryId != null) showTopics() else super.onBackPressed()
    }

    private fun buildChrome() {
        root = FrameLayout(this).apply { setBackgroundColor(SAGE) }
        contentHost = FrameLayout(this)
        root.addView(
            contentHost,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ).apply { bottomMargin = dp(106) },
        )
        dock = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            background = rounded(PAPER, 22)
            elevation = dp(10).toFloat()
            setPadding(dp(16), dp(10), dp(16), dp(10))
            visibility = View.GONE
        }
        root.addView(
            dock,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(96)).apply {
                gravity = Gravity.BOTTOM
                leftMargin = dp(9)
                rightMargin = dp(9)
                bottomMargin = dp(6)
            },
        )
        setContentView(root)
    }

    private fun showTopics() {
        setActiveStory(null)
        clearTopicRefs()
        dock.visibility = View.GONE
        contentHost.removeAllViews()

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
        }
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(18), dp(18), dp(28))
        }
        scroll.addView(column)
        contentHost.addView(scroll)

        val heading = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        heading.addView(label("Темы", 30, INK, Typeface.DEFAULT_BOLD), LinearLayout.LayoutParams(0, -2, 1f))
        heading.addView(secondaryButton("Настройки") { showSettings() })
        column.addView(heading)

        val create = primaryButton("+ Новая тема") { launchPhotoPicker() }.apply {
            contentDescription = "new-topic"
        }
        column.addView(create, blockMargins(top = 18, bottom = 20))

        val stories = store.stories()
        if (stories.isEmpty()) {
            column.addView(
                label(
                    "Выберите недавнее фото и расскажите голосом, какой пост хотите получить.",
                    15,
                    MUTED,
                    Typeface.DEFAULT,
                ).apply { setPadding(0, dp(16), 0, 0) }
            )
        } else {
            stories.forEach { story ->
                column.addView(topicCard(story), blockMargins(bottom = 12))
            }
        }
    }

    private fun topicCard(story: StorySnapshot): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = rounded(PAPER, 20)
            setPadding(dp(10), dp(10), dp(14), dp(10))
            isClickable = true
            isFocusable = true
            setOnClickListener { showTopic(story.clientStoryId) }
        }
        val image = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            decodeSampled(story.processedImagePath ?: story.photoPath, 240, 240)?.let(::setImageBitmap)
            background = rounded(SAGE_DARK, 14)
            clipToOutline = true
        }
        row.addView(image, LinearLayout.LayoutParams(dp(74), dp(74)))

        val text = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), 0, 0, 0)
        }
        text.addView(label(topicTitle(story), 17, INK, Typeface.DEFAULT_BOLD).apply { maxLines = 2 })
        text.addView(
            label(topicStatus(story), 13, statusColor(story.stage), Typeface.DEFAULT).apply {
                setPadding(0, dp(6), 0, 0)
                maxLines = 1
            }
        )
        row.addView(text, LinearLayout.LayoutParams(0, -2, 1f))
        return row
    }

    private fun showTopic(storyId: String) {
        val story = store.story(storyId) ?: run { showTopics(); return }
        setActiveStory(storyId)
        contentHost.removeAllViews()
        shownImagePath = null

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            contentDescription = "topic-scroll"
        }
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(14), dp(18), dp(30))
        }
        scroll.addView(column)
        contentHost.addView(scroll)

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(secondaryButton("‹ Темы") { showTopics() })
        topicTitleView = label(topicTitle(story), 20, INK, Typeface.DEFAULT_BOLD).apply {
            maxLines = 2
            setPadding(dp(10), 0, 0, 0)
        }
        header.addView(topicTitleView, LinearLayout.LayoutParams(0, -2, 1f))
        column.addView(header)

        topicStatusView = label("", 13, MUTED, Typeface.DEFAULT).apply {
            setPadding(0, dp(11), 0, dp(8))
            visibility = View.GONE
        }
        column.addView(topicStatusView)

        previewImage = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            background = rounded(SAGE_DARK, 22)
            clipToOutline = true
            contentDescription = "publication-image"
        }
        column.addView(previewImage, LinearLayout.LayoutParams(-1, dp(280)))

        literalBanner = label(
            "Дословная диктовка · ещё не применено",
            13,
            ACCENT,
            Typeface.DEFAULT_BOLD,
        ).apply {
            background = rounded(0xffffeee8.toInt(), 14)
            setPadding(dp(12), dp(9), dp(12), dp(9))
            visibility = View.GONE
        }
        column.addView(literalBanner, blockMargins(top = 12))

        previewText = label("", 18, INK, Typeface.DEFAULT).apply {
            setLineSpacing(dp(4).toFloat(), 1.08f)
            background = rounded(PAPER, 18)
            setPadding(dp(16), dp(16), dp(16), dp(16))
            contentDescription = "publication-preview"
        }
        column.addView(previewText, blockMargins(top = 14))

        lastChangeView = label("", 13, MUTED, Typeface.DEFAULT).apply {
            setPadding(dp(4), dp(9), dp(4), 0)
            visibility = View.GONE
        }
        column.addView(lastChangeView)

        val minorActions = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(12), 0, 0)
        }
        sourceButton = secondaryButton("Источники · 0") { showSources(storyId) }.apply {
            contentDescription = "sources"
        }
        minorActions.addView(sourceButton, LinearLayout.LayoutParams(0, dp(48), 1f))
        undoButton = secondaryButton("Отменить") {
            if (live.isActiveFor(storyId)) live.sendText("Верни предыдущую правку.")
        }.apply {
            contentDescription = "undo"
            visibility = View.GONE
        }
        minorActions.addView(
            undoButton,
            LinearLayout.LayoutParams(0, dp(48), 1f).apply { leftMargin = dp(8) },
        )
        column.addView(minorActions)

        publishButton = primaryButton("Опубликовать") {
            val current = store.story(storyId) ?: return@primaryButton
            if (!live.isActiveFor(storyId)) {
                Toast.makeText(this, "Включите Live, чтобы продолжить голосом", Toast.LENGTH_SHORT).show()
            } else if (current.stage == StoryStage.SCHEDULED) {
                live.sendText("Отмени текущую запланированную публикацию.")
            } else {
                live.sendText(
                    "Хочу опубликовать именно этот текущий вариант. " +
                        "Если не хватает канала, даты или времени — коротко спроси меня."
                )
            }
        }.apply { contentDescription = "publish-action" }
        column.addView(publishButton, blockMargins(top = 14))

        confirmationBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(0xfff1efe8.toInt(), 18)
            setPadding(dp(14), dp(14), dp(14), dp(14))
            visibility = View.GONE
            contentDescription = "publication-confirmation"
        }
        column.addView(confirmationBox, blockMargins(top = 12))

        buildDock(storyId)
        refreshTopicDetail()
        applyLiveState(live.snapshot())
    }

    private fun buildDock(storyId: String) {
        dock.removeAllViews()
        dock.visibility = View.VISIBLE
        val top = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        dockTopic = label("Тема · ${topicTitle(store.story(storyId))}", 12, MUTED, Typeface.DEFAULT).apply {
            maxLines = 1
        }
        top.addView(dockTopic, LinearLayout.LayoutParams(0, -2, 1f))
        micButton = Button(this).apply {
            text = "Микрофон"
            textSize = 15f
            isAllCaps = false
            setTextColor(PAPER)
            background = rounded(INK, 18)
            minHeight = 0
            minWidth = 0
            contentDescription = "live-mic"
            setOnClickListener { toggleLive(storyId) }
        }
        top.addView(micButton, LinearLayout.LayoutParams(dp(122), dp(44)))
        dock.addView(top)

        dockStatus = label("Микрофон выключен", 13, INK, Typeface.DEFAULT).apply {
            setPadding(0, dp(6), 0, 0)
            maxLines = 1
        }
        dock.addView(dockStatus)
    }

    private fun refreshSurface() {
        if (activeStoryId == null) showTopics() else refreshTopicDetail()
    }

    private fun refreshTopicDetail() {
        val id = activeStoryId ?: return
        val story = store.story(id) ?: run { showTopics(); return }

        topicTitleView?.text = topicTitle(story)
        dockTopic?.text = "Тема · ${topicTitle(story)}"

        val meaningful = story.stage in setOf(
            StoryStage.RESEARCHING,
            StoryStage.VISUAL_PROCESSING,
            StoryStage.SCHEDULING,
            StoryStage.SCHEDULED,
            StoryStage.PUBLISHED,
            StoryStage.NEEDS_REVIEW,
            StoryStage.VISUAL_BLOCKED,
        ) || !story.lastError.isNullOrBlank()
        topicStatusView?.apply {
            text = story.lastError?.takeIf { it.isNotBlank() } ?: topicStatus(story)
            setTextColor(if (!story.lastError.isNullOrBlank()) ACCENT else statusColor(story.stage))
            visibility = if (meaningful) View.VISIBLE else View.GONE
        }

        val imagePath = story.processedImagePath?.takeIf { File(it).isFile } ?: story.photoPath
        if (shownImagePath != imagePath) {
            shownImagePath = imagePath
            previewImage?.setImageBitmap(decodeSampled(imagePath, 1200, 1000))
        }

        previewText?.text = story.draftText?.takeIf { it.isNotBlank() }
            ?: "Расскажите голосом, что вы заметили и какой пост хотите получить."

        val projection = research.get(id)
        sourceButton?.text = "Источники · ${projection?.sourceCount ?: 0}"

        publishButton?.apply {
            val hasResult = !story.draftText.isNullOrBlank() &&
                (!story.processedImagePath.isNullOrBlank() || story.stage == StoryStage.SCHEDULED)
            visibility = if (hasResult) View.VISIBLE else View.GONE
            text = if (story.stage == StoryStage.SCHEDULED) "Отменить публикацию" else "Опубликовать"
        }
        applyLiveState(live.snapshot())
    }

    private fun applyLiveState(state: LiveUiState) {
        val id = activeStoryId ?: return
        if (state.storyId != null && state.storyId != id) {
            dockStatus?.text = "Live идёт в другой теме"
            micButton?.text = "Открыть"
            return
        }

        dockStatus?.text = state.error ?: state.status
        micButton?.text = if (state.active) "Стоп" else "Микрофон"
        literalBanner?.visibility = if (state.literalMode) View.VISIBLE else View.GONE

        val change = state.lastChange?.takeIf { it.isNotBlank() }
        lastChangeView?.apply {
            text = change.orEmpty()
            visibility = if (change == null) View.GONE else View.VISIBLE
        }
        undoButton?.visibility = if (state.active && change != null) View.VISIBLE else View.GONE

        confirmationBox?.apply {
            removeAllViews()
            val confirmation = state.confirmation
            if (confirmation == null) {
                visibility = View.GONE
            } else {
                visibility = View.VISIBLE
                addView(label("Проверь публикацию", 16, INK, Typeface.DEFAULT_BOLD))
                if (confirmation.destinations.isNotEmpty()) {
                    addView(
                        label(
                            confirmation.destinations.joinToString(", "),
                            13,
                            MUTED,
                            Typeface.DEFAULT,
                        ).apply { setPadding(0, dp(5), 0, 0) }
                    )
                }
                addView(
                    label(
                        "${confirmation.scheduledFor.orEmpty()} · ${confirmation.timezone.orEmpty()}",
                        13,
                        MUTED,
                        Typeface.DEFAULT,
                    ).apply { setPadding(0, dp(3), 0, 0) }
                )
                confirmation.text?.takeIf { it.isNotBlank() }?.let {
                    addView(
                        label(it, 14, INK, Typeface.DEFAULT).apply {
                            setPadding(0, dp(10), 0, dp(8))
                            maxLines = 6
                        }
                    )
                }
                addView(
                    primaryButton("Подтвердить") {
                        if (live.isActiveFor(id)) {
                            live.sendText("Подтверждаю именно показанную карточку публикации.")
                        }
                    }
                )
            }
        }
    }

    private fun toggleLive(storyId: String) {
        val current = live.snapshot()
        if (current.active) {
            if (current.storyId == storyId) {
                RecordingService.command(this, RecordingService.ACTION_FINISH)
            } else {
                current.storyId?.let(::showTopic)
            }
            return
        }
        if (!config.configured) {
            showSettings()
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingLiveStoryId = storyId
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_MIC)
            return
        }
        startLive(storyId)
    }

    private fun startLive(storyId: String) {
        pendingLiveStoryId = null
        live.start(storyId) { ready, error ->
            runOnUiThread {
                if (ready) {
                    RecordingService.start(this, storyId, RecordingKind.LIVE_ARCHIVE)
                } else if (!error.isNullOrBlank()) {
                    Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_MIC) return
        val storyId = pendingLiveStoryId
        pendingLiveStoryId = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && storyId != null) {
            startLive(storyId)
        } else {
            Toast.makeText(this, "Для Live нужен микрофон", Toast.LENGTH_LONG).show()
        }
    }

    @Suppress("DEPRECATION")
    private fun launchPhotoPicker() {
        val intent = if (Build.VERSION.SDK_INT >= 33) {
            Intent(MediaStore.ACTION_PICK_IMAGES).apply { type = "image/*" }
        } else {
            Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                type = "image/*"
                addCategory(Intent.CATEGORY_OPENABLE)
            }
        }
        startActivityForResult(intent, REQUEST_PHOTO)
    }

    @Deprecated("Small standalone MVP activity result path")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PHOTO || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        Thread {
            runCatching {
                val imported = PhotoImporter.import(this, uri)
                store.createStory(imported)
                imported.clientStoryId
            }.onSuccess { storyId ->
                runOnUiThread {
                    SyncScheduler.enqueue(this)
                    showTopic(storyId)
                }
            }.onFailure { exc ->
                runOnUiThread {
                    Toast.makeText(
                        this,
                        "Не удалось сохранить фото: ${exc.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }.start()
    }

    private fun showSources(storyId: String) {
        val facts = store.facts(storyId)
        val projection = research.get(storyId)
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(10))
        }
        val boxes = mutableListOf<Pair<FactSnapshot, CheckBox>>()

        facts.forEach { fact ->
            val box = CheckBox(this).apply {
                text = fact.text
                isChecked = fact.selected
                isEnabled = fact.evidenceSupported
                setTextColor(if (fact.evidenceSupported) INK else MUTED)
                textSize = 14f
            }
            boxes += fact to box
            wrap.addView(box)
        }
        projection?.sources?.takeIf { it.isNotEmpty() }?.let { sources ->
            wrap.addView(
                label("Источники", 15, INK, Typeface.DEFAULT_BOLD).apply {
                    setPadding(0, dp(14), 0, dp(4))
                }
            )
            sources.forEach { source ->
                wrap.addView(
                    label(
                        "${source.title}\n${source.url}",
                        12,
                        MUTED,
                        Typeface.DEFAULT,
                    ).apply {
                        autoLinkMask = Linkify.WEB_URLS
                        movementMethod = LinkMovementMethod.getInstance()
                        setPadding(0, dp(5), 0, dp(5))
                    }
                )
            }
        }

        val scroll = ScrollView(this).apply { addView(wrap) }
        AlertDialog.Builder(this)
            .setTitle("Факты и источники")
            .setView(scroll)
            .setPositiveButton("Применить") { _, _ ->
                val selected = boxes
                    .filter { (fact, box) -> fact.evidenceSupported && box.isChecked }
                    .map { it.first.factId }
                boxes.forEach { (fact, box) ->
                    store.setFactSelected(storyId, fact.factId, fact.evidenceSupported && box.isChecked)
                }
                store.enqueueOperation(
                    storyId,
                    "facts",
                    newRequestKey("facts-ui", "$storyId-${System.currentTimeMillis()}"),
                    gson.toJson(
                        mapOf(
                            "selected_fact_ids" to selected,
                            "preserve_draft" to true,
                        )
                    ),
                )
                SyncScheduler.enqueue(this)
                if (live.isActiveFor(storyId)) {
                    live.sendText(
                        "Я вручную изменил выбор фактов. Прочитай текущее состояние темы; " +
                            "не переписывай текст без отдельной просьбы."
                    )
                }
            }
            .setNegativeButton("Закрыть", null)
            .show()
    }

    private fun showSettings() {
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), 0, dp(20), 0)
        }
        val url = EditText(this).apply {
            hint = "https://street-story…"
            setText(config.backendUrl.orEmpty())
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        val token = EditText(this).apply {
            hint = if (config.deviceToken.isNullOrBlank()) {
                "Device token"
            } else {
                "Device token сохранён · пусто = не менять"
            }
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setSingleLine()
        }
        wrap.addView(url)
        wrap.addView(token)
        val dialog = AlertDialog.Builder(this)
            .setTitle("Street Story backend · DevCoveer")
            .setMessage("Только HTTPS. Provider credentials остаются на сервере.")
            .setView(wrap)
            .setPositiveButton("Сохранить") { _, _ ->
                val value = url.text.toString().trim().trimEnd('/')
                if (!value.startsWith("https://")) {
                    Toast.makeText(this, "Нужен HTTPS-адрес backend", Toast.LENGTH_LONG).show()
                    return@setPositiveButton
                }
                config.backendUrl = value
                if (token.text.toString().isNotBlank()) config.deviceToken = token.text.toString()
                if (!config.configured) {
                    Toast.makeText(this, "Нужен device token", Toast.LENGTH_LONG).show()
                } else {
                    SyncScheduler.enqueue(this)
                    Toast.makeText(this, "Синхронизация запущена", Toast.LENGTH_SHORT).show()
                }
                refreshSurface()
            }
            .setNegativeButton("Отмена", null)
            .create()
        dialog.show()
        dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
    }

    private fun setActiveStory(id: String?) {
        activeStoryId = id?.takeIf { store.story(it) != null }
        prefs.edit().apply {
            if (activeStoryId == null) remove("active_story_id")
            else putString("active_story_id", activeStoryId)
        }.apply()
    }

    private fun clearTopicRefs() {
        topicTitleView = null
        topicStatusView = null
        previewImage = null
        previewText = null
        literalBanner = null
        lastChangeView = null
        sourceButton = null
        undoButton = null
        publishButton = null
        confirmationBox = null
        dockTopic = null
        dockStatus = null
        micButton = null
        shownImagePath = null
    }

    private fun topicTitle(story: StorySnapshot?): String {
        if (story == null) return "Новая тема"
        story.placeName?.trim()?.takeIf { it.isNotBlank() }?.let { return it.take(80) }
        story.summary?.trim()?.takeIf { it.isNotBlank() }?.let {
            return it.lineSequence().first().take(80)
        }
        val date = Instant.ofEpochMilli(story.createdAt)
            .atZone(ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("d MMM", Locale("ru")))
        return "Тема · $date"
    }

    private fun topicStatus(story: StorySnapshot): String = when (story.stage) {
        StoryStage.SCHEDULED -> "Запланировано · ${story.scheduledFor.orEmpty()}"
        StoryStage.PUBLISHED -> "Опубликовано"
        StoryStage.NEEDS_REVIEW, StoryStage.VISUAL_BLOCKED -> "Нужно внимание"
        StoryStage.RESEARCHING -> "Ищем факты"
        StoryStage.VISUAL_PROCESSING -> "Готовим изображение"
        StoryStage.SCHEDULING -> "Планируем публикацию"
        else -> "В работе"
    }

    private fun statusColor(stage: String): Int =
        if (stage in setOf(StoryStage.NEEDS_REVIEW, StoryStage.VISUAL_BLOCKED)) ACCENT else MUTED

    private fun label(textValue: String, size: Int, color: Int, face: Typeface): TextView =
        TextView(this).apply {
            text = textValue
            textSize = size.toFloat()
            setTextColor(color)
            typeface = face
            setLineSpacing(dp(2).toFloat(), 1.03f)
        }

    private fun primaryButton(textValue: String, action: () -> Unit): Button =
        Button(this).apply {
            text = textValue
            textSize = 15f
            isAllCaps = false
            setTextColor(PAPER)
            typeface = Typeface.DEFAULT_BOLD
            background = rounded(INK, 18)
            minHeight = 0
            setPadding(dp(16), 0, dp(16), 0)
            setOnClickListener { action() }
            layoutParams = LinearLayout.LayoutParams(-1, dp(52))
        }

    private fun secondaryButton(textValue: String, action: () -> Unit): Button =
        Button(this).apply {
            text = textValue
            textSize = 13f
            isAllCaps = false
            setTextColor(INK)
            background = rounded(0xffe9e6df.toInt(), 16)
            minHeight = 0
            minWidth = 0
            setPadding(dp(12), 0, dp(12), 0)
            setOnClickListener { action() }
        }

    private fun rounded(color: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radiusDp).toFloat()
    }

    private fun blockMargins(top: Int = 0, bottom: Int = 0) =
        LinearLayout.LayoutParams(-1, -2).apply {
            topMargin = dp(top)
            bottomMargin = dp(bottom)
        }

    private fun decodeSampled(path: String, targetWidth: Int, targetHeight: Int): Bitmap? =
        runCatching {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(path, bounds)
            var sample = 1
            while (
                bounds.outWidth / sample > targetWidth * 2 ||
                bounds.outHeight / sample > targetHeight * 2
            ) sample *= 2
            BitmapFactory.decodeFile(
                path,
                BitmapFactory.Options().apply { inSampleSize = sample.coerceAtLeast(1) },
            )
        }.getOrNull()

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQUEST_PHOTO = 710
        private const val REQUEST_MIC = 711
        private const val SAGE = 0xffe8ece4.toInt()
        private const val SAGE_DARK = 0xffd7ddd1.toInt()
        private const val PAPER = 0xfffffdf8.toInt()
        private const val INK = 0xff242822.toInt()
        private const val MUTED = 0xff6f746d.toInt()
        private const val ACCENT = 0xffad4d38.toInt()
    }
}
