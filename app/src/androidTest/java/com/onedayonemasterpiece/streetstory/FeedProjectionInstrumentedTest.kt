package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.widget.CheckBox
import android.widget.ImageView
import android.widget.ScrollView
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.ByteArrayInputStream
import kotlin.math.abs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class FeedProjectionInstrumentedTest {
    @Test fun cleanedVoiceProjectionPersistsAcrossStoreRestart() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        context.deleteDatabase("street-story-feed.db")
        val wire = VoiceMessageWire().apply {
            sessionId = "voice-cleaned-persist"
            kind = RecordingKind.REFINEMENT
            rawTranscript = "э-э, я я хочу уточнить Дом Советов"
            displayText = "Я хочу уточнить Дом Советов"
            startedAt = "2026-09-08T12:00:00+02:00"
            endedAt = "2026-09-08T12:01:00+02:00"
        }
        FeedProjectionStore(context).use { projection ->
            projection.replaceVoiceMessages("story-cleaned-persist", listOf(wire))
            val saved = projection.voiceMessages("story-cleaned-persist").single()
            assertEquals(wire.rawTranscript, saved.rawTranscript)
            assertEquals(wire.displayText, saved.displayText)
        }
        FeedProjectionStore(context).use { projection ->
            val saved = projection.voiceMessages("story-cleaned-persist").single()
            assertEquals("Я хочу уточнить Дом Советов", saved.displayText)
        }
    }

    @Test fun feedKeepsLatestTenInteractiveThreadsAndFixedRecordingControls() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val store = AppGraph.store(context)
        store.activeVoiceSession()?.let { store.discardVoiceSession(it.sessionId) }
        val before = store.stories().size
        val created = mutableListOf<StorySnapshot>()
        repeat(12) { index ->
            val photo = PhotoImporter.importStream(
                context,
                ByteArrayInputStream("feed-photo-$index".toByteArray()),
                "image/jpeg",
                "feed-ui-${System.nanoTime()}-$index",
            )
            created += store.createStory(photo)
            Thread.sleep(2)
        }
        val target = created.last()
        val processing = created[created.lastIndex - 1]
        val generated = created[created.lastIndex - 2]
        store.setStage(processing.clientStoryId, StoryStage.RESEARCHING)
        store.setStage(generated.clientStoryId, StoryStage.READY_TO_PUBLISH)
        store.setProcessedImagePath(generated.clientStoryId, generated.photoPath)
        store.setStage(target.clientStoryId, StoryStage.REVIEW)
        store.setDraftText(
            target.clientStoryId,
            "Первая строка\nВторая строка\nТретья строка\nЧетвёртая строка\nПятая строка",
        )
        store.replaceFacts(
            target.clientStoryId,
            listOf(
                FactSnapshot(
                    "fact-supported",
                    "Подтверждённый факт",
                    .9,
                    true,
                    true,
                    "[{\"url\":\"https://example.test/source\"}]",
                ),
                FactSnapshot("fact-unsupported", "Неподтверждённый факт", .2, false, false, "[]"),
            ),
        )
        store.replaceDestinations(
            target.clientStoryId,
            listOf(
                DestinationSnapshot("tg-main", "Полюбить Калининград", "telegram", "supported", true),
                DestinationSnapshot("vk-main", "Полюбить Калининград", "vk", "needs_review", true),
            ),
        )
        val voice1 = VoiceMessageWire().apply {
            sessionId = "voice-feed-clean-1"
            kind = RecordingKind.INITIAL
            rawTranscript = "э-э, я я хочу про Дом Советов"
            displayText = "Я хочу про Дом Советов"
        }
        val voice2 = VoiceMessageWire().apply {
            sessionId = "voice-feed-clean-2"
            kind = RecordingKind.REFINEMENT
            rawTranscript = "а-а, ещё ещё про площадь"
            displayText = "Ещё про площадь"
        }
        FeedProjectionStore(context).use {
            it.replaceVoiceMessages(target.clientStoryId, listOf(voice1, voice2))
        }
        context.getSharedPreferences("street_story_ui", Context.MODE_PRIVATE)
            .edit()
            .putString("active_story_id", target.clientStoryId)
            .commit()
        val active = store.createVoiceSession(target.clientStoryId, RecordingKind.REFINEMENT, "ui-test")
        var scrollBeforeRecreate = 0

        try {
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    var root = activity.findViewById<android.view.View>(android.R.id.content)
                    assertNotNull(findByDescription(root, "fixed-recording-dock"))
                    assertVisible(findByDescription(root, "recording-mic-pulse"))
                    assertVisible(findByDescription(root, "record-pause"))
                    assertVisible(findByDescription(root, "record-finish"))
                    assertVisible(findByDescription(root, "new-story-action"))

                    val threads = collectByPrefix(root, "story-thread-")
                    assertEquals(10, threads.size)
                    assertTrue(store.stories().size >= before + 12)
                    val hidden = FeedModel.latest(store.stories()).let { store.stories().drop(it.size) }
                    assertTrue(hidden.isNotEmpty())
                    assertTrue(
                        hidden.all { story ->
                            findByDescription(root, "story-thread-${story.clientStoryId}") == null
                        },
                    )

                    val targetThread = requireNotNull(findByDescription(root, "story-thread-${target.clientStoryId}"))
                    val processingThread = requireNotNull(findByDescription(root, "story-thread-${processing.clientStoryId}"))
                    val generatedThread = requireNotNull(findByDescription(root, "story-thread-${generated.clientStoryId}"))
                    assertTrue(countType(targetThread, ImageView::class.java) >= 1)
                    assertTrue(countType(generatedThread, ImageView::class.java) >= 2)
                    assertTrue(collectText(processingThread).any { it.contains("Research") })

                    assertNotNull(findByDescription(root, "facts-expanded-${target.clientStoryId}"))
                    assertNotNull(findByDescription(root, "draft-collapsed-${target.clientStoryId}"))
                    assertNotNull(findByDescription(root, "provider-rows-${target.clientStoryId}"))
                    assertEquals(
                        2,
                        collectByPrefix(root, "voice-message-").count {
                            it.contentDescription.toString().contains("voice-feed-clean")
                        },
                    )
                    val text = collectText(root)
                    assertTrue(text.any { it.contains("Я хочу про Дом Советов") })
                    assertTrue(text.any { it.contains("Ещё про площадь") })
                    assertFalse(text.any { it.contains("э-э") || it.contains("а-а") || it.contains("я я") })
                    assertTrue(text.any { it.contains("Telegram") && it.contains("доступно") })
                    assertTrue(text.any { it.contains("VK") && it.contains("нужна проверка") })

                    val supported = requireNotNull(findByDescription(root, "fact-fact-supported")) as CheckBox
                    val unsupported = requireNotNull(findByDescription(root, "fact-fact-unsupported")) as CheckBox
                    assertTrue(supported.isEnabled && supported.isChecked)
                    assertFalse(unsupported.isEnabled)
                    supported.performClick()
                    assertFalse(store.facts(target.clientStoryId).first { it.factId == "fact-supported" }.selected)

                    requireNotNull(findText(root, "Развернуть")).performClick()
                    root = activity.findViewById(android.R.id.content)
                    assertNotNull(findByDescription(root, "draft-expanded-${target.clientStoryId}"))
                    requireNotNull(findText(root, "Свернуть")).performClick()
                    root = activity.findViewById(android.R.id.content)
                    assertNotNull(findByDescription(root, "draft-collapsed-${target.clientStoryId}"))
                }
                instrumentation.waitForIdleSync()
                scenario.onActivity { activity ->
                    val root = activity.findViewById<android.view.View>(android.R.id.content)
                    val scroll = requireNotNull(findByDescription(root, "story-feed")) as ScrollView
                    val maxScroll = ((scroll.getChildAt(0)?.height ?: 0) - scroll.height).coerceAtLeast(0)
                    assertTrue(maxScroll > 0)
                    scroll.scrollTo(0, minOf(320, maxScroll))
                    scrollBeforeRecreate = scroll.scrollY
                    assertTrue(scrollBeforeRecreate > 0)
                }
                instrumentation.waitForIdleSync()
                scenario.recreate()
                instrumentation.waitForIdleSync()
                scenario.onActivity { activity ->
                    val root = activity.findViewById<android.view.View>(android.R.id.content)
                    assertVisible(findByDescription(root, "record-pause"))
                    assertVisible(findByDescription(root, "record-finish"))
                    assertEquals(10, collectByPrefix(root, "story-thread-").size)
                    assertNotNull(findByDescription(root, "draft-collapsed-${target.clientStoryId}"))
                    val scroll = requireNotNull(findByDescription(root, "story-feed")) as ScrollView
                    assertTrue(scroll.scrollY > 0)
                    assertTrue(abs(scroll.scrollY - scrollBeforeRecreate) <= 120)
                }
            }
        } finally {
            store.activeVoiceSession()?.takeIf { it.sessionId == active.sessionId }?.let { store.discardVoiceSession(it.sessionId) }
        }
    }

    private fun findByDescription(view: android.view.View, description: String): android.view.View? {
        if (view.contentDescription?.toString() == description) return view
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) {
                findByDescription(view.getChildAt(index), description)?.let { return it }
            }
        }
        return null
    }

    private fun findText(view: android.view.View, expected: String): TextView? {
        if (view is TextView && view.text?.toString() == expected) return view
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) {
                findText(view.getChildAt(index), expected)?.let { return it }
            }
        }
        return null
    }

    private fun collectByPrefix(
        view: android.view.View,
        prefix: String,
        out: MutableList<android.view.View> = mutableListOf(),
    ): List<android.view.View> {
        if (view.contentDescription?.toString()?.startsWith(prefix) == true) out += view
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) collectByPrefix(view.getChildAt(index), prefix, out)
        }
        return out
    }

    private fun collectText(
        view: android.view.View,
        out: MutableList<String> = mutableListOf(),
    ): List<String> {
        if (view is TextView) out += view.text?.toString().orEmpty()
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) collectText(view.getChildAt(index), out)
        }
        return out
    }

    private fun countType(view: android.view.View, type: Class<*>): Int {
        var count = if (type.isInstance(view)) 1 else 0
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) count += countType(view.getChildAt(index), type)
        }
        return count
    }

    private fun assertVisible(view: android.view.View?) {
        assertNotNull(view)
        val rect = android.graphics.Rect()
        assertTrue(requireNotNull(view).getGlobalVisibleRect(rect))
        assertTrue(rect.width() > 0 && rect.height() > 0)
    }
}
