package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.graphics.Point
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.location.Location
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.view.View
import android.view.ViewGroup
import android.widget.GridView
import android.widget.ScrollView
import androidx.exifinterface.media.ExifInterface
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import androidx.work.ListenableWorker
import androidx.work.testing.TestListenableWorkerBuilder
import com.google.gson.JsonObject
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.math.cos
import kotlin.math.sqrt

/** The fixture goes through MediaStore, real EXIF and the Android 15 worker. */
@RunWith(AndroidJUnit4::class)
class PhonePhotoNearbyInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private val store: StoryStore get() = AppGraph.store(context)
    private val camera = PhotoPoint(54.70123456, 20.50234567)
    private val metersPerDegree = 111_195.0

    private fun grantGallery() {
        if (Build.VERSION.SDK_INT < 33) return
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        automation.grantRuntimePermission(context.packageName, Manifest.permission.READ_MEDIA_IMAGES)
        automation.grantRuntimePermission(context.packageName, Manifest.permission.ACCESS_MEDIA_LOCATION)
    }

    private fun point(northMeters: Double, eastMeters: Double = 0.0): PhotoPoint {
        val lat = camera.lat + northMeters / metersPerDegree
        return PhotoPoint(lat, camera.lon + eastMeters / (metersPerDegree * cos(Math.toRadians(lat))))
    }

    private fun imageBytes(point: PhotoPoint?): ByteArray {
        val file = File(context.cacheDir, "street-story-exif-" + System.nanoTime() + ".jpg")
        try {
            // Distinct synthetic test photos make 3-column thumbnails inspectable
            // in captured Android 15 screenshots; GPS still lives in real EXIF.
            val bitmap = Bitmap.createBitmap(240, 320, Bitmap.Config.ARGB_8888)
            val hue = kotlin.math.abs(((point?.lat ?: 0.0) * 100_000).toInt() % 360).toFloat()
            val canvas = Canvas(bitmap)
            canvas.drawColor(Color.HSVToColor(floatArrayOf(hue, .55f, .85f)))
            val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                color = Color.WHITE; textSize = 30f
            }
            canvas.drawText("Street Story", 20f, 140f, paint)
            canvas.drawText(if (point == null) "NO GPS" else "GPS", 20f, 190f, paint)
            val bytes = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.JPEG, 90, bytes)
            bitmap.recycle()
            file.writeBytes(bytes.toByteArray())
            if (point != null) {
                val exif = ExifInterface(file.absolutePath)
                exif.setGpsInfo(Location("test").apply {
                    latitude = point.lat
                    longitude = point.lon
                })
                exif.saveAttributes()
            }
            return file.readBytes()
        } finally { file.delete() }
    }

    private fun insertImage(point: PhotoPoint?, name: String): Uri {
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, "ss-nearby-" + name + "-" + System.nanoTime() + ".jpg")
            put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/StreetStoryTest")
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val uri = requireNotNull(context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values))
        context.contentResolver.openOutputStream(uri)!!.use { it.write(imageBytes(point)) }
        context.contentResolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
        return uri
    }

    private fun id(uri: Uri) = uri.lastPathSegment!!.toLong()

    private fun indexPass() {
        val result = runBlocking { TestListenableWorkerBuilder<PhonePhotoIndexWorker>(context).build().doWork() }
        assertTrue("MediaStore index worker must finish", result is ListenableWorker.Result.Success)
    }

    @Test fun fullReconciliationFindsBothCirclesAndExact149Not151ThenNewImage() {
        grantGallery()
        val created = mutableListOf<Uri>()
        val objectPoint = point(140.0)
        val across149 = point(70.0, sqrt(149.0 * 149.0 - 70.0 * 70.0))
        val across151 = point(70.0, sqrt(151.0 * 151.0 - 70.0 * 70.0))
        try {
            val nearIds = listOf(point(10.0), point(25.0), point(50.0), point(160.0), across149)
                .mapIndexed { n, coordinate -> insertImage(coordinate, "near-" + n).also(created::add) }
                .map(::id).toSet()
            val outside = listOf(across151, point(1000.0), null).mapIndexed { n, coordinate ->
                insertImage(coordinate, "out-" + n).also(created::add)
            }.map(::id).toSet()
            indexPass()
            val found = PhonePhotoIndex.nearby(store, camera, objectPoint).map { it.mediaId }.toSet()
            assertEquals("All five near photos, including the POI-only point and 149m", nearIds, found.intersect(nearIds + outside))
            assertTrue("151m, no GPS, far away excluded", found.intersect(outside).isEmpty())
            val initialVersions = store.phonePhotoVersions()
            assertTrue(initialVersions.keys.containsAll(nearIds + outside))
            assertEquals("No GPS is distinct from invalid reading", "none", initialVersions[id(created.last())]?.gpsState)

            val justTaken = insertImage(point(80.0), "new-while-open").also(created::add)
            indexPass()
            assertTrue(PhonePhotoIndex.nearby(store, camera, objectPoint).any { it.mediaId == id(justTaken) })
            context.contentResolver.delete(justTaken, null, null)
            indexPass()
            assertFalse(PhonePhotoIndex.nearby(store, camera, objectPoint).any { it.mediaId == id(justTaken) })
        } finally { created.forEach { runCatching { context.contentResolver.delete(it, null, null) } } }
    }

    @Test fun selectionPuzzleAndResultCarouselRunInActualActivity() {
        grantGallery()
        val gallery = listOf(insertImage(point(15.0), "ui-a"), insertImage(point(40.0), "ui-b"))
        val storyId = "ss-multiphoto-ui-" + System.nanoTime()
        val source = PhotoImporter.importStream(context, ByteArrayInputStream(PhotoGpsFixture.bytes()), "image/jpeg", storyId)
        val config = AppGraph.config(context)
        val priorBackend = config.backendUrl
        val priorToken = config.deviceToken
        val prefs = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        val priorActive = prefs.getString("active_story_id", null)
        val server = MockWebServer()
        val jpeg = PhotoGpsFixture.withoutGps()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse =
                MockResponse().setResponseCode(200).setHeader("Content-Type", "image/jpeg")
                    .setBody(okio.Buffer().write(jpeg))
        }
        server.start()
        config.backendUrl = server.url("/").toString()
        config.deviceToken = "instrumentation-only"
        store.createStory(source)
        val projection = ResearchProjectionStore(context)
        fun wire(revision: Int, cards: Boolean): StoryWire = StoryWire().apply {
            this.revision = revision
            this.clientStoryId = storyId
            this.id = storyId
            this.photoSha256 = source.sha256
            this.identityGeneration = 1
            this.placeSelection = JsonObject().apply {
                addProperty("mode", "exact")
                add("candidate", JsonObject().apply {
                    add("map_coordinates", JsonObject().apply {
                        addProperty("latitude", point(140.0).lat)
                        addProperty("longitude", point(140.0).lon)
                    })
                })
            }
            if (cards) this.cards = arrayListOf(
                StoryCardWire().apply { position = 0; url = "/mock-card/0" },
                StoryCardWire().apply { position = 1; url = "/mock-card/1" },
                StoryCardWire().apply { position = 2; url = "/mock-card/2" },
            )
        }
        projection.replace(storyId, wire(1, false))
        indexPass()
        prefs.edit().putString("active_story_id", storyId).commit()
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())

        fun find(root: View, description: String): View? {
            if (root.contentDescription?.toString() == description) return root
            if (root is ViewGroup) for (i in 0 until root.childCount)
                find(root.getChildAt(i), description)?.let { return it }
            return null
        }
        fun screenshot(name: String) {
            InstrumentationRegistry.getInstrumentation().waitForIdleSync()
            assertTrue(device.takeScreenshot(File(context.getExternalFilesDir(null), name)))
        }
        try {
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    val decor = activity.window.decorView
                    val scroll = find(decor, "topic-scroll") as ScrollView
                    val panel = requireNotNull(find(decor, "nearby-photos"))
                    scroll.scrollTo(0, panel.top)
                }
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                scenario.onActivity { activity ->
                    val root = activity.window.decorView
                    val grid = requireNotNull(find(root, "nearby-photo-grid")) as GridView
                    assertTrue("source and two gallery entries", grid.adapter.count >= 3)
                    requireNotNull(grid.getChildAt(1)).performClick()
                    requireNotNull(grid.getChildAt(2)).performClick()
                    assertEquals(3, store.selectedStoryPhotos(storyId).size)
                }
                screenshot("multiphoto-selection.png")
                scenario.onActivity { activity ->
                    requireNotNull(find(activity.window.decorView, "nearby-photo-done")).performClick()
                }
                assertEquals(3, store.selectedStoryPhotos(storyId).size)
                assertNotNull(device.wait(Until.findObject(By.desc("order-tile-0")), 4000))
                screenshot("multiphoto-puzzle.png")

                val first = device.wait(Until.findObject(By.desc("order-tile-0")), 4000)
                val second = device.wait(Until.findObject(By.desc("order-tile-1")), 4000)
                assertNotNull(first)
                assertNotNull(second)
                val orderBefore = store.selectedStoryPhotos(storyId).map { it.photoId }
                first!!.drag(second!!.visibleCenter, 35)
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                screenshot("multiphoto-reordered.png")
                val orderAfter = store.selectedStoryPhotos(storyId).map { it.photoId }
                assertEquals(3, orderAfter.size)
                assertNotEquals("Drag must durably change card order", orderBefore, orderAfter)

                projection.replace(storyId, wire(2, true))
                context.sendBroadcast(android.content.Intent(SyncWorker.ACTION_STATE_CHANGED).setPackage(context.packageName))
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                scenario.onActivity { activity ->
                    val root = activity.window.decorView
                    val scroll = find(root, "topic-scroll") as ScrollView
                    val cards = requireNotNull(find(root, "story-cards-grid"))
                    assertEquals(View.VISIBLE, cards.visibility)
                    scroll.scrollTo(0, cards.top)
                }
                screenshot("multiphoto-cards.png")
                scenario.onActivity { activity ->
                    requireNotNull(find(activity.window.decorView, "story-card-0")).performClick()
                }
                val next = device.wait(Until.findObject(By.desc("card-next")), 4000)
                assertNotNull(next)
                next!!.click()
                screenshot("multiphoto-carousel.png")
                device.findObject(By.desc("card-close"))?.click()
            }
        } finally {
            prefs.edit().apply { if (priorActive == null) remove("active_story_id") else putString("active_story_id", priorActive) }.commit()
            projection.clear(storyId)
            store.deleteStory(storyId)
            config.backendUrl = priorBackend
            config.deviceToken = priorToken
            gallery.forEach { context.contentResolver.delete(it, null, null) }
            server.shutdown()
        }
    }
}
