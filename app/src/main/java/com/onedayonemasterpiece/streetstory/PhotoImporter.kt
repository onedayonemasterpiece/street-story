package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest

object PhotoImporter {
    fun import(context: Context, uri: Uri, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        val resolver = context.contentResolver
        val mime = resolver.getType(uri) ?: "image/jpeg"
        return resolver.openInputStream(uri).use { input ->
            requireNotNull(input) { "Photo Picker did not expose the selected photo" }
            importStream(context, input, mime, clientStoryId)
        }
    }

    fun importStream(context: Context, input: InputStream, mimeType: String, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        val ext = when (mimeType) { "image/png" -> "png"; "image/webp" -> "webp"; "image/heic", "image/heif" -> "heic"; else -> "jpg" }
        val dir = File(context.filesDir, "stories/$clientStoryId").apply { mkdirs() }
        val part = File(dir, "photo.$ext.part")
        val target = File(dir, "photo.$ext")
        part.delete(); target.delete()
        val digest = MessageDigest.getInstance("SHA-256")
        FileOutputStream(part).use { out ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) { val count = input.read(buffer); if (count < 0) break; out.write(buffer, 0, count); digest.update(buffer, 0, count) }
            out.fd.sync()
        }
        check(part.renameTo(target) || runCatching { part.copyTo(target, overwrite = true); part.delete(); true }.getOrDefault(false))
        val coords = runCatching {
            val result = FloatArray(2)
            @Suppress("DEPRECATION")
            if (ExifInterface(target).getLatLong(result)) result else null
        }.getOrNull()
        return ImportedPhoto(clientStoryId, target.absolutePath, digest.digest().joinToString("") { "%02x".format(it) }, mimeType,
            coords?.get(0)?.toDouble(), coords?.get(1)?.toDouble())
    }
}
