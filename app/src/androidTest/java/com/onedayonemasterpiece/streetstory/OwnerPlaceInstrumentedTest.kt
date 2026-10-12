package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.testing.TestListenableWorkerBuilder
import com.google.gson.Gson
import com.google.gson.JsonParser
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CopyOnWriteArrayList

@RunWith(AndroidJUnit4::class)
class OwnerPlaceInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Test fun emptyFactsShowUnfinishedResearchAfterReopenAndClearForAnotherBuilding() {
        val config = AppGraph.config(context)
        val previousBackend = config.backendUrl
        config.backendUrl = null
        val photo = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = photo.clientStoryId
        val store = AppGraph.store(context)
        val research = ResearchProjectionStore(context)
        val topics = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        val priorActive = topics.getString("active_story_id", null)
        // Controlled backend readback, not real-provider or physical-phone evidence.
        val wire = Gson().fromJson("""{"id":"server-$id","photo_sha256":"${photo.sha256}",
            "identity_mode":"manual","identity_generation":1,"revision":2,
            "visual_identity":{"status":"owner_confirmed","candidate_id":"osm:way:A","candidate_name":"Дом A"},
            "research_pending":{"facts":false},"facts":[],
            "research_outcome":{"outcome":"resource_blocked","reason":"source_selection_unavailable",
              "coverage_complete":false,"eligible_count":0,"purpose":"facts",
              "photo_sha256":"${photo.sha256}","identity_generation":1}}""", StoryWire::class.java)
        try {
            store.createStory(photo); store.setServerIdentity(id, wire.id)
            research.replace(id, wire)
            topics.edit().putString("active_story_id", id).commit()
            assertTrue(store.facts(id).isEmpty())
            androidx.test.core.app.ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                val instrumentation = androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()
                val device = androidx.test.uiautomator.UiDevice.getInstance(instrumentation)
                fun assertNoticeVisibleAndCapture(filename: String) {
                    try {
                        instrumentation.waitForIdleSync()
                        EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-empty-facts")
                        scenario.onActivity { activity ->
                            val views = descendants(activity.findViewById(android.R.id.content)).toList()
                            val notice = views.filterIsInstance<android.widget.TextView>()
                                .single { it.contentDescription?.toString() == "facts-research-outcome" }
                            assertEquals("Исследование не завершено · временная проблема с выбором источников", notice.text.toString())
                            assertEquals(android.view.View.VISIBLE, notice.visibility)
                            assertEquals(android.view.View.VISIBLE, (notice.parent as android.view.View).visibility)
                            assertTrue("manual terminal facts must not show automatic recognition stages",
                                views.none { it.contentDescription?.toString() == "identity-progress" && it.visibility == android.view.View.VISIBLE })
                            notice.requestRectangleOnScreen(android.graphics.Rect(0, 0, notice.width, notice.height), true)
                        }
                        instrumentation.waitForIdleSync(); device.waitForIdle()
                        EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-empty-facts")
                        val rendered = requireNotNull(device.findObject(androidx.test.uiautomator.By.desc("facts-research-outcome")))
                        assertTrue("the empty-facts status must be visible on screen", !rendered.visibleBounds.isEmpty)
                        assertTrue("retain unfinished-research UI evidence", device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), filename)))
                    } catch (failure: Throwable) {
                        runCatching { device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), "owner-place-empty-facts-failure.png")) }
                        runCatching { device.dumpWindowHierarchy(java.io.File(context.getExternalFilesDir(null), "owner-place-empty-facts-failure.xml")) }
                        throw failure
                    }
                }
                assertNoticeVisibleAndCapture("owner-place-empty-facts-ui.png")
                assertEquals("resource_blocked", ResearchProjectionStore(context).get(id)?.researchOutcome?.outcome)
                scenario.recreate()
                assertNoticeVisibleAndCapture("owner-place-empty-facts-reopen.png")
                // Even a late A outcome attached to the B readback must be discarded.
                wire.identityGeneration = 2; wire.revision = 3
                wire.visualIdentity = VisualIdentityWire().apply { status = "owner_confirmed"; candidateId = "osm:way:B"; candidateName = "Дом B" }
                research.replace(id, wire)
                assertNull(ResearchProjectionStore(context).get(id)?.researchOutcome)
                scenario.recreate()
                scenario.onActivity { activity ->
                    val views = descendants(activity.findViewById(android.R.id.content)).toList()
                    assertTrue("A outcome must disappear after selecting B",
                        views.none { it.contentDescription?.toString() == "facts-research-outcome" })
                    assertEquals(android.view.View.GONE,
                        views.single { it.contentDescription?.toString() == "facts-island-expanded" }.visibility)
                }
                wire.identityGeneration = 1; wire.revision = 99
                research.replace(id, wire)
                assertEquals(2, ResearchProjectionStore(context).get(id)?.identityGeneration)
                assertNull(research.get(id)?.researchOutcome)
            }
        } finally {
            store.deleteStory(id); research.clear(id); PhotoAssets.releaseTemporary(photo.path)
            topics.edit().putString("active_story_id", priorActive).commit()
            config.backendUrl = previousBackend
        }
    }

    @Test fun manualSyncSkipsIdentificationAndPlaceChangeDetachesDraftAndLateFacts() {
        val config = AppGraph.config(context)
        val previousBackend = config.backendUrl
        config.backendUrl = null
        val store = AppGraph.store(context)
        val research = ResearchProjectionStore(context)
        val feed = FeedProjectionStore(context)
        val photo = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = photo.clientStoryId
        val gson = Gson()
        val calls = CopyOnWriteArrayList<RecordedRequest>()
        val wire = StoryWire().apply {
            this.id = "server-$id"; clientStoryId = id; photoSha256 = photo.sha256
            identityMode = "manual"; identityGeneration = 0; revision = 1
            state = StoryStage.PHOTO_READY; sourceAvailable = true
        }
        val server = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    calls.add(request)
                    if (request.method == "POST" && request.path!!.endsWith("/place")) {
                        wire.identityGeneration = 2; wire.revision = 3; wire.state = StoryStage.RESEARCHING
                        wire.visualIdentity = VisualIdentityWire().apply { status = "owner_confirmed"; candidateId = "osm:way:B"; candidateName = "Дом B" }
                        wire.placeName = "Дом B"; wire.draftText = null
                        wire.cameraPoint = PlaceCameraWire().apply { latitude = 54.7; longitude = 20.5; origin = "exif" }
                        wire.placeSelection = JsonParser.parseString("""{"candidate_id":"osm:way:B","mode":"exact"}""").asJsonObject
                        wire.placeMap = Gson().fromJson("""{"snapshot_id":"map-B","identity_generation":2,
                            "center":{"latitude":54.701,"longitude":20.501},"zoom":18,
                            "camera":{"latitude":54.7,"longitude":20.5,"origin":"exif"},
                            "buildings":{"type":"FeatureCollection","features":[]}}""", PlaceMapWire::class.java)
                        wire.facts = arrayListOf(FactWire().apply { factId = "fact-B"; text = "Факт дома B"; evidenceSupported = true; eligibility = "eligible" })
                    }
                    return MockResponse().setHeader("Content-Type", "application/json").setBody(gson.toJson(wire))
                }
            }; start()
        }
        try {
            store.createStory(photo); store.setServerIdentity(id, wire.id)
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
            val api = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only")
            val worker = TestListenableWorkerBuilder<SyncWorker>(context).build()
            worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null)
            assertTrue("manual intake must never implicitly start identity", calls.none { it.path!!.endsWith("/identity") })
            wire.identityGeneration = 1; wire.revision = 2; wire.state = StoryStage.REVIEW
            wire.visualIdentity = VisualIdentityWire().apply { status = "owner_confirmed"; candidateId = "osm:way:A" }
            wire.draftText = "Черновик A"
            worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null)
            store.setDraftText(id, "Локальный черновик A")
            store.enqueueOperation(id, "place", "instrumentation-place-$id", """{"mode":"exact","candidate_id":"osm:way:B","expected_identity_generation":1}""")
            worker.syncStory(store, feed, research, api, requireNotNull(store.story(id)), null)
            assertNull("draft A must not survive selection B", store.story(id)?.draftText)
            assertEquals(listOf("fact-B"), store.facts(id).map { it.factId })
            val reopened = ResearchProjectionStore(context).get(id)!!
            assertEquals("osm:way:B", reopened.candidateId)
            assertEquals(18.0, reopened.placeMap?.zoom)
            assertEquals("exif", reopened.cameraPoint?.origin)
            research.replace(id, StoryWire().apply { identityGeneration = 1; revision = 99; visualIdentity = VisualIdentityWire().apply { candidateId = "osm:way:A" } })
            assertEquals("osm:way:B", research.get(id)?.candidateId)
        } finally {
            store.deleteStory(id); research.clear(id); PhotoAssets.releaseTemporary(photo.path)
            server.shutdown(); config.backendUrl = previousBackend
        }
    }

    @Test fun nativeMapRendersSelectableContourAlongsidePhotoAndKeepsScrollDuringPan() {
        val config = AppGraph.config(context)
        val previousBackend = config.backendUrl
        config.backendUrl = null
        val photo = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = photo.clientStoryId
        val store = AppGraph.store(context)
        val research = ResearchProjectionStore(context)
        val priorActive = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).getString("active_story_id", null)
        val gson = Gson()
        try {
            store.createStory(photo)
            research.replace(id, StoryWire().apply {
                identityMode = "manual"; identityGeneration = 0; revision = 1
                placeMap = gson.fromJson("""{"snapshot_id":"ui-map","photo_sha256":"${photo.sha256}","identity_generation":0,
                  "camera":{"latitude":54.7,"longitude":20.5,"origin":"exif"},
                  "center":{"latitude":54.7,"longitude":20.5},"zoom":18,
                  "buildings":{"type":"FeatureCollection","features":[{"type":"Feature","id":"osm:way:fixture",
                    "properties":{"candidate_id":"osm:way:fixture","poi_id":"osm:way:fixture","label":"Дом карты"},
                    "geometry":{"type":"Polygon","coordinates":[[[20.4998,54.6998],[20.5002,54.6998],[20.5002,54.7002],[20.4998,54.7002],[20.4998,54.6998]]]}}]}}""", PlaceMapWire::class.java)
            })
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit().putString("active_story_id", id).commit()
            androidx.test.core.app.ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                val device = androidx.test.uiautomator.UiDevice.getInstance(androidx.test.platform.app.InstrumentationRegistry.getInstrumentation())
                var map: org.maplibre.android.maps.MapLibreMap? = null
                var mapView: org.maplibre.android.maps.MapView? = null
                try {
                scenario.onActivity { activity ->
                    val views = descendants(activity.findViewById(android.R.id.content)).toList()
                    mapView = views.filterIsInstance<org.maplibre.android.maps.MapView>().single()
                    mapView!!.getMapAsync { map = it }
                    listOf("publication-image", "owner-place-map", "owner-place-confirm", "owner-place-point").forEach { description ->
                        val view = views.single { it.contentDescription?.toString() == description }
                        assertTrue("$description must be visible with the other selection controls", view.getGlobalVisibleRect(android.graphics.Rect()))
                    }
                }
                assertTrue("opening the map must not enqueue recognition", store.pendingOperations(id).none { it.kind == "identity" })
                val deadline = android.os.SystemClock.elapsedRealtime() + 12000
                var rendered = false
                var targetX = 0; var targetY = 0
                var previousBounds: android.graphics.Rect? = null
                while (!rendered && android.os.SystemClock.elapsedRealtime() < deadline) {
                    scenario.onActivity {
                        val current = map
                        val view = mapView
                        if (current?.style != null && view != null) {
                            val bounds = android.graphics.Rect()
                            val visible = view.getGlobalVisibleRect(bounds)
                            val point = current.projection.toScreenLocation(org.maplibre.android.geometry.LatLng(54.7, 20.5))
                            // The SDK's first queryable frame can precede its load overlay removal.
                            // Require a visible native surface and two stable layout observations.
                            rendered = visible && view.isAttachedToWindow && !view.isLayoutRequested &&
                                view.foreground == null && previousBounds == bounds &&
                                current.queryRenderedFeatures(point, "owner-buildings-fill").any { it.id() == "osm:way:fixture" }
                            previousBounds = android.graphics.Rect(bounds)
                        }
                    }
                    if (!rendered) android.os.SystemClock.sleep(100)
                }
                assertTrue("native contour must render on a stable visible map before touch", rendered)
                androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                device.waitForIdle()
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-map")
                device.dumpWindowHierarchy(java.io.File(context.getExternalFilesDir(null), "owner-place-map-before-tap.xml"))
                scenario.onActivity { activity ->
                    val currentViews = descendants(activity.findViewById(android.R.id.content)).toList()
                    val view = currentViews.filterIsInstance<org.maplibre.android.maps.MapView>().single()
                    assertSame("click must target the currently attached native view", mapView, view)
                    val point = map!!.projection.toScreenLocation(org.maplibre.android.geometry.LatLng(54.7, 20.5))
                    assertTrue("the exact injected touch must hit the server contour", map!!.queryRenderedFeatures(point, "owner-buildings-fill").any { it.id() == "osm:way:fixture" })
                    val origin = IntArray(2); view.getLocationOnScreen(origin)
                    targetX = origin[0] + point.x.toInt(); targetY = origin[1] + point.y.toInt()
                    val bounds = android.graphics.Rect(); assertTrue(view.getGlobalVisibleRect(bounds))
                    assertTrue("touch point must lie inside current visible bounds", bounds.contains(targetX, targetY))
                    android.util.Log.i("StreetStoryMapE2E", "stage=before_single_tap target=$targetX,$targetY bounds=$bounds camera=${map!!.cameraPosition} foreground=${view.foreground}")
                }
                assertTrue("one physical map tap must be injected", device.click(targetX, targetY))
                val selectedStatus = device.wait(androidx.test.uiautomator.Until.findObject(androidx.test.uiautomator.By.textContains("Дом карты")), 3000)
                assertNotNull("one physical tap must select the queried contour", selectedStatus)
                assertTrue("retain native contour selection screenshot", device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), "owner-place-map-ui.png")))
                scenario.onActivity { activity ->
                    descendants(activity.findViewById(android.R.id.content)).single { it.contentDescription?.toString() == "owner-place-confirm" }.performClick()
                }
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-map")
                val cityInput = requireNotNull(device.wait(androidx.test.uiautomator.Until.findObject(androidx.test.uiautomator.By.desc("owner-place-city")), 3000))
                assertTrue("city must not be guessed from EXIF", cityInput.text.isNullOrBlank() || cityInput.text == "Город")
                cityInput.text = "Калининград"
                requireNotNull(device.findObject(androidx.test.uiautomator.By.desc("owner-place-address"))).text = "Клиническая 19А"
                requireNotNull(device.findObject(androidx.test.uiautomator.By.desc("owner-place-name"))).text = "Дом карты"
                requireNotNull(device.findObject(androidx.test.uiautomator.By.res("android:id/button1"))).click()
                val confirmDeadline = android.os.SystemClock.elapsedRealtime() + 3000
                var queuedPlace: PendingOperation? = null
                while (queuedPlace == null && android.os.SystemClock.elapsedRealtime() < confirmDeadline) {
                    queuedPlace = store.pendingOperations(id).firstOrNull { it.kind == "place" }
                    if (queuedPlace == null) android.os.SystemClock.sleep(50)
                }
                if (queuedPlace == null) {
                    device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), "owner-place-map-failure.png"))
                    device.dumpWindowHierarchy(java.io.File(context.getExternalFilesDir(null), "owner-place-map-failure.xml"))
                }
                val queued = requireNotNull(queuedPlace) { "owner context confirmation must persist the place outbox command" }
                val payload = JsonParser.parseString(queued.payloadJson).asJsonObject
                assertEquals("osm:way:fixture", payload["candidate_id"].asString)
                assertEquals("ui-map", payload["snapshot_id"].asString)
                assertEquals("Калининград", payload.getAsJsonObject("owner_context")["city"].asString)
                assertEquals("Клиническая 19А", payload.getAsJsonObject("owner_context")["address"].asString)
                assertEquals("Дом карты", payload.getAsJsonObject("owner_context")["name"].asString)
                MockWebServer().apply { start() }.use { transport ->
                    transport.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody("{\"id\":\"ui-story\"}"))
                    ApiClient(transport.url("/").toString().trimEnd('/'), "instrumentation-only").mutate("ui-story", "place", queued.payloadJson, queued.requestKey)
                    val sent = JsonParser.parseString(transport.takeRequest().body.readUtf8()).asJsonObject
                    assertEquals(payload.getAsJsonObject("owner_context"), sent.getAsJsonObject("owner_context"))
                    assertEquals("osm:way:fixture", sent["candidate_id"].asString)
                }
                scenario.onActivity { activity ->
                    (activity.getSystemService(Context.INPUT_METHOD_SERVICE) as android.view.inputmethod.InputMethodManager)
                        .hideSoftInputFromWindow(activity.window.decorView.windowToken, 0)
                }
                var scrollBefore = 0; var centerBefore: org.maplibre.android.geometry.LatLng? = null
                scenario.onActivity { activity ->
                    scrollBefore = descendants(activity.findViewById(android.R.id.content)).filterIsInstance<android.widget.ScrollView>().single().scrollY
                    centerBefore = map!!.cameraPosition.target
                }
                device.swipe(targetX - 45, targetY, targetX + 45, targetY, 20)
                scenario.onActivity { activity ->
                    assertEquals("panning the map must not scroll the story", scrollBefore, descendants(activity.findViewById(android.R.id.content)).filterIsInstance<android.widget.ScrollView>().single().scrollY)
                    assertTrue("pan moves native camera", centerBefore!!.distanceTo(map!!.cameraPosition.target!!) > 1)
                }
                var zoomBefore = 0.0
                scenario.onActivity { zoomBefore = map!!.cameraPosition.zoom }
                requireNotNull(device.findObject(androidx.test.uiautomator.By.desc("owner-place-map"))).pinchOpen(.6f)
                android.os.SystemClock.sleep(400)
                scenario.onActivity { assertTrue("pinch changes native zoom", map!!.cameraPosition.zoom > zoomBefore) }
                map = null
                scenario.recreate()
                scenario.onActivity { activity ->
                    val restoredMap = descendants(activity.findViewById(android.R.id.content)).filterIsInstance<org.maplibre.android.maps.MapView>().single()
                    restoredMap.getMapAsync { map = it }
                }
                val reopenDeadline = android.os.SystemClock.elapsedRealtime() + 12000
                var reopenRendered = false
                while (!reopenRendered && android.os.SystemClock.elapsedRealtime() < reopenDeadline) {
                    scenario.onActivity {
                        val current = map
                        if (current?.style != null) {
                            val point = current.projection.toScreenLocation(org.maplibre.android.geometry.LatLng(54.7, 20.5))
                            reopenRendered = current.queryRenderedFeatures(point, "owner-buildings-fill").isNotEmpty()
                        }
                    }
                    if (!reopenRendered) android.os.SystemClock.sleep(100)
                }
                assertTrue("recreated native map must restore server contours", reopenRendered)
                assertTrue("retain native map reopen screenshot", device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), "owner-place-map-reopen.png")))
                scenario.onActivity { activity ->
                    descendants(activity.findViewById(android.R.id.content)).single { it.contentDescription?.toString() == "owner-place-automatic" }.performClick()
                }
                assertEquals("recognition remains an explicit separate action", 1, store.pendingOperations(id).count { it.kind == "identity" })
                } catch (failure: Throwable) {
                    // Capture before ActivityScenario.use closes the failed screen, at every stage.
                    runCatching { device.takeScreenshot(java.io.File(context.getExternalFilesDir(null), "owner-place-map-failure.png")) }
                    runCatching { device.dumpWindowHierarchy(java.io.File(context.getExternalFilesDir(null), "owner-place-map-failure.xml")) }
                    runCatching {
                        scenario.onActivity { activity ->
                            val view = descendants(activity.findViewById(android.R.id.content)).filterIsInstance<org.maplibre.android.maps.MapView>().firstOrNull()
                            val bounds = android.graphics.Rect(); view?.getGlobalVisibleRect(bounds)
                            val native = map
                            val point = native?.projection?.toScreenLocation(org.maplibre.android.geometry.LatLng(54.7, 20.5))
                            val hits = if (native != null && point != null) native.queryRenderedFeatures(point, "owner-buildings-fill").map { it.id() } else emptyList()
                            val status = descendants(activity.findViewById(android.R.id.content)).filterIsInstance<android.widget.TextView>()
                                .firstOrNull { it.contentDescription?.toString() == "owner-place-status" }?.text
                            val diagnostic = "failure=${failure.javaClass.simpleName}: ${failure.message}\nbounds=$bounds attached=${view?.isAttachedToWindow} foreground=${view?.foreground}\ncamera=${native?.cameraPosition} point=$point hits=$hits status=$status\n"
                            java.io.File(context.getExternalFilesDir(null), "owner-place-map-failure.txt").writeText(diagnostic)
                            android.util.Log.e("StreetStoryMapE2E", diagnostic)
                        }
                    }
                    throw failure
                }
            }
        } finally {
            store.deleteStory(id); research.clear(id); PhotoAssets.releaseTemporary(photo.path)
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit().putString("active_story_id", priorActive).commit()
            config.backendUrl = previousBackend
        }
    }

    private fun descendants(view: android.view.View): Sequence<android.view.View> = sequence {
        yield(view)
        if (view is android.view.ViewGroup) for (index in 0 until view.childCount) yieldAll(descendants(view.getChildAt(index)))
    }

    @Test fun pointFallbackUsesOwnerCoordinatesAndPlaceReadbackWithoutLive() {
        val server = MockWebServer().apply { start() }
        try {
            server.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody("""{"id":"s","identity_mode":"manual","identity_generation":1,
                "camera_point":{"latitude":54.7,"longitude":20.5,"origin":"manual"},
                "place_selection":{"candidate_id":"owner:point"}}"""))
            val payload = """{"mode":"exact","fallback":{"latitude":54.7,"longitude":20.5,"city":"Калининград","address":"Клиническая 19А"},"expected_identity_generation":0}"""
            val wire = ApiClient(server.url("/").toString().trimEnd('/'), "instrumentation-only").mutate("s", "place", payload, "point-command")
            val request = server.takeRequest()
            assertEquals("/v1/stories/s/place", request.path)
            assertEquals("point-command", request.getHeader("Idempotency-Key"))
            assertEquals(54.7, JsonParser.parseString(request.body.readUtf8()).asJsonObject.getAsJsonObject("fallback")["latitude"].asDouble, 0.0)
            assertEquals("manual", wire.cameraPoint?.origin)
            assertEquals("owner:point", wire.placeSelection?.get("candidate_id")?.asString)
        } finally { server.shutdown() }
    }
}
