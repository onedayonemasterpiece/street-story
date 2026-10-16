package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.graphics.Bitmap
import java.io.File
import java.io.InputStream
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface

/**
 * Decodes the stored photo exactly as the user expects to see it.
 *
 * Photo Picker frequently returns JPEG pixels in sensor orientation plus an
 * EXIF orientation tag. BitmapFactory deliberately doesn't apply that tag.
 * Keep the original bytes untouched for upload; normalize only
 * the UI bitmap at decode time.
 */
internal object ImagePreviewDecoder {
    fun decode(path: String, targetWidth: Int, targetHeight: Int): Bitmap? =
        decodeStream({ File(path).inputStream() }, targetWidth, targetHeight)

    fun decode(context: Context, path: String, targetWidth: Int, targetHeight: Int): Bitmap? =
        decodeStream({ PhotoAssets.open(context, path) }, targetWidth, targetHeight)

    fun decodePhotoFormat(context: Context, path: String, targetWidth: Int, targetHeight: Int,
                          format: PhotoFormatWire?): Bitmap? {
        val bitmap = decode(context, path, targetWidth, targetHeight) ?: return null
        if (format?.mode != "crop_4_5") return bitmap
        // Without an author crop, the model composes 4:5 from this whole frame.
        val rect = format.cropRect ?: return bitmap
        val full = format.orientedSize
        val requested = if (full?.size == 2 && rect.size == 4 && full[0] > 0 && full[1] > 0) {
            val x = (rect[0].toDouble() * bitmap.width / full[0]).toInt()
            val y = (rect[1].toDouble() * bitmap.height / full[1]).toInt()
            val width = (rect[2].toDouble() * bitmap.width / full[0]).toInt()
            val height = (rect[3].toDouble() * bitmap.height / full[1]).toInt()
            intArrayOf(x, y, width, height)
        } else intArrayOf(0, 0, bitmap.width, bitmap.height)
        val unit = minOf(requested[2] / 4, requested[3] / 5)
        if (unit < 1) return bitmap
        val width = unit * 4
        val height = unit * 5
        val x = (requested[0] + (requested[2] - width) / 2).coerceIn(0, bitmap.width - width)
        val y = (requested[1] + (requested[3] - height) / 2).coerceIn(0, bitmap.height - height)
        return Bitmap.createBitmap(bitmap, x, y, width, height).also { if (it !== bitmap) bitmap.recycle() }
    }

    private fun decodeStream(open: () -> InputStream, targetWidth: Int, targetHeight: Int): Bitmap? =
        runCatching {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            open().use { BitmapFactory.decodeStream(it, null, bounds) }
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return@runCatching null

            val orientation = runCatching {
                open().use { ExifInterface(it).getAttributeInt(
                    ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL) }
            }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)
            val swapsAxes = orientation in setOf(
                ExifInterface.ORIENTATION_TRANSPOSE,
                ExifInterface.ORIENTATION_ROTATE_90,
                ExifInterface.ORIENTATION_TRANSVERSE,
                ExifInterface.ORIENTATION_ROTATE_270,
            )
            val sourceWidth = if (swapsAxes) bounds.outHeight else bounds.outWidth
            val sourceHeight = if (swapsAxes) bounds.outWidth else bounds.outHeight
            var sample = 1
            while (
                sourceWidth / sample > targetWidth.coerceAtLeast(1) * 2 ||
                sourceHeight / sample > targetHeight.coerceAtLeast(1) * 2
            ) {
                sample *= 2
            }
            val decoded = open().use { BitmapFactory.decodeStream(it, null,
                BitmapFactory.Options().apply { inSampleSize = sample.coerceAtLeast(1) })
            } ?: return@runCatching null

            val matrix = Matrix()
            when (orientation) {
                ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.setScale(-1f, 1f)
                ExifInterface.ORIENTATION_ROTATE_180 -> matrix.setRotate(180f)
                ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.setScale(1f, -1f)
                ExifInterface.ORIENTATION_TRANSPOSE -> {
                    matrix.setRotate(90f)
                    matrix.postScale(-1f, 1f)
                }
                ExifInterface.ORIENTATION_ROTATE_90 -> matrix.setRotate(90f)
                ExifInterface.ORIENTATION_TRANSVERSE -> {
                    matrix.setRotate(-90f)
                    matrix.postScale(-1f, 1f)
                }
                ExifInterface.ORIENTATION_ROTATE_270 -> matrix.setRotate(-90f)
            }
            if (matrix.isIdentity) {
                decoded
            } else {
                Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, matrix, true)
                    .also { if (it !== decoded) decoded.recycle() }
            }
        }.getOrNull()
}
