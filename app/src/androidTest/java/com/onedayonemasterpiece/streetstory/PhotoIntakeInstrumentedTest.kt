package com.onedayonemasterpiece.streetstory

import android.content.Context
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
        device.wait(Until.findObject(By.text("Без геометок")), 1500)?.click()
    }

    private fun waitForStory(previous: Set<String>): StorySnapshot {
        val deadline = System.currentTimeMillis() + 8000
        while (System.currentTimeMillis() < deadline) {
            AppGraph.store(context).stories().firstOrNull { it.clientStoryId !in previous }?.let { return it }
            Thread.sleep(50)
        }
        error("Shared photo did not create a story")
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

    @Test fun nativePickerSelectsMonthOldPhotoAndFutureContractAcceptsNineWithoutDeduplication() {
        assertTrue("Android 15 emulator must have Photo Picker", PickVisualMedia.isPhotoPickerAvailable(context))
        val intent = PhotoIntake.pickerIntent(context)
        assertEquals(MediaStore.ACTION_PICK_IMAGES, intent.action)
        assertEquals("image/*", intent.type)
        val future = PhotoIntake.pickerIntent(context, PhotoIntake.FUTURE_LIMIT)
        assertEquals(9, future.getIntExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, 0))
        assertEquals(9, PhotoIntake.checked(List(9) { photo }, 9).size)
        assertEquals(listOf(photo), PhotoIntake.sharedPhotos(share()))
        assertTrue(PhotoIntake.sharedPhotos(Intent(Intent.ACTION_SEND).apply { type = "text/plain" }).isEmpty())
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        val store = AppGraph.store(context)
        val previous = store.stories().map { it.clientStoryId }.toSet()
        val takenAt = System.currentTimeMillis() - 28L * 24 * 60 * 60 * 1000
        val resolver = context.contentResolver
        val media = requireNotNull(resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, "street-story-month-old-test.jpg")
            put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(MediaStore.Images.Media.DATE_TAKEN, takenAt)
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }))
        try {
            resolver.openOutputStream(media)!!.use { it.write(PhotoGpsFixture.bytes()) }
            resolver.update(media, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    MainActivity::class.java.getDeclaredMethod("openOriginalPhotoPicker").apply { isAccessible = true }.invoke(activity)
                }
                val pickerPackage = context.packageManager.resolveActivity(intent, 0)!!.activityInfo.packageName
                assertTrue(device.wait(Until.hasObject(By.pkg(pickerPackage).depth(0)), 5000))
                val thumbnail = device.wait(Until.findObject(By.res(Pattern.compile(".*:id/icon_thumbnail"))), 5000)
                assertNotNull("Month-old photo must be selectable in native picker", thumbnail)
                thumbnail!!.click()
                val selected = waitForStory(previous)
                assertTrue(selected.photoPath.startsWith("content:") || selected.photoPath.startsWith("ram-photo:"))
                assertTrue(PhotoAssets.open(context, selected.photoPath).use { it.read() } >= 0)
                println("photo-intake native-month-old PASS story=${selected.clientStoryId} gps=${selected.latitude != null}")
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
