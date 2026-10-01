package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.PropertyValuesHolder
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.ColorStateList
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
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
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
    private var pendingPhotoRecoveryStoryId: String? = null
    private var photoRecoveryButton: Button? = null
    private var identityLinkView: TextView? = null
    private var pendingLiveAutoIdentity = false
    private var autoIdentityLiveStartingStoryId: String? = null
    private var receiverRegistered = false
    private var pendingUpdate: UpdateInfo? = null
    private var waitingForInstallPermission = false
    private var updateCheckInFlight = false
    private var updateDownloadInFlight = false

    private var topicTitleView: TextView? = null
    private var topicStatusView: TextView? = null
    private var previewImage: ImageView? = null
    private var previewText: TextView? = null
    private var literalBanner: TextView? = null
    private var lastChangeView: TextView? = null
    private var chatBox: LinearLayout? = null
    private var chatMessages: LinearLayout? = null
    private var chatStatus: TextView? = null
    private var sourceButton: Button? = null
    private var undoButton: Button? = null
    private var publishButton: Button? = null
    private var confirmationBox: LinearLayout? = null
    private var dockTopic: TextView? = null
    private var dockStatus: TextView? = null
    private var micButton: ImageButton? = null
    private var micPulse: ObjectAnimator? = null
    private var shownImagePath: String? = null
    private var topicScroll: ScrollView? = null
    private var previewExpanded = true
    private var lastDialogueMessageCount = 0

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
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = SAGE
        window.navigationBarColor = SAGE
        pendingPhotoRecoveryStoryId = savedInstanceState?.getString("photo_recovery_story")
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
        resumeUpdateAfterPermission()
        maybeCheckForUpdate()
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
        outState.putString("photo_recovery_story", pendingPhotoRecoveryStoryId)
        super.onSaveInstanceState(outState)
    }

    @Deprecated("Single-activity product navigation")
    override fun onBackPressed() {
        if (activeStoryId != null) showTopics() else super.onBackPressed()
    }

    private fun buildChrome() {
        root = FrameLayout(this).apply { setBackgroundColor(SAGE) }
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(0, bars.top, 0, bars.bottom)
            insets
        }
        contentHost = FrameLayout(this)
        root.addView(
            contentHost,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ).apply { bottomMargin = 0 },
        )
        dock = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            background = null
            elevation = 0f
            visibility = View.GONE
        }
        root.addView(
            dock,
            FrameLayout.LayoutParams(dp(96), dp(96)).apply {
                gravity = Gravity.END or Gravity.BOTTOM
                rightMargin = dp(14)
                bottomMargin = dp(10)
            },
        )
        setContentView(root)
        ViewCompat.requestApplyInsets(root)
    }

    private fun showTopics() {
        setActiveStory(null)
        clearTopicRefs()
        dock.visibility = View.GONE
        (contentHost.layoutParams as? FrameLayout.LayoutParams)?.let {
            it.bottomMargin = 0
            contentHost.layoutParams = it
        }
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
            ImagePreviewDecoder.decode(story.processedImagePath ?: story.photoPath, 240, 240)?.let(::setImageBitmap)
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

        val delete = ImageButton(this).apply {
            setImageResource(R.drawable.ic_delete)
            imageTintList = ColorStateList.valueOf(MUTED)
            background = null
            setPadding(dp(10), dp(10), dp(10), dp(10))
            contentDescription = "delete-topic"
            setOnClickListener { confirmDeleteTopic(story) }
        }
        row.addView(delete, LinearLayout.LayoutParams(dp(44), dp(44)))
        return row
    }

    private fun showTopic(storyId: String) {
        val story = store.story(storyId) ?: run { showTopics(); return }
        setActiveStory(storyId)
        contentHost.removeAllViews()
        shownImagePath = null
        previewExpanded = true
        lastDialogueMessageCount = 0

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            contentDescription = "topic-scroll"
            setOnScrollChangeListener { _, _, scrollY, _, oldScrollY ->
                if (scrollY < oldScrollY) {
                    setPreviewCompact(false)
                } else if (scrollY > oldScrollY && lastDialogueMessageCount > 0) {
                    setPreviewCompact(true)
                }
            }
        }
        topicScroll = scroll
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(14), dp(18), dp(118))
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
        photoRecoveryButton = secondaryButton("Прочитать GPS из оригинала фото") {
            pendingPhotoRecoveryStoryId = story.clientStoryId
            stopLiveForOwner(story.clientStoryId)
            launchPhotoPicker()
        }.apply { visibility = View.GONE; contentDescription = "recover-photo-gps" }
        column.addView(photoRecoveryButton)
        identityLinkView = label("", 14, ACCENT, Typeface.DEFAULT).apply {
            visibility = View.GONE
            setPadding(0, dp(4), 0, dp(8))
            contentDescription = "identified-object-source"
        }
        column.addView(identityLinkView)

        previewImage = ImageView(this).apply {
            adjustViewBounds = true
            scaleType = ImageView.ScaleType.FIT_CENTER
            background = rounded(SAGE_DARK, 22)
            clipToOutline = true
            contentDescription = "publication-image"
            setOnClickListener { setPreviewCompact(previewExpanded) }
        }
        column.addView(previewImage, LinearLayout.LayoutParams(-1, ViewGroup.LayoutParams.WRAP_CONTENT))

        chatBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(0xfff4f2ec.toInt(), 18)
            setPadding(dp(12), dp(12), dp(12), dp(11))
            contentDescription = "live-chat"
        }
        chatBox?.addView(label("Разговор с Мирой", 14, INK, Typeface.DEFAULT_BOLD))
        chatMessages = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, 0)
        }
        chatBox?.addView(chatMessages)
        chatStatus = label("Микрофон выключен", 13, MUTED, Typeface.DEFAULT).apply {
            setPadding(dp(2), dp(7), dp(2), 0)
            contentDescription = "live-chat-status"
        }
        chatBox?.addView(chatStatus)
        column.addView(chatBox, blockMargins(top = 12))

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
            visibility = View.GONE
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
        (contentHost.layoutParams as? FrameLayout.LayoutParams)?.let {
            it.bottomMargin = 0
            contentHost.layoutParams = it
        }
        micButton = ImageButton(this).apply {
            setImageResource(R.drawable.ic_mic)
            imageTintList = ColorStateList.valueOf(PAPER)
            background = oval(INK)
            elevation = dp(8).toFloat()
            setPadding(dp(21), dp(21), dp(21), dp(21))
            contentDescription = "live-mic"
            setOnClickListener { toggleLive(storyId) }
        }
        dock.addView(micButton, LinearLayout.LayoutParams(dp(78), dp(78)))
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
            StoryStage.IDENTIFYING,
            StoryStage.IDENTITY_READY,
            StoryStage.RESEARCHING,
            StoryStage.VISUAL_PROCESSING,
            StoryStage.SCHEDULING,
            StoryStage.SCHEDULED,
            StoryStage.PUBLISHED,
            StoryStage.NEEDS_REVIEW,
            StoryStage.VISUAL_BLOCKED,
        ) || !story.lastError.isNullOrBlank()
        topicStatusView?.apply {
            val ownerError = ownerVisibleStoryError(story.lastError)
            text = ownerError ?: topicStatus(story)
            setTextColor(if (ownerError != null) ACCENT else statusColor(story.stage))
            visibility = if (meaningful) View.VISIBLE else View.GONE
        }

        val imagePath = story.processedImagePath?.takeIf { File(it).isFile } ?: story.photoPath
        if (shownImagePath != imagePath) {
            shownImagePath = imagePath
            previewImage?.setImageBitmap(ImagePreviewDecoder.decode(imagePath, 1200, 1000))
        }

        previewText?.apply {
            val draft = story.draftText?.takeIf { it.isNotBlank() }
            text = draft.orEmpty()
            visibility = if (draft == null) View.GONE else View.VISIBLE
        }

        photoRecoveryButton?.visibility = if ((story.latitude == null || story.longitude == null) && story.stage == StoryStage.NEEDS_REVIEW) View.VISIBLE else View.GONE
        val projection = research.get(id)
        val identified = projection?.candidates?.firstOrNull { it.candidateId == projection.candidateId }
        identityLinkView?.apply {
            val accepted = projection?.identityStatus in setOf("match", "owner_confirmed")
            text = if (accepted && identified != null) "${identified.name} ↗" else ""
            visibility = if (text.isNotBlank()) View.VISIBLE else View.GONE
            setOnClickListener {
                val uri = identified?.url?.let(Uri::parse)
                if (uri?.scheme == "https" && (uri.host == "www.openstreetmap.org" || uri.host?.endsWith(".wikipedia.org") == true))
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
            }
        }
        sourceButton?.apply {
            val count = projection?.sourceCount ?: 0
            text = "Источники · $count"
            visibility = if (count > 0) View.VISIBLE else View.GONE
        }
        maybeAutoStartIdentityLive(story, projection)

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
            chatStatus?.text = "• Live идёт в другой теме"
            stopMicPulse()
            return
        }

        chatStatus?.apply {
            text = if (state.error.isNullOrBlank()) "• ${state.status}" else "⚠ ${state.error}"
            setTextColor(if (state.error.isNullOrBlank()) MUTED else ACCENT)
        }
        renderLiveMessages(state.messages)
        val dialogueCount = state.messages.count {
            it.role == LiveRole.USER || it.role == LiveRole.ASSISTANT
        }
        if (dialogueCount > lastDialogueMessageCount) {
            lastDialogueMessageCount = dialogueCount
            setPreviewCompact(true)
            chatBox?.post {
                val scroll = topicScroll ?: return@post
                val target = ((chatBox?.top ?: 0) - dp(10)).coerceAtLeast(0)
                scroll.smoothScrollTo(0, target)
            }
        } else {
            lastDialogueMessageCount = maxOf(lastDialogueMessageCount, dialogueCount)
        }
        micButton?.apply {
            background = oval(if (state.active || state.connecting) ACCENT else INK)
            contentDescription = if (state.active) "live-stop" else "live-mic"
        }
        updateMicPulse(state.inputActive)
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
        if (current.active || current.connecting || pendingLiveStoryId == storyId) {
            if (current.storyId == storyId || pendingLiveStoryId == storyId) {
                stopLiveForOwner(storyId)
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
            pendingLiveAutoIdentity = false
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_MIC)
            return
        }
        startLive(storyId)
    }

    private fun stopLiveForOwner(storyId: String) {
        // Stop does not depend on an archive, successful handshake or server reply.
        prefs.edit().putBoolean("identity_live_attempted:$storyId", true).apply()
        pendingLiveStoryId = null
        pendingLiveAutoIdentity = false
        autoIdentityLiveStartingStoryId = null
        live.stopLocal()
        RecordingService.command(this, RecordingService.ACTION_TRANSPORT_FINISH)
    }

    private fun startLive(storyId: String, autoIdentity: Boolean = false) {
        pendingLiveStoryId = null
        if (!autoIdentity) pendingLiveAutoIdentity = false
        live.start(storyId) { ready, error ->
            runOnUiThread {
                if (autoIdentity) autoIdentityLiveStartingStoryId = null
                if (ready && live.isActiveFor(storyId) && !live.snapshot().connecting) {
                    RecordingService.start(this, storyId, RecordingKind.LIVE_ARCHIVE)
                } else if (!error.isNullOrBlank()) {
                    Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun maybeAutoStartIdentityLive(
        story: StorySnapshot,
        projection: ResearchProjectionSnapshot?,
    ) {
        val status = projection?.identityStatus ?: return
        if (status !in setOf("match", "owner_confirmed", "uncertain", "mismatch")) return
        if (status !in setOf("match", "owner_confirmed") && projection.candidates.isEmpty()) return
        if (!config.configured) return

        val current = live.snapshot()
        if (current.active || current.connecting) return
        if (autoIdentityLiveStartingStoryId != null) return

        val attemptKey = "identity_live_attempted:${story.clientStoryId}"
        if (prefs.getBoolean(attemptKey, false)) return
        prefs.edit().putBoolean(attemptKey, true).apply()
        autoIdentityLiveStartingStoryId = story.clientStoryId

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingLiveStoryId = story.clientStoryId
            pendingLiveAutoIdentity = true
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_MIC)
            return
        }
        startLive(story.clientStoryId, autoIdentity = true)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PHOTO_LOCATION) {
            openOriginalPhotoPicker()
            return
        }
        if (requestCode != REQUEST_MIC) return
        val storyId = pendingLiveStoryId
        val autoIdentity = pendingLiveAutoIdentity
        pendingLiveStoryId = null
        pendingLiveAutoIdentity = false
        if (storyId == null) return
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && storyId != null) {
            startLive(storyId, autoIdentity = autoIdentity)
        } else {
            if (autoIdentity) autoIdentityLiveStartingStoryId = null
            Toast.makeText(this, "Для Live нужен микрофон", Toast.LENGTH_LONG).show()
        }
    }

    private fun launchPhotoPicker() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            openOriginalPhotoPicker()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Геометки выбранного фото")
            .setMessage("Для поиска объекта нужны координаты внутри исходного фото. Разрешение относится к метаданным выбранного файла, не к вашей текущей геопозиции.")
            .setPositiveButton("Продолжить") { _, _ ->
                requestPermissions(arrayOf(Manifest.permission.ACCESS_MEDIA_LOCATION), REQUEST_PHOTO_LOCATION)
            }
            .setNegativeButton("Без геометок") { _, _ -> openOriginalPhotoPicker() }
            .show()
    }

    @Suppress("DEPRECATION")
    private fun openOriginalPhotoPicker() {
        // A single user-selected original, without access to the whole gallery.
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            type = "image/*"
            addCategory(Intent.CATEGORY_OPENABLE)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }, REQUEST_PHOTO)
    }

    @Deprecated("Small standalone MVP activity result path")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PHOTO) return
        val recoveryId = pendingPhotoRecoveryStoryId
        pendingPhotoRecoveryStoryId = null
        if (resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        Thread {
            var imported: ImportedPhoto? = null
            runCatching {
                val photo = PhotoImporter.import(this, uri)
                imported = photo
                if (recoveryId != null) {
                    val existing = requireNotNull(store.story(recoveryId)) { "Тема уже удалена" }
                    val server = requireNotNull(existing.serverStoryId) { "Дождитесь синхронизации темы" }
                    val api = ApiClient(requireNotNull(config.backendUrl), requireNotNull(config.deviceToken))
                    val restored = api.recoverPhotoLocation(server, existing.photoSha256, photo)
                    check(restored.id == server) { "Backend вернул другую тему" }
                    PhotoImportTelemetry.pending(this, photo.clientStoryId)?.let { payload ->
                        runCatching { api.photoDiagnostic(server, payload) }
                    }
                    recoveryId
                } else {
                    store.createStory(photo)
                    photo.clientStoryId
                }
            }.onSuccess { storyId ->
                runOnUiThread {
                    SyncScheduler.enqueue(this)
                    showTopic(storyId)
                }
            }.onFailure { exc ->
                runOnUiThread { Toast.makeText(this, "Не удалось прочитать оригинал: ${exc.message}", Toast.LENGTH_LONG).show() }
            }
            if (recoveryId != null) imported?.let { photo ->
                File(photo.path).delete()
                PhotoImportTelemetry.pending(this, photo.clientStoryId)?.let { PhotoImportTelemetry.acknowledge(this, photo.clientStoryId, it) }
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

    private fun maybeCheckForUpdate() {
        val now = System.currentTimeMillis()
        val last = prefs.getLong("update_checked_at", 0L)
        if (now - last < UPDATE_INTERVAL_MS) return
        checkForUpdate(showUpToDate = false)
    }

    private fun checkForUpdate(showUpToDate: Boolean) {
        if (updateCheckInFlight) return
        updateCheckInFlight = true
        prefs.edit().putLong("update_checked_at", System.currentTimeMillis()).apply()
        AppUpdater.checkLatest(BuildConfig.VERSION_CODE) { result ->
            runOnUiThread {
                updateCheckInFlight = false
                result.onSuccess { info ->
                    if (info == null) {
                        if (showUpToDate) {
                            Toast.makeText(
                                this,
                                "Установлена актуальная версия ${BuildConfig.VERSION_NAME}",
                                Toast.LENGTH_SHORT,
                            ).show()
                        }
                    } else {
                        AlertDialog.Builder(this)
                            .setTitle("Есть новая версия")
                            .setMessage(
                                "Street Story ${info.versionName} готова. " +
                                    "Обновление скачивается из официального GitHub Release проекта."
                            )
                            .setPositiveButton("Обновить") { _, _ -> beginUpdate(info) }
                            .setNegativeButton("Позже", null)
                            .show()
                    }
                }.onFailure { exc ->
                    if (showUpToDate) {
                        Toast.makeText(
                            this,
                            "Не удалось проверить обновление: ${exc.message ?: "ошибка сети"}",
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            }
        }
    }

    private fun beginUpdate(info: UpdateInfo) {
        pendingUpdate = info
        if (!AppUpdater.canInstallPackages(this)) {
            waitingForInstallPermission = true
            Toast.makeText(
                this,
                "Разрешите Street Story устанавливать обновления из GitHub",
                Toast.LENGTH_LONG,
            ).show()
            startActivity(AppUpdater.installPermissionIntent(this))
            return
        }
        downloadAndInstall(info)
    }

    private fun resumeUpdateAfterPermission() {
        if (!waitingForInstallPermission) return
        waitingForInstallPermission = false
        val info = pendingUpdate ?: return
        if (AppUpdater.canInstallPackages(this)) {
            downloadAndInstall(info)
        } else {
            Toast.makeText(
                this,
                "Разрешение не выдано. Обновление можно повторить в Настройках.",
                Toast.LENGTH_LONG,
            ).show()
        }
    }

    private fun downloadAndInstall(info: UpdateInfo) {
        if (updateDownloadInFlight) return
        updateDownloadInFlight = true
        Toast.makeText(this, "Скачиваю Street Story ${info.versionName}…", Toast.LENGTH_SHORT).show()
        AppUpdater.download(this, info) { result ->
            runOnUiThread {
                updateDownloadInFlight = false
                result.onSuccess { apk ->
                    pendingUpdate = null
                    runCatching { AppUpdater.install(this, apk) }
                        .onFailure { exc ->
                            Toast.makeText(
                                this,
                                "Не удалось открыть установщик: ${exc.message ?: "ошибка"}",
                                Toast.LENGTH_LONG,
                            ).show()
                        }
                }.onFailure { exc ->
                    Toast.makeText(
                        this,
                        "Не удалось скачать обновление: ${exc.message ?: "ошибка сети"}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    private fun showSettings() {
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), 0, dp(20), 0)
        }
        wrap.addView(
            label(
                "Версия ${BuildConfig.VERSION_NAME} · ${BuildConfig.SOURCE_SHA.take(12)}",
                13,
                MUTED,
                Typeface.DEFAULT,
            ).apply { setPadding(0, 0, 0, dp(8)) }
        )
        wrap.addView(
            secondaryButton("Проверить обновление") { checkForUpdate(showUpToDate = true) },
            LinearLayout.LayoutParams(-1, dp(46)).apply { bottomMargin = dp(10) },
        )
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
        stopMicPulse()
        topicTitleView = null
        topicStatusView = null
        previewImage = null
        previewText = null
        literalBanner = null
        lastChangeView = null
        chatBox = null
        chatMessages = null
        chatStatus = null
        sourceButton = null
        undoButton = null
        publishButton = null
        confirmationBox = null
        dockTopic = null
        dockStatus = null
        micButton = null
        photoRecoveryButton = null
        identityLinkView = null
        shownImagePath = null
        topicScroll = null
        previewExpanded = true
        lastDialogueMessageCount = 0
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

    private fun ownerVisibleStoryError(value: String?): String? {
        val text = value?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        val lower = text.lowercase(Locale.ROOT)
        return when {
            lower.contains("resource_token_budget") ->
                "Голосовой лимит временно исчерпан. Подождите около минуты."
            lower.contains("429") || lower.contains("503") ||
                lower.contains("resource_capacity") ||
                lower.contains("live_busy") || lower.contains("provider_quota") ->
                "Мира сейчас занята. Результат сохранён — попробуйте ещё раз через несколько секунд."
            lower.contains("timeout") || lower.contains("timed out") || lower.contains("connection") ||
                lower.contains("network") || lower.contains("socket") ->
                "Связь прервалась. Локальные данные сохранены."
            Regex("^[A-Z0-9_.:-]{4,}$").matches(text) || lower.startsWith("http_") ->
                "Не удалось завершить действие. Диагностика сохранена."
            else -> text.take(220)
        }
    }

    private fun topicStatus(story: StorySnapshot): String = when (story.stage) {
        StoryStage.SCHEDULED -> "Запланировано · ${story.scheduledFor.orEmpty()}"
        StoryStage.PUBLISHED -> "Опубликовано"
        StoryStage.NEEDS_REVIEW, StoryStage.VISUAL_BLOCKED -> "Нужно внимание"
        StoryStage.IDENTIFYING -> "Определяю объект"
        StoryStage.IDENTITY_READY -> "Объект определён"
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

    private fun renderLiveMessages(messages: List<LiveChatMessage>) {
        val host = chatMessages ?: return
        host.removeAllViews()
        val identity = activeStoryId?.let(research::get)
        val identityMessage = identity
            ?.takeIf {
                it.identityStatus in setOf("match", "owner_confirmed") &&
                    !it.candidateName.isNullOrBlank()
            }
            ?.candidateName
            ?.let { LiveChatMessage(LiveRole.ASSISTANT, "Я нашла, что изображено на фото: это $it.") }
        val visible = buildList {
            identityMessage?.let(::add)
            addAll(messages.takeLast(18))
        }
        visible.forEach { message ->
            val row = LinearLayout(this).apply {
                gravity = if (message.role == LiveRole.USER) Gravity.END else Gravity.START
            }
            val title = when (message.role) {
                LiveRole.USER -> "Вы"
                LiveRole.ASSISTANT -> "Мира"
                else -> "Система"
            }
            val bubble = label("$title\n${message.text}", 14, INK, Typeface.DEFAULT).apply {
                background = rounded(
                    if (message.role == LiveRole.USER) 0xffe2e8dd.toInt() else PAPER,
                    14,
                )
                setPadding(dp(11), dp(8), dp(11), dp(8))
                maxWidth = dp(310)
            }
            row.addView(bubble)
            host.addView(row, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(6) })
        }
    }

    private fun setPreviewCompact(compact: Boolean) {
        val image = previewImage ?: return
        val expanded = !compact
        if (previewExpanded == expanded && image.layoutParams != null) return
        previewExpanded = expanded
        image.adjustViewBounds = true
        image.scaleType = ImageView.ScaleType.FIT_CENTER
        val params = image.layoutParams as? LinearLayout.LayoutParams ?: return
        params.width = if (expanded) ViewGroup.LayoutParams.MATCH_PARENT else dp(190)
        params.height = ViewGroup.LayoutParams.WRAP_CONTENT
        params.gravity = Gravity.CENTER_HORIZONTAL
        image.layoutParams = params
        image.requestLayout()
    }

    private fun confirmDeleteTopic(story: StorySnapshot) {
        val activeRecording = store.activeVoiceSession()
        if (live.isActiveFor(story.clientStoryId) || activeRecording?.storyId == story.clientStoryId) {
            Toast.makeText(this, "Сначала остановите Live или запись этой темы.", Toast.LENGTH_LONG).show()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Удалить тему?")
            .setMessage("Фото, локальная история этой темы и серверная запись будут удалены.")
            .setPositiveButton("Удалить") { _, _ -> deleteTopic(story) }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun deleteTopic(story: StorySnapshot) {
        Thread {
            runCatching {
                val serverId = story.serverStoryId
                if (!serverId.isNullOrBlank()) {
                    val base = config.backendUrl
                    val token = config.deviceToken
                    check(!base.isNullOrBlank() && !token.isNullOrBlank()) {
                        "Backend не настроен — серверную тему нельзя безопасно удалить."
                    }
                    try {
                        val receipt = ApiClient(base, token).deleteStory(serverId)
                        check(receipt.ok && receipt.storyId == serverId) {
                            "Backend не подтвердил удаление темы."
                        }
                    } catch (exc: ApiException) {
                        if (exc.status != 404) throw exc
                    }
                }
                store.deleteStory(story.clientStoryId)
                research.clear(story.clientStoryId)
                val feed = FeedProjectionStore(this)
                try {
                    feed.clear(story.clientStoryId)
                } finally {
                    feed.close()
                }
            }.onSuccess {
                runOnUiThread {
                    if (activeStoryId == story.clientStoryId) setActiveStory(null)
                    showTopics()
                    Toast.makeText(this, "Тема удалена", Toast.LENGTH_SHORT).show()
                }
            }.onFailure { exc ->
                runOnUiThread {
                    Toast.makeText(
                        this,
                        "Не удалось удалить тему: ${exc.message ?: "ошибка"}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }.start()
    }

    private fun updateMicPulse(active: Boolean) {
        val button = micButton ?: return
        if (!active) {
            stopMicPulse()
            button.scaleX = 1f
            button.scaleY = 1f
            button.alpha = 1f
            return
        }
        if (micPulse?.isRunning == true) return
        micPulse = ObjectAnimator.ofPropertyValuesHolder(
            button,
            PropertyValuesHolder.ofFloat(View.SCALE_X, 1f, 1.10f),
            PropertyValuesHolder.ofFloat(View.SCALE_Y, 1f, 1.10f),
            PropertyValuesHolder.ofFloat(View.ALPHA, 1f, 0.72f),
        ).apply {
            duration = 520
            repeatMode = ObjectAnimator.REVERSE
            repeatCount = ObjectAnimator.INFINITE
            start()
        }
    }

    private fun stopMicPulse() {
        micPulse?.cancel()
        micPulse = null
    }

    private fun oval(color: Int) = GradientDrawable().apply {
        shape = GradientDrawable.OVAL
        setColor(color)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQUEST_PHOTO_LOCATION = 714
        private const val REQUEST_PHOTO = 710
        private const val REQUEST_MIC = 711
        private const val UPDATE_INTERVAL_MS = 6L * 60L * 60L * 1000L
        private const val SAGE = 0xffe8ece4.toInt()
        private const val SAGE_DARK = 0xffd7ddd1.toInt()
        private const val PAPER = 0xfffffdf8.toInt()
        private const val INK = 0xff242822.toInt()
        private const val MUTED = 0xff6f746d.toInt()
        private const val ACCENT = 0xffad4d38.toInt()
    }
}
