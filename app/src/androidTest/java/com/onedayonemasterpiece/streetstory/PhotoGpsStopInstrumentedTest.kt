package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.view.ViewGroup
import android.widget.ImageButton
import androidx.exifinterface.media.ExifInterface
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.io.File

@RunWith(AndroidJUnit4::class)
class PhotoGpsStopInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    private fun fixture(): ByteArray {
        // Synthetic test-only JPEG metadata in RAM; no source image files or hashing.
        val bitmap = Bitmap.createBitmap(24, 32, Bitmap.Config.ARGB_8888)
        val jpeg = ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.JPEG, 90, it) }.toByteArray()
        bitmap.recycle()
        val tiff = ByteBuffer.allocate(140).order(ByteOrder.LITTLE_ENDIAN)
        tiff.put('I'.code.toByte()).put('I'.code.toByte()).putShort(42).putInt(8)
        tiff.putShort(2)
        fun entry(tag: Int, type: Int, count: Int, value: Int) {
            tiff.putShort(tag.toShort()).putShort(type.toShort()).putInt(count).putInt(value)
        }
        entry(0x112, 3, 1, 6); entry(0x8825, 4, 1, 38); tiff.putInt(0)
        tiff.putShort(4)
        entry(1, 2, 2, 'N'.code); entry(2, 5, 3, 92)
        entry(3, 2, 2, 'E'.code); entry(4, 5, 3, 116); tiff.putInt(0)
        for (value in intArrayOf(54, 1, 42, 1, 4444416, 1000000, 20, 1, 30, 1, 8444412, 1000000)) tiff.putInt(value)
        val exif = "Exif\u0000\u0000".toByteArray() + tiff.array()
        return byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe1.toByte(),
            ((exif.size + 2) shr 8).toByte(), (exif.size + 2).toByte()) + exif + jpeg.copyOfRange(2, jpeg.size)
    }

    @Test fun exifGpsIsReadAsDoubleWithoutRotatingOrReencodingOriginal() {
        val source = fixture()
        val id = "gps-contract-${System.nanoTime()}"
        try {
            val bytes = source
            val imported = PhotoImporter.importStream(context, ByteArrayInputStream(bytes), "image/jpeg", id)
            assertEquals(54.70123456, imported.latitude!!, 0.0000001)
            assertEquals(20.50234567, imported.longitude!!, 0.0000001)
            assertArrayEquals(bytes, PhotoAssets.open(context, imported.path).use { it.readBytes() })
            assertEquals(6, PhotoAssets.open(context, imported.path).use { ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, 0) })
            assertFalse(File(context.filesDir, "stories/$id").exists())
        } finally { PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) } }
    }

    @Test fun selectedOriginalImportHasSafeDiagnosticReceipt() {
        val source = fixture()
        val id = "gps-diag-${System.nanoTime()}"
        try {
            val imported = PhotoImporter.importStream(context, ByteArrayInputStream(source), "image/jpeg", id)
            assertNotNull(imported.latitude)
            val record = PhotoImportTelemetry.pending(context, id)!!
            assertTrue(record.contains("gps_present"))
            assertFalse(File(context.filesDir, "stories/$id").exists())
            assertFalse(record.contains("54.701"))
            val declared = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty()
            assertTrue(declared.contains(Manifest.permission.ACCESS_MEDIA_LOCATION))
            assertFalse(declared.contains(Manifest.permission.ACCESS_FINE_LOCATION))
        } finally {
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
        }
    }

    @Test fun floatingButtonCancelsConnectingLiveEvenWithoutRecordingArchive() {
        val source = fixture()
        val id = "stop-contract-${System.nanoTime()}"
        val store = AppGraph.store(context)
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(source), "image/jpeg", id)
        store.createStory(imported)
        val live = AppGraph.live(context)
        live.stopLocal(false)
        val prefs = context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
        prefs.edit().putString("active_story_id", id).putBoolean("identity_live_attempted:$id", true).commit()
        try {
            ActivityScenario.launch<MainActivity>(Intent(context, MainActivity::class.java)).use { scenario ->
                scenario.onActivity { activity ->
                    val stateField = LiveSessionController::class.java.getDeclaredField("state").apply { isAccessible = true }
                    stateField.set(live, LiveUiState(storyId = id, connecting = true, status = "Подключаю Live…"))
                    fun find(group: ViewGroup): ImageButton? {
                        for (index in 0 until group.childCount) {
                            val child = group.getChildAt(index)
                            if (child is ImageButton && child.contentDescription == "live-mic") return child
                            if (child is ViewGroup) find(child)?.let { return it }
                        }
                        return null
                    }
                    val button = find(activity.window.decorView as ViewGroup)
                    assertNotNull(button)
                    button!!.performClick()
                    assertFalse(live.snapshot().connecting)
                    assertFalse(live.snapshot().active)
                    assertFalse(live.snapshot().inputActive)
                    assertEquals("Микрофон выключен", live.snapshot().status)
                }
                InstrumentationRegistry.getInstrumentation().waitForIdleSync()
                assertFalse(live.snapshot().active)
            }
        } finally {
            live.stopLocal(false)
            store.deleteStory(id)
            prefs.edit().remove("active_story_id").remove("identity_live_attempted:$id").commit()
        }
    }
}
