package com.onedayonemasterpiece.streetstory

import android.graphics.Bitmap
import androidx.exifinterface.media.ExifInterface
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.io.FileOutputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ImagePreviewInstrumentedTest {
    @Test
    fun chosenCropShowsSourcePixelsAndOriginalRestoresWholePhoto() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val file = File(context.cacheDir, "photo-format-preview.png")
        val source = Bitmap.createBitmap(80, 60, Bitmap.Config.ARGB_8888)
        for (x in 0 until 80) for (y in 0 until 60) source.setPixel(x, y, android.graphics.Color.rgb(x * 3, y * 4, 20))
        FileOutputStream(file).use { source.compress(Bitmap.CompressFormat.PNG, 100, it) }
        val before = file.readBytes()
        val crop = PhotoFormatWire().apply { mode = "crop_4_5"; orientedSize = listOf(80, 60); cropRect = listOf(16, 0, 48, 60) }
        val cropped = ImagePreviewDecoder.decodePhotoFormat(context, file.absolutePath, 100, 100, crop)!!
        assertEquals(48, cropped.width)
        assertEquals(60, cropped.height)
        assertEquals(source.getPixel(16, 0), cropped.getPixel(0, 0))
        assertEquals(source.getPixel(63, 59), cropped.getPixel(47, 59))
        val original = ImagePreviewDecoder.decodePhotoFormat(context, file.absolutePath, 100, 100, PhotoFormatWire())!!
        assertEquals(80, original.width)
        assertEquals(60, original.height)
        org.junit.Assert.assertArrayEquals(before, file.readBytes())
        source.recycle(); cropped.recycle(); original.recycle(); file.delete()
    }

    @Test
    fun exifRotationIsAppliedWithoutChangingStoredPhoto() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val file = File(context.cacheDir, "orientation-preview-test.jpg")
        val bitmap = Bitmap.createBitmap(80, 40, Bitmap.Config.ARGB_8888)
        FileOutputStream(file).use { bitmap.compress(Bitmap.CompressFormat.JPEG, 92, it) }
        bitmap.recycle()
        ExifInterface(file).apply {
            setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString())
            saveAttributes()
        }

        val decoded = ImagePreviewDecoder.decode(file.absolutePath, 400, 400)
        assertNotNull(decoded)
        assertEquals(40, decoded!!.width)
        assertEquals(80, decoded.height)
        assertEquals(
            ExifInterface.ORIENTATION_ROTATE_90,
            ExifInterface(file).getAttributeInt(
                ExifInterface.TAG_ORIENTATION,
                ExifInterface.ORIENTATION_NORMAL,
            ),
        )
        decoded.recycle()
        file.delete()
    }
}