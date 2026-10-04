package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.graphics.Rect
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.CheckBox
import android.text.Spanned
import android.text.style.ClickableSpan
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import org.json.JSONObject
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.util.Base64

@RunWith(AndroidJUnit4::class)
class OwnerReviewUiInstrumentedTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    @Test
    fun realInternetFactsShowSourcesAndKeepOwnerSubsetAfterReopen() {
        // Replay the captured production projection, not a connected-backend test.
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val captured = JSONObject(instrumentation.context.assets.open("live-first-real-facts.json").bufferedReader().use { it.readText() })
        assertEquals("real_retrieval", captured.getString("source_mode"))
        val storyId = "real-facts-ui-" + System.nanoTime()
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(Base64.getDecoder().decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlJkAAAAASUVORK5CYII="
        )), "image/png", storyId)
        val rows = captured.getJSONArray("facts")
        val facts = (0 until rows.length()).map { index ->
            val fact = rows.getJSONObject(index)
            FactSnapshot(fact.getString("fact_id"), fact.getString("text"), fact.getDouble("confidence"),
                fact.getBoolean("evidence_supported"), fact.getBoolean("selected"), fact.getJSONArray("sources").toString())
        }
        StoryStore(context).use { store ->
            store.createStory(imported)
            store.replaceFacts(storyId, facts)
            store.setDraftText(storyId, captured.getString("draft_text"))
        }
        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit().putString("active_story_id", storyId).commit()
        var selectedId = ""
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val root = findByDescription(activity.findViewById(android.R.id.content), "facts-island-expanded")!!
                val boxes = descendants(root).filterIsInstance<CheckBox>()
                assertEquals(facts.size, boxes.size)
                assertEquals(3, boxes.count { it.isChecked })
                assertTrue(boxes.all { it.isEnabled })
                boxes[0].performClick()
                assertEquals("Факты · выбрано 4 из 16", (root as ViewGroup).getChildAt(0).let { (it as TextView).text.toString() })
                selectedId = facts[0].factId
                val sources = descendants(root).filterIsInstance<TextView>().filter { it.contentDescription?.toString()?.startsWith("Источники факта:") == true }
                assertEquals(facts.size, sources.size)
                assertTrue(sources.all { view -> (view.text as Spanned).getSpans(0, view.text.length, ClickableSpan::class.java).isNotEmpty() })
                boxes[0].requestRectangleOnScreen(Rect(0, 0, boxes[0].width, boxes[0].height), true)
            }
            instrumentation.waitForIdleSync()
            val screenshot = File(context.getExternalFilesDir(null), "real-facts-ui.png")
            assertTrue(UiDevice.getInstance(instrumentation).takeScreenshot(screenshot))
            scenario.recreate()
            scenario.onActivity { activity ->
                val root = findByDescription(activity.findViewById(android.R.id.content), "facts-island-expanded")!!
                assertEquals(4, descendants(root).filterIsInstance<CheckBox>().count { it.isChecked })
            }
        }
        StoryStore(context).use { store ->
            assertTrue(store.facts(storyId).single { it.factId == selectedId }.selected)
            assertEquals(4, store.facts(storyId).count { it.selected })
        }
    }

    private fun descendants(root: View): List<View> = listOf(root) +
        if (root is ViewGroup) (0 until root.childCount).flatMap { descendants(root.getChildAt(it)) } else emptyList()

    @Test
    fun topicsUseThumbReachableBottomEndFab() {
        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
            .edit().remove("active_story_id").commit()
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val content = activity.findViewById<ViewGroup>(android.R.id.content)
                val fab = findByDescription(content, "new-topic")
                assertNotNull(fab)
                val dock = fab!!.parent as View
                val params = dock.layoutParams as FrameLayout.LayoutParams
                assertTrue(params.gravity and Gravity.BOTTOM == Gravity.BOTTOM)
                assertTrue(params.gravity and Gravity.END == Gravity.END)
            }
        }
    }

    @Test
    fun topicMilestonesLiveInsideConversationAndStickyBentoIsSeparate() {
        val storyId = "owner-review-ui-" + System.nanoTime()
        val png = Base64.getDecoder().decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlJkAAAAASUVORK5CYII="
        )
        val imported = PhotoImporter.importStream(
            context,
            ByteArrayInputStream(png),
            "image/png",
            storyId,
        )
        StoryStore(context).use { store ->
            store.createStory(imported)
            store.setDraftText(storyId, "Черновик публикации для UI acceptance.")
            store.replaceFacts(
                storyId,
                listOf(
                    FactSnapshot(
                        "fact-1",
                        "Проверенный факт.",
                        0.95,
                        true,
                        true,
                        """[{"type":"web","title":"Источник факта","url":"https://example.com/source"}]""",
                    )
                ),
            )
        }
        ResearchProjectionStore(context).replace(
            storyId,
            StoryWire().apply {
                publicationConcept = "Показать современную жизнь места."
                publication = PublicationWire().apply {
                    state = "scheduled"
                    destinations.add("owner_test_tg")
                    scheduledFor = "2026-10-02T12:00:00+02:00"
                }
            },
        )
        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
            .edit().putString("active_story_id", storyId).commit()

        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val content = activity.findViewById<ViewGroup>(android.R.id.content)
                val chat = findByDescription(content, "live-chat")
                val facts = findByDescription(content, "facts-island-expanded")
                val concept = findByDescription(content, "concept-island-expanded")
                val draft = findByDescription(content, "publication-preview-chat")
                val publication = findByDescription(content, "publication-event")
                val bento = findByDescription(content, "sticky-topic-bento")

                listOf(chat, facts, concept, draft, publication, bento).forEach(::assertNotNull)
                assertTrue(isDescendant(facts!!, chat!!))
                assertTrue(isDescendant(concept!!, chat))
                assertTrue(isDescendant(draft!!, chat))
                assertTrue(isDescendant(publication!!, chat))
                assertTrue(!isDescendant(bento!!, chat))
                assertEquals("Запланировано · ТГ", (publication as TextView).text.toString())
            }
        }
    }

    private fun findByDescription(root: View, description: String): View? {
        if (root.contentDescription?.toString() == description) return root
        if (root is ViewGroup) {
            for (index in 0 until root.childCount) {
                findByDescription(root.getChildAt(index), description)?.let { return it }
            }
        }
        return null
    }

    private fun isDescendant(view: View, ancestor: View): Boolean {
        var current = view.parent
        while (current is View) {
            if (current === ancestor) return true
            current = current.parent
        }
        return false
    }
}
