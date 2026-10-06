package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream

/** URI grants survive restart; image bytes are never copied into app storage. */
internal object PhotoAssets {
    private const val PREFIX = "ram-photo:"
    private const val MAX_CACHE_BYTES = 64 * 1024 * 1024
    private const val TTL_MS = 15 * 60 * 1000L
    private data class Entry(val bytes: ByteArray, val expiresAt: Long)
    private val temporary = linkedMapOf<String, Entry>()

    @Synchronized fun retainTemporary(bytes: ByteArray, id: String = newPhotoUploadId()): String {
        require(bytes.isNotEmpty() && bytes.size <= 16 * 1024 * 1024)
        evictExpired()
        while (temporary.isNotEmpty() && temporary.values.sumOf { it.bytes.size } + bytes.size > MAX_CACHE_BYTES)
            temporary.remove(temporary.keys.first())
        val path = PREFIX + id
        temporary[path] = Entry(bytes, System.currentTimeMillis() + TTL_MS)
        return path
    }

    @Synchronized fun releaseTemporary(path: String) { temporary.remove(path) }

    @Synchronized fun available(path: String): Boolean {
        evictExpired()
        return when {
            path.startsWith(PREFIX) -> temporary.containsKey(path)
            path.startsWith("content:") -> true
            else -> File(if (path.startsWith("file:")) requireNotNull(Uri.parse(path).path) else path).isFile
        }
    }

    private fun evictExpired() {
        val now = System.currentTimeMillis()
        temporary.entries.removeAll { it.value.expiresAt <= now }
    }

    fun open(context: Context, path: String): InputStream {
        if (path.startsWith(PREFIX)) return synchronized(this) {
            evictExpired()
            ByteArrayInputStream(requireNotNull(temporary[path]) { "Временное изображение уже недоступно" }.bytes)
        }
        val uri = Uri.parse(path)
        if (uri.scheme == "content") {
            val resolver = context.contentResolver
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED) {
                val mediaUri = if (uri.authority == "media") uri else runCatching { MediaStore.getMediaUri(context, uri) }.getOrNull()
                if (mediaUri != null) {
                    runCatching { resolver.openInputStream(MediaStore.setRequireOriginal(mediaUri)) }.getOrNull()?.let { return it }
                }
            }
            return requireNotNull(resolver.openInputStream(uri)) { "Не удалось открыть выбранное фото" }
        }
        return File(if (uri.scheme == "file") requireNotNull(uri.path) else path).inputStream()
    }
}
