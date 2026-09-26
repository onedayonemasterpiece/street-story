package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.widget.ScrollView
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.ByteArrayInputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class FeedProjectionInstrumentedTest {
    @Test
    fun cleanedVoiceProjectionPersistsAcrossStoreRestart() {
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
            assertEquals(
                "Я хочу уточнить Дом Советов",
                projection.voiceMessages("story-cleaned-persist").single().displayText,
            )
        }
    }

    @Test
    fun topicsListAndTopicDetailAreSeparateSimpleSurfaces() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val store = AppGraph.store(context)
        store.activeVoiceSession()?.let { store.discardVoiceSession(it.sessionId) }

        val firstPhoto = PhotoImporter.importStream(
            context,
            ByteArrayInputStream("topics-photo-a".toByteArray()),
            "image/jpeg",
            "topics-ui-a-${System.nanoTime()}",
        )
        val secondPhoto = PhotoImporter.importStream(
            context,
            ByteArrayInputStream("topics-photo-b".toByteArray()),
            "image/jpeg",
            "topics-ui-b-${System.nanoTime()}",
        )
        val first = store.createStory(firstPhoto)
        val second = store.createStory(secondPhoto)
        store.setServerSnapshot(
            first.clientStoryId,
            StoryStage.REVIEW,
            "Бранденбургские ворота",
            "Калининград",
            "Первый готовый текст публикации.",
            null,
            null,
            null,
            null,
            3,
        )
        store.setDraftText(first.clientStoryId, "Первый готовый текст публикации.")
        store.replaceFacts(
            first.clientStoryId,
            listOf(FactSnapshot("fact-1", "Подтверждённый факт", .9, true, true, "[]")),
        )
        store.setStage(second.clientStoryId, StoryStage.RESEARCHING)

        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
            .edit().remove("active_story_id").commit()

        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val root = activity.findViewById<android.view.View>(android.R.id.content)
                assertNotNull(findText(root, "Темы"))
                assertNotNull(findByDescription(root, "new-topic"))
                assertFalse(collectText(root).any { it.contains("Первый готовый текст публикации.") })
                assertFalse(collectText(root).any { it.contains("Подтверждённый факт") })
                assertTrue(collectText(root).any { it.contains("Бранденбургские ворота") })
                assertTrue(collectText(root).any { it.contains("Ищем факты") })
                var node: android.view.View? = findText(root, "Бранденбургские ворота")
                repeat(2) { node = node?.parent as? android.view.View }
                node?.performClick()
            }
            instrumentation.waitForIdleSync()
            scenario.onActivity { activity ->
                val root = activity.findViewById<android.view.View>(android.R.id.content)
                assertNotNull(findByDescription(root, "topic-scroll") as? ScrollView)
                assertNotNull(findByDescription(root, "publication-image"))
                val preview = findByDescription(root, "publication-preview") as? TextView
                assertNotNull(preview)
                assertTrue(preview?.text?.toString()?.contains("Первый готовый текст") == true)
                assertNotNull(findByDescription(root, "sources"))
                assertNotNull(findByDescription(root, "live-mic"))
                assertFalse(collectText(root).any { it.contains("voice-cleaned-persist") })
            }
            scenario.recreate()
            instrumentation.waitForIdleSync()
            scenario.onActivity { activity ->
                val root = activity.findViewById<android.view.View>(android.R.id.content)
                assertNotNull(findByDescription(root, "publication-preview"))
                assertNotNull(findByDescription(root, "live-mic"))
            }
        }
    }

    private fun findByDescription(view: android.view.View?, description: String): android.view.View? {
        if (view == null) return null
        if (view.contentDescription?.toString() == description) return view
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) {
                findByDescription(view.getChildAt(index), description)?.let { return it }
            }
        }
        return null
    }

    private fun findText(view: android.view.View?, expected: String): TextView? {
        if (view == null) return null
        if (view is TextView && view.text?.toString() == expected) return view
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) {
                findText(view.getChildAt(index), expected)?.let { return it }
            }
        }
        return null
    }

    private fun collectText(
        view: android.view.View?,
        out: MutableList<String> = mutableListOf(),
    ): List<String> {
        if (view == null) return out
        if (view is TextView) out += view.text?.toString().orEmpty()
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) collectText(view.getChildAt(index), out)
        }
        return out
    }
}
