package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.graphics.Rect
import android.net.Uri
import android.os.SystemClock
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
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
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.MapView
import java.io.File
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class OwnerRoughPlaceInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Test fun nativeGroupToggleAcknowledgementAndProvedMemberSurviveReopening() {
        // Controlled photo/contours and HTTP readbacks; native rendering/touch is real.
        // This test makes no provider-identification or physical-phone claim.
        val config = AppGraph.config(context)
        val priorBackend = config.backendUrl
        config.backendUrl = null
        val topics = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        val priorActive = topics.getString("active_story_id", null)
        val photo = PhotoImporter.import(context, Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original"))
        val id = photo.clientStoryId
        val store = AppGraph.store(context)
        val research = ResearchProjectionStore(context)
        val gson = Gson()
        fun feature(name: String, longitude: Double) = JsonParser.parseString("""{"type":"Feature","id":"osm:way:$name",
            "properties":{"candidate_id":"osm:way:$name","label":"Дом $name","tags":{"addr:city":"Калининград","name":"Дом $name"}},
            "geometry":{"type":"Polygon","coordinates":[[[${longitude-.00013},54.69987],[${longitude+.00013},54.69987],
                [${longitude+.00013},54.70013],[${longitude-.00013},54.70013],[${longitude-.00013},54.69987]]]}}""")
        val features = JsonParser.parseString("""{"type":"FeatureCollection","features":[]}""").asJsonObject.apply {
            getAsJsonArray("features").add(feature("A", 20.4996)); getAsJsonArray("features").add(feature("B", 20.5004))
        }
        val wire = StoryWire().apply {
            this.id = "rough-$id"; clientStoryId = id; photoSha256 = photo.sha256
            identityMode = "manual"; identityGeneration = 0; revision = 1
            state = StoryStage.PHOTO_READY; sourceAvailable = true
            placeMap = gson.fromJson("""{"snapshot_id":"rough-map","photo_sha256":"${photo.sha256}","identity_generation":0,
                "camera":{"latitude":54.7,"longitude":20.5,"origin":"exif"},
                "center":{"latitude":54.7,"longitude":20.5},"zoom":18}""", PlaceMapWire::class.java).apply { buildings = features }
            cameraPoint = placeMap?.camera
        }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val device = UiDevice.getInstance(instrumentation)
        try {
            store.createStory(photo); research.replace(id, wire)
            topics.edit().putString("active_story_id", id).commit()
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                var map: MapLibreMap? = null
                var mapView: MapView? = null
                fun bindMap() = scenario.onActivity { activity ->
                    mapView = views(activity.findViewById(android.R.id.content)).filterIsInstance<MapView>().single()
                    mapView!!.getMapAsync { map = it }
                }
                fun awaitNative(selected: Boolean, narrowed: Boolean = false) {
                    val deadline = SystemClock.elapsedRealtime() + 12000
                    var ready = false
                    var previous: Rect? = null
                    while (!ready && SystemClock.elapsedRealtime() < deadline) {
                        scenario.onActivity {
                            val current = map; val view = mapView
                            if (current?.style != null && view != null) {
                                val bounds = Rect()
                                ready = view.getGlobalVisibleRect(bounds) && view.isAttachedToWindow && !view.isLayoutRequested &&
                                    view.foreground == null && previous == bounds && listOf("A" to 20.4996, "B" to 20.5004).all { (name, lon) ->
                                        val point = current.projection.toScreenLocation(LatLng(54.7, lon))
                                        current.queryRenderedFeatures(point, "owner-buildings-fill").any { it.id() == "osm:way:$name" } &&
                                            (!selected || current.queryRenderedFeatures(point, "owner-selected-fill").any { it.id() == "osm:way:$name" }) &&
                                            (!narrowed || name != "B" || current.queryRenderedFeatures(point, "owner-narrowed-fill").any { it.id() == "osm:way:B" })
                                    }
                                previous = Rect(bounds)
                            }
                        }
                        if (!ready) SystemClock.sleep(100)
                    }
                    assertTrue("both controlled contours must render on a stable visible native frame", ready)
                    instrumentation.waitForIdleSync(); device.waitForIdle()
                    EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-group")
                }
                fun tapMember(name: String, longitude: Double) {
                    EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-group")
                    var x = 0; var y = 0
                    scenario.onActivity {
                        val view = requireNotNull(mapView); val current = requireNotNull(map)
                        val point = current.projection.toScreenLocation(LatLng(54.7, longitude))
                        assertTrue(current.queryRenderedFeatures(point, "owner-buildings-fill").any { it.id() == "osm:way:$name" })
                        val origin = IntArray(2); view.getLocationOnScreen(origin)
                        x = origin[0] + point.x.toInt(); y = origin[1] + point.y.toInt()
                        val bounds = Rect(); assertTrue(view.getGlobalVisibleRect(bounds)); assertTrue(bounds.contains(x, y))
                    }
                    assertTrue("inject one intentional member-toggle tap", device.click(x, y))
                    instrumentation.waitForIdleSync(); device.waitForIdle()
                }
                fun capture(filename: String) {
                    instrumentation.waitForIdleSync(); device.waitForIdle()
                    EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-group")
                    assertTrue(device.takeScreenshot(File(context.getExternalFilesDir(null), filename)))
                }
                try {
                    bindMap(); awaitNative(false)
                    requireNotNull(device.findObject(By.desc("owner-place-mode-rough"))).click()
                    tapMember("A", 20.4996)
                    assertFalse(requireNotNull(device.findObject(By.desc("owner-place-confirm"))).isEnabled)
                    tapMember("B", 20.5004)
                    assertTrue(requireNotNull(device.findObject(By.desc("owner-place-confirm"))).isEnabled)
                    // These are deliberate toggle-off/on interactions, not retries of a failed tap.
                    tapMember("A", 20.4996)
                    assertFalse(requireNotNull(device.findObject(By.desc("owner-place-confirm"))).isEnabled)
                    tapMember("A", 20.4996); awaitNative(true)
                    assertFalse(requireNotNull(device.findObject(By.desc("owner-place-point"))).isEnabled)
                    requireNotNull(device.findObject(By.desc("owner-place-confirm"))).click()
                    assertNotNull(device.wait(Until.findObject(By.desc("owner-place-city")), 3000))
                    requireNotNull(device.findObject(By.res("android:id/button1"))).click()
                    instrumentation.waitForIdleSync(); device.waitForIdle()
                    val queued = requireNotNull(store.pendingOperations(id).singleOrNull { it.kind == "place" })
                    val body = JsonParser.parseString(queued.payloadJson).asJsonObject
                    assertEquals("rough", body["mode"].asString)
                    assertEquals(setOf("osm:way:A", "osm:way:B"), body.getAsJsonArray("candidate_ids").map { it.asString }.toSet())
                    assertFalse("a rough group must not bind one exact member", body.has("candidate_id"))
                    assertFalse("one member's name must not be assigned to the group", body.getAsJsonObject("owner_context").has("name"))
                    assertEquals(photo.sha256, body["expected_photo_sha256"].asString)
                    assertEquals(0, body["expected_identity_generation"].asInt)
                    capture("owner-place-group-pending.png")
                    // Reopening before ACK retains a pending group, not a proved identity.
                    scenario.recreate(); map = null; bindMap(); awaitNative(true)
                    assertTrue(requireNotNull(device.findObject(By.desc("owner-place-mode-rough"))).isChecked)
                    assertNull(research.get(id)?.candidateId)
                    assertTrue(requireNotNull(device.findObject(By.desc("owner-place-group-status"))).text.contains("сохраняется"))
                    MockWebServer().apply { start() }.use { transport ->
                        transport.dispatcher = object : Dispatcher() {
                            override fun dispatch(request: RecordedRequest): MockResponse {
                                if (request.method == "POST" && request.path == "/v1/stories/${wire.id}/place") {
                                    wire.identityGeneration = 1; wire.revision = 2; wire.state = StoryStage.IDENTIFYING
                                    wire.placeMap!!.identityGeneration = 1
                                    wire.placeSelection = JsonParser.parseString("""{"mode":"rough","group_id":"group-$id","status":"identifying",
                                        "candidate_id":null,"narrowed_candidate_id":null,"candidate_ids":["osm:way:A","osm:way:B"],
                                        "generation":1,"photo_sha256":"${photo.sha256}","snapshot_id":"rough-map",
                                        "candidates":[{"candidate_id":"osm:way:A","name":"Дом A"},{"candidate_id":"osm:way:B","name":"Дом B"}]}""").asJsonObject.apply {
                                        add("features", features)
                                    }
                                    wire.visualIdentity = VisualIdentityWire().apply { status = "uncertain" }
                                } else if (request.method != "GET" || request.path != "/v1/stories/${wire.id}") return MockResponse().setResponseCode(404)
                                return MockResponse().setHeader("Content-Type", "application/json").setBody(gson.toJson(wire))
                            }
                        }
                        store.setServerIdentity(id, wire.id)
                        PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
                        val worker = TestListenableWorkerBuilder<SyncWorker>(context).build()
                        val api = ApiClient(transport.url("/").toString().trimEnd('/'), "instrumentation-only")
                        fun sync() = worker.syncStory(store, FeedProjectionStore(context), research, api, requireNotNull(store.story(id)), null)
                        sync()
                        assertEquals("GET", requireNotNull(transport.takeRequest(3, TimeUnit.SECONDS)).method)
                        val post = requireNotNull(transport.takeRequest(3, TimeUnit.SECONDS))
                        assertEquals(queued.requestKey, post.getHeader("Idempotency-Key"))
                        assertEquals(body, JsonParser.parseString(post.body.readUtf8()).asJsonObject)
                        assertEquals("GET", requireNotNull(transport.takeRequest(3, TimeUnit.SECONDS)).method)
                        assertEquals(3, transport.requestCount)
                        assertEquals("done", store.operation(queued.id)?.state)
                        val retainedGroup = wire.placeSelection!!.deepCopy()
                        assertTrue(store.facts(id).isEmpty())
                        assertNull(research.get(id)?.candidateId)
                        scenario.recreate(); map = null; bindMap(); awaitNative(true)
                        assertTrue(requireNotNull(device.findObject(By.desc("owner-place-group-status"))).text.contains("Уточняем"))
                        capture("owner-place-group-identifying.png")
                        wire.revision = 3; wire.state = StoryStage.NEEDS_REVIEW
                        wire.placeSelection!!.addProperty("status", "needs_review")
                        wire.identityClarification = JsonParser.parseString("""{"question":"Какой дом в группе виден на фото?",
                            "photo_sha256":"${photo.sha256}","generation":1}""").asJsonObject
                        sync()
                        scenario.recreate(); map = null; bindMap(); awaitNative(true)
                        assertEquals("Какой дом в группе виден на фото?", requireNotNull(device.findObject(By.desc("owner-place-group-status"))).text)
                        assertTrue(store.facts(id).isEmpty())
                        capture("owner-place-group-review.png")
                        // The normal readback proves B, while preserving A+B and Rough mode.
                        wire.revision = 4; wire.state = StoryStage.RESEARCHING
                        wire.identityClarification = null
                        wire.placeSelection!!.addProperty("status", "resolved")
                        wire.placeSelection!!.addProperty("narrowed_candidate_id", "osm:way:B")
                        wire.visualIdentity = VisualIdentityWire().apply { status = "match"; candidateId = "osm:way:B"; candidateName = "Дом B" }
                        sync()
                        scenario.recreate(); map = null; bindMap(); awaitNative(true, true)
                        assertTrue(requireNotNull(device.findObject(By.desc("owner-place-mode-rough"))).isChecked)
                        assertTrue(requireNotNull(device.findObject(By.desc("owner-place-group-status"))).text.contains("Дом B"))
                        val reopened = requireNotNull(ResearchProjectionStore(context).get(id))
                        val group = requireNotNull(PlaceGroupPresentation.current(reopened.placeSelection, photo.sha256, reopened.identityGeneration))
                        assertEquals(2, group.candidateIds.size); assertEquals("osm:way:B", group.narrowedCandidateId)
                        capture("owner-place-group-reopen.png")
                        // A subsequent exact target clears the group; a late old group cannot restore it.
                        wire.identityGeneration = 2; wire.revision = 5
                        wire.placeMap!!.identityGeneration = 2
                        wire.placeSelection = JsonParser.parseString("""{"mode":"exact","candidate_id":"osm:way:B","generation":2,"photo_sha256":"${photo.sha256}"}""").asJsonObject
                        wire.visualIdentity!!.status = "owner_confirmed"
                        sync()
                        scenario.recreate(); map = null; bindMap(); awaitNative(false)
                        assertTrue(requireNotNull(device.findObject(By.desc("owner-place-mode-exact"))).isChecked)
                        scenario.onActivity { activity -> assertEquals(View.GONE, views(activity.findViewById(android.R.id.content))
                            .single { it.contentDescription?.toString() == "owner-place-group-status" }.visibility) }
                        assertNull(PlaceGroupPresentation.current(research.get(id)?.placeSelection, photo.sha256, 2))
                        wire.identityGeneration = 1; wire.revision = 99; wire.placeSelection = retainedGroup
                        sync()
                        assertEquals(2, ResearchProjectionStore(context).get(id)?.identityGeneration)
                        assertEquals("exact", research.get(id)?.placeSelection?.get("mode")?.asString)
                    }
                } catch (failure: Throwable) {
                    runCatching { device.takeScreenshot(File(context.getExternalFilesDir(null), "owner-place-group-failure.png")) }
                    runCatching { device.dumpWindowHierarchy(File(context.getExternalFilesDir(null), "owner-place-group-failure.xml")) }
                    throw failure
                }
            }
        } finally {
            store.deleteStory(id); research.clear(id); PhotoAssets.releaseTemporary(photo.path)
            topics.edit().putString("active_story_id", priorActive).commit()
            config.backendUrl = priorBackend
        }
    }

    private fun views(view: View): Sequence<View> = sequence {
        yield(view)
        if (view is ViewGroup) for (index in 0 until view.childCount) yieldAll(views(view.getChildAt(index)))
    }
}
