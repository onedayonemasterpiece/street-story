package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.Manifest
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import androidx.activity.result.contract.ActivityResultContracts.PickVisualMedia
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.regex.Pattern

@RunWith(AndroidJUnit4::class)
class PhotoIntakeInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private val photo = Uri.parse("content://com.onedayonemasterpiece.streetstory.test.photo-intake/original")

    private fun share() = Intent(context, MainActivity::class.java).apply {
        action = Intent.ACTION_SEND
        type = "image/jpeg"
        putExtra(Intent.EXTRA_STREAM, photo)
    }

    private fun allowExistingMetadata() {
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        // Material dialog buttons render labels in uppercase. Use the stable
        // negative-button resource, scoped by this dialog's title.
        if (device.wait(Until.findObject(By.text("Геометки выбранного фото")), 1500) != null) {
            requireNotNull(device.findObject(By.res("android", "button2"))).click()
        }
    }

    private fun waitForStory(previous: Set<String>): StorySnapshot {
        val deadline = System.currentTimeMillis() + 8000
        while (System.currentTimeMillis() < deadline) {
            AppGraph.store(context).stories().firstOrNull { it.clientStoryId !in previous }?.let { return it }
            Thread.sleep(50)
        }
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        val visible = device.findObjects(By.text(Pattern.compile(".+"))).map { it.text }.take(12)
        error("Shared photo did not create a story; visible=$visible")
    }

    @Test fun temporaryGalleryGrantPreservesGpsAndDoesNotDeduplicate() {
        val first = PhotoImporter.import(context, photo)
        val second = PhotoImporter.import(context, photo)
        try {
            assertNotEquals(first.clientStoryId, second.clientStoryId)
            assertNotEquals(first.sha256, second.sha256)
            assertTrue(first.path.startsWith("ram-photo:"))
            assertEquals(54.70123456, first.latitude!!, 0.0000001)
            assertEquals(20.50234567, first.longitude!!, 0.0000001)
            assertArrayEquals(PhotoGpsFixture.bytes(), PhotoAssets.open(context, first.path).use { it.readBytes() })
        } finally {
            listOf(first, second).forEach { imported ->
                PhotoAssets.releaseTemporary(imported.path)
                PhotoImportTelemetry.pending(context, imported.clientStoryId)?.let { PhotoImportTelemetry.acknowledge(context, imported.clientStoryId, it) }
            }
        }
    }

    @Test fun coldShareRecreationAndWarmShareCreateExactlyTwoStories() {
        val store = AppGraph.store(context)
        val previous = store.stories().map { it.clientStoryId }.toSet()
        try {
            ActivityScenario.launch<MainActivity>(share()).use { scenario ->
                allowExistingMetadata()
                val first = waitForStory(previous)
                assertNotNull(first.latitude)
                scenario.recreate()
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                assertEquals(1, store.stories().count { it.clientStoryId !in previous })
                context.startActivity(share().addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
                allowExistingMetadata()
                val second = waitForStory(previous + first.clientStoryId)
                assertNotEquals(first.clientStoryId, second.clientStoryId)
                assertEquals(2, store.stories().count { it.clientStoryId !in previous })
            }
        } finally {
            store.stories().filter { it.clientStoryId !in previous }.forEach { story ->
                store.deleteStory(story.clientStoryId)
                PhotoImportTelemetry.pending(context, story.clientStoryId)?.let { PhotoImportTelemetry.acknowledge(context, story.clientStoryId, it) }
            }
        }
    }

    @Test fun finishedIdentificationHasNoPauseControlsButExplicitPauseCanResume() {
        val store = AppGraph.store(context)
        val imported = PhotoImporter.import(context, photo)
        val id = imported.clientStoryId
        val prefs = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        val prior = prefs.getString("active_story_id", null)
        val projection = ResearchProjectionStore(context)
        store.createStory(imported)
        store.setServerIdentity(id, "story_owner_gps_ui_test")
        store.setStage(id, StoryStage.NEEDS_REVIEW)
        val wire = StoryWire().apply {
            photoSha256 = imported.sha256
            identityGeneration = 0
            identityProgress = IdentityProgressWire().apply { finished = true }
            researchControls = listOf("identity", "facts").associateWith {
                ResearchControlWire().apply { photoSha256 = imported.sha256; identityGeneration = 0 }
            }
        }
        projection.replace(id, wire)
        prefs.edit().putString("active_story_id", id).commit()
        try {
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    val host = MainActivity::class.java.getDeclaredField("researchControlBlock").apply { isAccessible = true }
                        .get(activity) as android.widget.LinearLayout
                    assertEquals(android.view.View.GONE, host.visibility)
                    wire.researchControls.getValue("identity").stopped = true
                    projection.replace(id, wire)
                    MainActivity::class.java.getDeclaredMethod("refreshTopicDetail").apply { isAccessible = true }.invoke(activity)
                    assertEquals(android.view.View.VISIBLE, host.visibility)
                    assertEquals(1, host.childCount)
                    val resume = host.getChildAt(0) as android.widget.Button
                    assertEquals("Возобновить поиск объекта", resume.text.toString())
                    assertEquals("research-resume-identity", resume.contentDescription.toString())
                    wire.researchControls.getValue("identity").stopped = false
                    wire.identityProgress!!.finished = false
                    store.setStage(id, StoryStage.IDENTIFYING)
                    projection.replace(id, wire)
                    MainActivity::class.java.getDeclaredMethod("refreshTopicDetail").apply { isAccessible = true }.invoke(activity)
                    assertEquals(1, host.childCount)
                    assertEquals("Приостановить поиск объекта", (host.getChildAt(0) as android.widget.Button).text.toString())
                }
            }
        } finally {
            store.deleteStory(id)
            projection.clear(id)
            prefs.edit().putString("active_story_id", prior).commit()
            PhotoAssets.releaseTemporary(imported.path)
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
        }
    }

    @Test fun originalPickerSelectsMonthOldPhotoWithGpsAndFutureContractAcceptsNineWithoutDeduplication() {
        assertTrue("Android 15 emulator must have Photo Picker", PickVisualMedia.isPhotoPickerAvailable(context))
        val intent = PhotoIntake.documentIntent()
        assertEquals(Intent.ACTION_OPEN_DOCUMENT, intent.action)
        assertEquals("image/*", intent.type)
        val future = PhotoIntake.pickerIntent(context, PhotoIntake.FUTURE_LIMIT)
        assertEquals(9, future.getIntExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, 0))
        assertEquals(9, PhotoIntake.checked(List(9) { photo }, 9).size)
        assertEquals(listOf(photo), PhotoIntake.sharedPhotos(share()))
        assertTrue(PhotoIntake.sharedPhotos(Intent(Intent.ACTION_SEND).apply { type = "text/plain" }).isEmpty())
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        fun grant(packageName: String, permission: String) {
            InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(
                "pm grant $packageName $permission").use { descriptor ->
                android.os.ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { it.readBytes() }
            }
        }
        grant(context.packageName, Manifest.permission.ACCESS_MEDIA_LOCATION)
        val pickerPackage = context.packageManager.resolveActivity(intent, 0)!!.activityInfo.packageName
        val store = AppGraph.store(context)
        val previous = store.stories().map { it.clientStoryId }.toSet()
        val takenAt = System.currentTimeMillis() - 28L * 24 * 60 * 60 * 1000
        val resolver = context.contentResolver
        val media = requireNotNull(resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, "street-story-month-old-test.jpg")
            put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(MediaStore.Images.Media.RELATIVE_PATH, "DCIM/Camera")
            put(MediaStore.Images.Media.DATE_TAKEN, takenAt)
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }))
        try {
            resolver.openOutputStream(media)!!.use { it.write(PhotoGpsFixture.galleryBytes()) }
            resolver.update(media, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    MainActivity::class.java.getDeclaredMethod("openOriginalPhotoPicker").apply { isAccessible = true }.invoke(activity)
                }
                assertTrue(device.wait(Until.hasObject(By.pkg(pickerPackage).depth(0)), 5000))
                // Images root can show camera albums first, rather than a flat
                // Recent list. Open the exact folder seeded by this fixture.
                device.wait(Until.findObject(By.pkg(pickerPackage).text("Camera")), 2000)?.click()
                val item = device.wait(Until.findObject(By.text("street-story-month-old-test.jpg")), 5000)
                    ?: device.findObject(By.descContains("street-story-month-old-test"))
                    ?: device.findObject(By.res(Pattern.compile(".*:id/(icon_thumb|icon_thumbnail)")))
                    ?: device.wait(Until.findObject(By.pkg(pickerPackage).desc(Pattern.compile("(?i).*photo taken.*"))), 15000)
                device.dumpWindowHierarchy(File(context.getExternalFilesDir(null), "original-picker-ui.xml"))
                device.takeScreenshot(File(context.getExternalFilesDir(null), "original-picker-ui.png"))
                assertNotNull("Month-old photo must be selectable in original provider; package=$pickerPackage", item)
                item!!.click()
                val selected = waitForStory(previous)
                assertTrue(selected.photoPath.startsWith("content:") || selected.photoPath.startsWith("ram-photo:"))
                assertTrue(PhotoAssets.open(context, selected.photoPath).use { it.read() } >= 0)
                assertEquals("Normal selected original must preserve GPS", 54.70123456, selected.latitude!!, 0.0000001)
                assertEquals(20.50234567, selected.longitude!!, 0.0000001)
                val diagnostic = PhotoImportTelemetry.pending(context, selected.clientStoryId).orEmpty()
                assertTrue("GPS must reach import telemetry", diagnostic.contains("gps_present"))
                println("photo-intake original-month-old PASS story=${selected.clientStoryId} gps=true")
            }
        } finally {
            resolver.delete(media, null, null)
            store.stories().filter { it.clientStoryId !in previous }.forEach { story ->
                store.deleteStory(story.clientStoryId)
                PhotoImportTelemetry.pending(context, story.clientStoryId)?.let { PhotoImportTelemetry.acknowledge(context, story.clientStoryId, it) }
            }
        }
    }
}
