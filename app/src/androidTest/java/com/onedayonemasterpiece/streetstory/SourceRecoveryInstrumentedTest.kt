package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.testing.TestListenableWorkerBuilder
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
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


    @Test fun reopenedActivePreviewLoadsBeforeUnrelatedStoryAndKeepsPassOrder() = runBlocking {
        val config = AppGraph.config(context)
        val priorBackend = config.backendUrl
        val priorToken = config.deviceToken
        config.backendUrl = null
        val prefs = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        val priorActive = prefs.getString("active_story_id", null)
        val store = AppGraph.store(context)
        val priorStories = store.stories()
        val photos = List(3) {
            PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        }
        val active = photos[1]
        val newer = photos[2]
        val assetPath = "/v1/assets/server-${active.clientStoryId}/processed"
        val stalePreview = PhotoAssets.retainTemporary(PhotoGpsFixture.bytes())
        PhotoAssets.releaseTemporary(stalePreview) // A reopened process has no previous RAM preview.
        val calls = CopyOnWriteArrayList<RecordedRequest>()
        val blockedBackground = java.util.concurrent.CountDownLatch(1)
        val releaseBackground = java.util.concurrent.CountDownLatch(1)
        val blockOnce = java.util.concurrent.atomic.AtomicBoolean(true)
        val wires = photos.associate { photo ->
            val wire = StoryWire().apply {
                id = "server-${photo.clientStoryId}"
                clientStoryId = photo.clientStoryId
                photoSha256 = photo.sha256
                state = StoryStage.READY_TO_PUBLISH
                sourceAvailable = true
                visualIdentity = VisualIdentityWire().apply { status = "match"; candidateId = "fixture-poi" }
                if (photo == active) processedImageUrl = assetPath
            }
            wire.id to wire
        }
        val server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    calls.add(request)
                    val path = request.path.orEmpty()
                    if (path == "/v1/capabilities") return MockResponse().setBody("{}")
                    if (path == assetPath) return MockResponse().setHeader("Content-Type", "image/jpeg")
                        .setBody(okio.Buffer().write(PhotoGpsFixture.bytes()))
                    if (path == "/v1/stories/server-${newer.clientStoryId}" && blockOnce.getAndSet(false)) {
                        blockedBackground.countDown()
                        releaseBackground.await(10, java.util.concurrent.TimeUnit.SECONDS)
                    }
                    val wire = wires[path.removePrefix("/v1/stories/")]
                    return if (wire != null) MockResponse().setHeader("Content-Type", "application/json")
                        .setBody(Gson().toJson(wire))
                    else MockResponse().setResponseCode(404)
                        .setBody("""{"error":{"code":"fixture_only","message":"Not part of this fixture"}}""")
                }
            }
            start()
        }
        fun storyReads() = calls.filter { it.method == "GET" && it.path.orEmpty().startsWith("/v1/stories/") }
            .map { it.path.orEmpty().removePrefix("/v1/stories/") }
        suspend fun syncPass() = TestListenableWorkerBuilder<SyncWorker>(context).build().doWork()
        try {
            val createdAt = System.currentTimeMillis()
            photos.forEachIndexed { index, photo ->
                store.createStory(photo)
                store.setServerIdentity(photo.clientStoryId, "server-${photo.clientStoryId}")
                store.writableDatabase.execSQL("UPDATE stories SET created_at=? WHERE client_story_id=?",
                    arrayOf(createdAt + index, photo.clientStoryId))
                PhotoImportTelemetry.pending(context, photo.clientStoryId)?.let {
                    PhotoImportTelemetry.acknowledge(context, photo.clientStoryId, it)
                }
            }
            store.setServerSnapshot(active.clientStoryId, StoryStage.READY_TO_PUBLISH,
                null, null, null, assetPath, null, null, null, 1)
            store.setProcessedImagePath(active.clientStoryId, stalePreview, assetPath)
            val normalOrder = store.stories().mapNotNull { it.serverStoryId }
            val activeServerId = "server-${active.clientStoryId}"
            prefs.edit().putString("active_story_id", active.clientStoryId).commit()
            config.backendUrl = server.url("/").toString().trimEnd('/')
            config.deviceToken = "instrumentation-only"
            val work = async(Dispatchers.IO) { syncPass() }
            try {
                assertTrue("Fixture did not reach its unrelated story", blockedBackground.await(10, java.util.concurrent.TimeUnit.SECONDS))
                val preview = store.story(active.clientStoryId)?.processedImagePath
                assertTrue("Active preview still waits behind an unrelated story", preview != null && PhotoAssets.available(preview))
                val bitmap = preview?.let { ImagePreviewDecoder.decode(context, it, 1200, 1000) }
                assertNotNull("Active preview is not decodable", bitmap)
                bitmap?.recycle()
            } finally {
                releaseBackground.countDown()
                work.await()
            }
            assertEquals(listOf(activeServerId) + normalOrder.filter { it != activeServerId }, storyReads())
            for (missingActive in listOf(null, "missing-story")) {
                calls.clear()
                prefs.edit().putString("active_story_id", missingActive).commit()
                syncPass()
                assertEquals("Missing active story must preserve the existing pass order", normalOrder, storyReads())
            }
        } finally {
            releaseBackground.countDown()
            config.backendUrl = null
            photos.forEach { photo ->
                store.deleteStory(photo.clientStoryId)
                ResearchProjectionStore(context).clear(photo.clientStoryId)
                PhotoAssets.releaseTemporary(photo.path)
            }
            priorStories.forEach { store.setStage(it.clientStoryId, it.stage, it.lastError) }
            prefs.edit().putString("active_story_id", priorActive).commit()
            config.deviceToken = priorToken
            server.shutdown()
            config.backendUrl = priorBackend
        }
    }

    @Test fun unchangedSyncReadsOnceWhileMutationKeepsAuthoritativeReadback() {
        val config = AppGraph.config(context)
        val priorBackend = config.backendUrl
        config.backendUrl = null
        val store = AppGraph.store(context)
        val feed = FeedProjectionStore(context)
        val research = ResearchProjectionStore(context)
        val imported = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = imported.clientStoryId
        val calls = CopyOnWriteArrayList<RecordedRequest>()
        var mutated = false
        val wire = StoryWire().apply {
            this.id = "server-$id"
            clientStoryId = id
            photoSha256 = imported.sha256
            state = StoryStage.REVIEW
            sourceAvailable = true
            revision = 17
            draftText = "Сохранённый черновик"
            visualIdentity = VisualIdentityWire().apply { status = "match"; candidateId = "fixture-poi" }
            facts = arrayListOf(FactWire().apply {
                factId = "fixture-supported"
                text = "Собственный подтверждённый факт"
                confidence = .9
                evidenceSupported = true
                eligibility = "eligible"
                selected = true
                sources = arrayListOf(Gson().fromJson("""{"type":"web","url":"https://archive.example/history",
                    "title":"Собственный архив","origin":"rkb","revision":"rev-17","rkb_entity_id":"record-17",
                    "source_ref":"knowledge://chunks/own","supports":[{"kind":"verified_page_span",
                    "source_version_id":"version-17","page_number":17,"text":"Ремонт только планировался в 2009 году."}],
                    "bibliography":[{"author":"Автор описания","title":"Каталог","year":2005,"pages":"17"}]}""", SourceWire::class.java))
            })
        }
        val server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    calls.add(request)
                    if (request.method == "POST" && request.path.orEmpty().endsWith("/facts")) {
                        mutated = true
                        wire.revision = 18
                        wire.facts.single().selected = false
                        wire.summary = "Mutation response"
                    } else if (request.method == "GET" && mutated) {
                        wire.revision = 19
                        wire.summary = "Authoritative readback"
                    }
                    return MockResponse().setHeader("Content-Type", "application/json").setBody(Gson().toJson(wire))
                }
            }
            start()
        }
        try {
            store.createStory(imported)
            store.setServerIdentity(id, wire.id)
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
            val api = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only") { PhotoAssets.open(context, it) }
            val worker = TestListenableWorkerBuilder<SyncWorker>(context).build()
            assertFalse(worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null))
            assertEquals(1, calls.size)
            assertEquals("GET", calls.single().method)
            assertTrue(store.facts(id).single().selected)
            assertEquals("Сохранённый черновик", store.story(id)?.draftText)
            StoryStore(context).use { reopened ->
                val own = Gson().fromJson(reopened.facts(id).single().sourcesJson, Array<SourceWire>::class.java).toList()
                val detail = FactPresentation.sourceDetails(own)
                listOf("version-17", "Страница 17", "только планировался", "rev-17", "record-17", "Автор описания").forEach {
                    assertTrue(detail.contains(it))
                }
                assertTrue(FactPresentation.rkbOrigin(own.single()))
            }
            val operation = store.enqueueOperation(id, "facts", "select-$id", "{\"selected_fact_ids\":[]}")
            assertFalse(worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null))
            assertEquals(3, calls.count { it.method == "GET" })
            val mutation = calls.single { it.method == "POST" }
            assertEquals(operation.requestKey, mutation.getHeader("Idempotency-Key"))
            assertEquals("Authoritative readback", store.story(id)?.summary)
            assertFalse(store.facts(id).single().selected)
            assertTrue(store.pendingOperations(id).isEmpty())
            assertEquals("Сохранённый черновик", store.story(id)?.draftText)
        } finally {
            feed.close()
            store.deleteStory(id)
            research.clear(id)
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
            PhotoAssets.releaseTemporary(imported.path)
            server.shutdown()
            config.backendUrl = priorBackend
        }
    }

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

    @Test fun durableStopReachesServerBeforeFailedVoiceArchive() {
        val store = AppGraph.store(context)
        val feed = FeedProjectionStore(context)
        val research = ResearchProjectionStore(context)
        val imported = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = imported.clientStoryId
        val calls = CopyOnWriteArrayList<RecordedRequest>()
        val wire = StoryWire().apply {
            this.id = "server-$id"
            clientStoryId = id
            photoSha256 = imported.sha256
            state = StoryStage.REVIEW
            sourceAvailable = true
            visualIdentity = VisualIdentityWire().apply { status = "match"; candidateId = "fixture-poi" }
        }
        val server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    calls.add(request)
                    if (request.path.orEmpty().contains("voice-sessions")) {
                        return MockResponse().setResponseCode(503).setBody("{\"error\":{\"code\":\"archive_unavailable\",\"message\":\"Archive temporarily unavailable\"}}")
                    }
                    if (request.path.orEmpty().endsWith("/research-control")) {
                        return MockResponse().setHeader("Content-Type", "application/json")
                            .setBody(Gson().toJson(mapOf("action" to "stop", "purposes" to listOf("facts"), "story" to wire)))
                    }
                    return MockResponse().setHeader("Content-Type", "application/json").setBody(Gson().toJson(wire))
                }
            }
            start()
        }
        try {
            store.createStory(imported)
            store.setServerIdentity(id, wire.id)
            val voice = store.createVoiceSession(id, RecordingKind.REFINEMENT, "controlled-emulator")
            store.finishVoiceSession(voice.sessionId, "2026-10-10T22:00:00Z", 0, 0)
            val stop = store.enqueueOperation(id, "research-control", "stop-$id", "{\"purpose\":\"facts\",\"action\":\"stop\"}")
            val api = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only") { PhotoAssets.open(context, it) }
            val worker = TestListenableWorkerBuilder<SyncWorker>(context).build()
            assertThrows(ApiException::class.java) {
                worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null)
            }
            val stopIndex = calls.indexOfFirst { it.path.orEmpty().endsWith("/research-control") }
            val archiveIndex = calls.indexOfFirst { it.path.orEmpty().contains("voice-sessions") }
            assertTrue(stopIndex >= 0 && archiveIndex > stopIndex)
            assertEquals(stop.requestKey, calls[stopIndex].getHeader("Idempotency-Key"))
            assertFalse(store.pendingOperations(id).any { it.id == stop.id })
            assertTrue(store.finishedVoiceSessions().any { it.sessionId == voice.sessionId })
        } finally {
            feed.close()
            store.deleteStory(id)
            research.clear(id)
            PhotoAssets.releaseTemporary(imported.path)
            server.shutdown()
        }
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
            // An already accepted external operation may complete without
            // local SOURCE access. Keep readback alive, without another send.
            wire.state = StoryStage.VISUAL_PROCESSING
            assertTrue(first.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null))
            assertEquals(StoryStage.VISUAL_PROCESSING, store.story(id)?.stage)
            assertFalse(calls.any { it.path.orEmpty().endsWith("/visual") })
            wire.state = StoryStage.REVIEW
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
