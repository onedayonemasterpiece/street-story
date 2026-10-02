package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
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
