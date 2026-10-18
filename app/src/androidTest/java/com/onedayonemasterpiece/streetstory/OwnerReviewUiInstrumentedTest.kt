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

@RunWith(AndroidJUnit4::class)
class OwnerReviewUiInstrumentedTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    @Test fun conflict_journal_reads_own_sides_and_keeps_decision_after_reopen() {
        val config = AppGraph.config(context)
        val previousBackend = config.backendUrl
        config.backendUrl = null
        val storyId = "conflict-journal-" + System.nanoTime()
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(PhotoGpsFixture.bytes()), "image/jpeg", storyId)
        val server = okhttp3.mockwebserver.MockWebServer().apply { start() }
        val serverId = "server-$storyId"
        try {
            val body = """{"story_id":"$serverId","photo_sha256":"${imported.sha256}","identity_generation":2,
              "items":[{"conflict_id":"own-conflict","poi_key":"own-poi","reason":"Расхождение двух собственных источников",
              "left":{"fact_id":"left","text":"В 2009 году планировали ремонт.","recorded_revision_digest":"left-rev",
              "current_revision_digest":"left-rev","sources":[{"type":"web","url":"https://left.example/history",
              "supports":[{"source_version_id":"left-version","page_number":17,"text":"Работы ещё не начались."}]}]},
              "right":{"fact_id":"right","text":"В 2013 году ремонт завершён.","recorded_revision_digest":"right-rev",
              "current_revision_digest":"right-rev","sources":[{"type":"web","url":"https://right.example/history",
              "supports":[{"source_version_id":"right-version","page_number":23,"text":"Завершение описано отдельно."}]}]},
              "resolution":"both_valid","resolution_reason":"Разные временные границы.","resolved_by":"mira",
              "first_seen_at":1700000000,"last_seen_at":1700000001,"arbitration_events":[{"resolution":"both_valid",
              "reason":"Разные временные границы.","arbitrated_by":"mira","state":"active","created_at":1700000001}]}],
              "has_more":false,"read_only":true}"""
            server.enqueue(okhttp3.mockwebserver.MockResponse().setHeader("Content-Type", "application/json").setBody(body))
            val api = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only")
            val journal = api.conflictJournal(serverId)
            assertEquals("/v1/stories/$serverId/fact-conflicts", server.takeRequest().path)
            assertEquals(1, server.requestCount)
            StoryStore(context).use { store ->
                store.createStory(imported)
                store.setServerIdentity(storyId, serverId)
                store.setDraftText(storyId, "Сохранённый черновик")
                store.replaceFacts(storyId, listOf(
                    FactSnapshot("left", "В 2009 году планировали ремонт.", .9, false, false, "[]", "withheld"),
                    FactSnapshot("right", "В 2013 году ремонт завершён.", .9, false, false, "[]", "withheld")))
            }
            ResearchProjectionStore(context).apply {
                replace(storyId, StoryWire().apply { photoSha256 = imported.sha256; identityGeneration = 2 })
                replaceConflictJournal(storyId, journal, false)
            }
            val reopened = requireNotNull(ResearchProjectionStore(context).conflictJournal(storyId))
            assertEquals("both_valid", reopened.items.single().resolution)
            assertEquals("left-version", reopened.items.single().left.sources.single().supports.single().sourceVersionId)
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit()
                .putString("active_story_id", storyId).commit()
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-review-conflict")
                scenario.onActivity { activity ->
                    descendants(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>()
                        .single { it.text.toString() == "Противоречия и решения" }.performClick()
                }
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-review-conflict")
                val quote = requireNotNull(device.wait(androidx.test.uiautomator.Until.findObject(
                    androidx.test.uiautomator.By.textContains("left-version")), 3000))
                assertTrue(quote.text.contains("Работы ещё не начались") && quote.text.contains("Страница 17"))
                if (device.findObject(androidx.test.uiautomator.By.text("Уточнить с Мирой")) == null) {
                    requireNotNull(device.findObject(androidx.test.uiautomator.By.scrollable(true))).scrollUntil(
                        androidx.test.uiautomator.Direction.DOWN,
                        androidx.test.uiautomator.Until.hasObject(androidx.test.uiautomator.By.text("Уточнить с Мирой")))
                }
                val review = requireNotNull(device.findObject(androidx.test.uiautomator.By.text("Уточнить с Мирой")))
                assertTrue(!review.isEnabled)
                assertNotNull(device.findObject(androidx.test.uiautomator.By.textContains("Разные временные границы")))
                device.pressBack()
            }
            assertEquals(1, server.requestCount)
            StoryStore(context).use { assertEquals("Сохранённый черновик", it.story(storyId)?.draftText) }
        } finally {
            StoryStore(context).use { it.deleteStory(storyId) }
            ResearchProjectionStore(context).clear(storyId)
            PhotoAssets.releaseTemporary(imported.path)
            PhotoImportTelemetry.pending(context, storyId)?.let { PhotoImportTelemetry.acknowledge(context, storyId, it) }
            server.shutdown()
            config.backendUrl = previousBackend
        }
    }

    @Test fun own_source_dialog_keeps_versions_and_quotes_after_reopen() {
        val config = AppGraph.config(context)
        val previousBackend = config.backendUrl
        config.backendUrl = null
        val storyId = "own-source-dialog-" + System.nanoTime()
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(PhotoGpsFixture.bytes()), "image/jpeg", storyId)
        try {
            StoryStore(context).use { store ->
                store.createStory(imported)
                store.replaceFacts(storyId, listOf(FactSnapshot("own-fact", "В 2009 году планировали ремонт.", .9, false, false,
                    """[{"type":"web","origin":"rkb","revision":"own-rev","url":"https://archive.example/history",
                      "supports":[{"source_version_id":"version-A","page_number":17,"text":"В 2009 году планировали ремонт."}]},
                      {"type":"web","url":"https://archive.example/history","supports":[{"source_version_id":"version-B",
                      "text":"Работы ещё не начались."}]}]""", "withheld")))
                store.setDraftText(storyId, "Сохранённый авторский черновик")
            }
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit()
                .putString("active_story_id", storyId).commit()
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-review-source")
                scenario.onActivity { activity ->
                    val root = findByDescription(activity.findViewById(android.R.id.content), "facts-island-expanded")!!
                    assertTrue(descendants(root).filterIsInstance<CheckBox>().none { it.isEnabled })
                    val link = descendants(root).filterIsInstance<TextView>().single { it.text.toString().contains("· RKB") }
                    val spans = link.text as Spanned
                    spans.getSpans(0, spans.length, ClickableSpan::class.java).single().onClick(link)
                }
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-review-source")
                val content = requireNotNull(device.wait(androidx.test.uiautomator.Until.findObject(androidx.test.uiautomator.By.textContains("version-A")), 3000))
                assertTrue(content.text.contains("version-B") && content.text.contains("Страница 17"))
                assertTrue(content.text.contains("планировали ремонт"))
                assertTrue(content.text.contains("Работы ещё не начались"))
                device.pressBack()
            }
            StoryStore(context).use { store ->
                assertEquals("Сохранённый авторский черновик", store.story(storyId)?.draftText)
                assertTrue(!store.facts(storyId).single().evidenceSupported && !store.facts(storyId).single().selected)
            }
        } finally {
            StoryStore(context).use { it.deleteStory(storyId) }
            ResearchProjectionStore(context).clear(storyId)
            PhotoAssets.releaseTemporary(imported.path)
            PhotoImportTelemetry.pending(context, storyId)?.let { PhotoImportTelemetry.acknowledge(context, storyId, it) }
            config.backendUrl = previousBackend
        }
    }

    @Test fun pending_facts_and_status_reason_survive_store_reopen_without_live_session() {
        val storyId = "durable-research-ui-" + System.nanoTime()
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(PhotoGpsFixture.bytes()), "image/jpeg", storyId)
        StoryStore(context).use { store ->
            store.createStory(imported)
            store.replaceFacts(storyId, listOf(FactSnapshot("f", "Нужно проверить собственный источник", .5,
                false, false, "[]", "withheld", "own_evidence_missing")))
        }
        val wire = StoryWire().apply {
            photoSha256 = imported.sha256
            identityGeneration = 2
            researchPending = mapOf("facts" to true)
            researchControls = mapOf("facts" to ResearchControlWire().apply {
                photoSha256 = imported.sha256; identityGeneration = 2
            })
        }
        ResearchProjectionStore(context).replace(storyId, wire)
        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit()
            .putString("active_story_id", storyId).commit()
        StoryStore(context).use { store ->
            assertEquals("own_evidence_missing", store.facts(storyId).single().eligibilityReason)
        }
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val root = findByDescription(activity.findViewById(android.R.id.content), "facts-island-expanded")!!
                val texts = descendants(root).filterIsInstance<TextView>().map { it.text.toString() }
                assertTrue(texts.contains("Факты · поиск продолжается"))
                assertTrue(texts.contains("Нет собственной опоры в источнике"))
                assertNotNull(findByDescription(root, "research-loader"))
            }
        }
    }

    @Test
    fun realInternetFactsShowSourcesAndKeepOwnerSubsetAfterReopen() {
        // Replay the captured production projection, not a connected-backend test.
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val captured = JSONObject(instrumentation.context.assets.open("live-first-real-facts.json").bufferedReader().use { it.readText() })
        assertEquals("real_retrieval", captured.getString("source_mode"))
        val storyId = "real-facts-ui-" + System.nanoTime()
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(PhotoGpsFixture.bytes()), "image/jpeg", storyId)
        val rows = captured.getJSONArray("facts")
        val facts = (0 until rows.length()).map { index ->
            val json = rows.getJSONObject(index)
            val fact = com.google.gson.Gson().fromJson(json.toString(), FactWire::class.java)
            FactSnapshot(fact.factId, fact.text, fact.confidence,
                fact.eligibleForSelection, fact.selected && fact.eligibleForSelection, json.getJSONArray("sources").toString())
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
                // Three owner actions returning to the same selection must
                // remain three durable mutations, including after reopen.
                boxes[0].performClick()
                boxes[0].performClick()
                StoryStore(context).use { store ->
                    val edits = store.pendingOperations(storyId).filter { it.kind == "facts" }
                    assertEquals(3, edits.size)
                    assertEquals(3, edits.map { it.requestKey }.distinct().size)
                    assertEquals(edits.first().payloadJson, edits.last().payloadJson)
                }
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
        val original = PhotoGpsFixture.bytes()
        val imported = PhotoImporter.importStream(
            context,
            ByteArrayInputStream(original),
            "image/jpeg",
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
