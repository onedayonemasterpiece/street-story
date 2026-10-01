package com.onedayonemasterpiece.streetstory

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface

/**
 * Decodes the stored photo exactly as the user expects to see it.
 *
 * Photo Picker frequently returns JPEG pixels in sensor orientation plus an
 * EXIF orientation tag. BitmapFactory deliberately doesn't apply that tag.
 * Keep the original bytes untouched for upload/hash identity; normalize only
 * the UI bitmap at decode time.
 */
internal object ImagePreviewDecoder {
    fun decode(path: String, targetWidth: Int, targetHeight: Int): Bitmap? =
        runCatching {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(path, bounds)
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return@runCatching null

            val orientation = runCatching {
                ExifInterface(path).getAttributeInt(
                    ExifInterface.TAG_ORIENTATION,
                    ExifInterface.ORIENTATION_NORMAL,
                )
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
            val decoded = BitmapFactory.decodeFile(
                path,
                BitmapFactory.Options().apply { inSampleSize = sample.coerceAtLeast(1) },
            ) ?: return@runCatching null

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
