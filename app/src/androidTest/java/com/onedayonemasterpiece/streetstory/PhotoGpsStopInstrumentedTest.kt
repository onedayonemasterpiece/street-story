package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
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
import java.io.File

@RunWith(AndroidJUnit4::class)
class PhotoGpsStopInstrumentedTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    private fun fixture(): File {
        val source = File(context.cacheDir, "gps-original-${System.nanoTime()}.jpg")
        val bitmap = Bitmap.createBitmap(24, 32, Bitmap.Config.ARGB_8888)
        source.outputStream().use { bitmap.compress(Bitmap.CompressFormat.JPEG, 90, it) }
        bitmap.recycle()
        ExifInterface(source).apply {
            setLatLong(54.70123456, 20.50234567)
            setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString())
            saveAttributes()
        }
        return source
    }

    @Test fun exifGpsIsReadAsDoubleWithoutRotatingOrReencodingOriginal() {
        val source = fixture()
        val id = "gps-contract-${System.nanoTime()}"
        try {
            val bytes = source.readBytes()
            val imported = PhotoImporter.importStream(context, ByteArrayInputStream(bytes), "image/jpeg", id)
            assertEquals(54.70123456, imported.latitude!!, 0.0000001)
            assertEquals(20.50234567, imported.longitude!!, 0.0000001)
            assertArrayEquals(bytes, File(imported.path).readBytes())
            assertEquals(6, ExifInterface(File(imported.path)).getAttributeInt(ExifInterface.TAG_ORIENTATION, 0))
        } finally { source.delete(); File(context.filesDir, "stories/$id").deleteRecursively() }
    }

    @Test fun selectedOriginalImportHasSafeDiagnosticReceipt() {
        val source = fixture()
        val id = "gps-diag-${System.nanoTime()}"
        try {
            val imported = PhotoImporter.import(context, Uri.fromFile(source), id)
            assertNotNull(imported.latitude)
            val record = PhotoImportTelemetry.pending(context, id)!!
            assertTrue(record.contains("gps_present"))
            assertFalse(record.contains(source.absolutePath))
            assertFalse(record.contains("54.701"))
            val declared = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty()
            assertTrue(declared.contains(Manifest.permission.ACCESS_MEDIA_LOCATION))
            assertFalse(declared.contains(Manifest.permission.ACCESS_FINE_LOCATION))
        } finally {
            source.delete(); File(context.filesDir, "stories/$id").deleteRecursively()
            PhotoImportTelemetry.pending(context, id)?.let { PhotoImportTelemetry.acknowledge(context, id, it) }
        }
    }

    @Test fun floatingButtonCancelsConnectingLiveEvenWithoutRecordingArchive() {
        val source = fixture()
        val id = "stop-contract-${System.nanoTime()}"
        val store = AppGraph.store(context)
        val imported = source.inputStream().use { PhotoImporter.importStream(context, it, "image/jpeg", id) }
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
            source.delete()
            prefs.edit().remove("active_story_id").remove("identity_live_attempted:$id").commit()
        }
    }
}
