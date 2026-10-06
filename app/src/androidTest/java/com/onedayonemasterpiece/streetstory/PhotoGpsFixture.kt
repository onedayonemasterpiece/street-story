package com.onedayonemasterpiece.streetstory

import android.graphics.Bitmap
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

internal object PhotoGpsFixture {
    fun bytes(): ByteArray {
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

}
