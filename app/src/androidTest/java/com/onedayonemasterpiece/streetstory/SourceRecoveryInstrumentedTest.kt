package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.testing.TestListenableWorkerBuilder
import com.google.gson.Gson
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CopyOnWriteArrayList

@RunWith(AndroidJUnit4::class)
class SourceRecoveryInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Test fun revokedProviderUsesRamOnlyUntilExpiryAndThenReportsRecoverableSourceAccess() {
        val revoked = "content://com.onedayonemasterpiece.streetstory.test.photo-intake/revoked"
        PhotoAssets.retainSelectedUri(revoked, PhotoGpsFixture.bytes())
        try {
            assertArrayEquals(PhotoGpsFixture.bytes(), PhotoAssets.open(context, revoked).use { it.readBytes() })
            PhotoAssets.releaseTemporary(revoked)
            val failure = assertThrows(SourceUnavailableException::class.java) {
                PhotoAssets.open(context, revoked).use { it.readBytes() }
            }
            assertTrue(failure.cause is SecurityException)
            assertEquals(SOURCE_UNAVAILABLE_MESSAGE, failure.message)
        } finally { PhotoAssets.releaseTemporary(revoked) }
    }

    @Test fun missingSharedSourcePreservesProductStateAndSyncsSelectionUntilSameStoryReopensOriginal() {
        val store = AppGraph.store(context)
        val feed = FeedProjectionStore(context)
        val research = ResearchProjectionStore(context)
        val photo = Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original")
        val imported = PhotoImporter.import(context, photo)
        val id = imported.clientStoryId
        val serverId = "server-$id"
        val calls = CopyOnWriteArrayList<RecordedRequest>()
        val wire = StoryWire().apply {
            this.id = serverId
            clientStoryId = id
            state = StoryStage.REVIEW
            sourceAvailable = false
            photoSha256 = imported.sha256
            visualIdentity = VisualIdentityWire().apply { status = "match"; candidateId = "fixture-poi" }
            facts = arrayListOf(FactWire().apply {
                factId = "fixture-supported"
                text = "Сохранённый проверенный факт"
                evidenceSupported = true
                eligibility = "eligible"
                selected = true
            })
            publicationConcept = "Уже выбранный угол"
            draftText = "Сохранённый черновик"
        }
        val gson = Gson()
        val server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    calls.add(request)
                    val path = request.path.orEmpty()
                    if (path == "/v1/capabilities") return MockResponse().setBody("{\"destinations\":[]}")
                    if (request.method == "POST" && path == "/v1/stories") wire.sourceAvailable = true
                    if (request.method == "POST" && path.endsWith("/facts")) wire.facts.single().selected = false
                    return MockResponse().setHeader("Content-Type", "application/json").setBody(gson.toJson(wire))
                }
            }
            start()
        }
        val config = AppGraph.config(context)
        val priorBackend = config.backendUrl
        config.backendUrl = null
        try {
            store.createStory(imported)
            store.setServerIdentity(id, serverId)
            store.setStage(id, StoryStage.REVIEW)
            store.setDraftText(id, "Локальный сохранённый черновик")
            store.replaceFacts(id, listOf(FactSnapshot("fixture-supported", "Сохранённый проверенный факт", .9, true, true, "[]")))
            val factOp = store.enqueueOperation(id, "facts", "select-$id", "{\"selected_fact_ids\":[]}")
            val visualOp = store.enqueueOperation(id, "visual", "visual-$id", "{}")
            // Remove RAM and make the retained provider URI unavailable. This
            // covers provider deletion/revocation, not a fake network timeout.
            PhotoAssets.releaseTemporary(imported.path)
            store.setPhotoSourcePath(id, "content://com.onedayonemasterpiece.streetstory.test.photo-intake/missing")
            val api = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only") { PhotoAssets.open(context, it) }
            val first = TestListenableWorkerBuilder<SyncWorker>(context).build()
            assertFalse(first.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null))
            val blocked = requireNotNull(store.story(id))
            assertEquals(StoryStage.REVIEW, blocked.stage)
            assertEquals(serverId, blocked.serverStoryId)
            assertEquals(imported.sha256, blocked.photoSha256)
            assertEquals("Локальный сохранённый черновик", blocked.draftText)
            assertEquals(SOURCE_UNAVAILABLE_MESSAGE, blocked.lastError)
            assertFalse(store.facts(id).single().selected)
            assertFalse(store.pendingOperations(id).any { it.id == factOp.id })
            assertEquals(visualOp.requestKey, store.pendingOperations(id).single().requestKey)
            assertTrue(calls.any { it.method == "POST" && it.path.orEmpty().endsWith("/facts") })
            assertFalse("SOURCE access must be checked before opening an upload", calls.any { it.method == "POST" && it.path == "/v1/stories" })
            assertFalse("No visual send while SOURCE unavailable", calls.any { it.path.orEmpty().endsWith("/visual") })
            val projection = ResearchProjectionStore(context).get(id)
            assertEquals("Уже выбранный угол", projection?.publicationConcept)
            // The same still-granted provider URI is restored; no photo copy,
            // no new story, and no fresh visual request ID is invented.
            store.setPhotoSourcePath(id, photo.toString())
            val second = TestListenableWorkerBuilder<SyncWorker>(context).build()
            assertFalse(second.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null))
            val restored = requireNotNull(store.story(id))
            assertEquals(serverId, restored.serverStoryId)
            assertEquals(imported.sha256, restored.photoSha256)
            assertEquals(StoryStage.REVIEW, restored.stage)
            assertNull(restored.lastError)
            assertFalse(store.facts(id).single().selected)
            assertEquals("Локальный сохранённый черновик", restored.draftText)
            assertTrue(store.pendingOperations(id).isEmpty())
            val upload = calls.single { it.method == "POST" && it.path == "/v1/stories" }
            assertTrue(upload.body.readUtf8().contains(id))
            val visual = calls.single { it.path.orEmpty().endsWith("/visual") }
            assertEquals(visualOp.requestKey, visual.getHeader("Idempotency-Key"))
            assertFalse(calls.any { it.path.orEmpty().endsWith("/identity") })
            println("source-recovery PASS provider_uri=true facts_selection_preserved=true same_story=true no_source_disk_copy=true")
        } finally {
            feed.close()
            store.deleteStory(id)
            ResearchProjectionStore(context).clear(id)
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
            PhotoAssets.releaseTemporary(imported.path)
            server.shutdown()
            config.backendUrl = priorBackend
        }
    }
}
