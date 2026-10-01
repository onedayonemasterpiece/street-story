package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.SystemClock
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import androidx.exifinterface.media.ExifInterface
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest

object PhotoImporter {
    private const val MAX_BYTES = 16 * 1024 * 1024

    fun import(context: Context, uri: Uri, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        val resolver = context.contentResolver
        val mime = resolver.getType(uri) ?: "image/jpeg"
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED
        val started = SystemClock.elapsedRealtime()
        var originalRequested = false
        var originalOpened = false
        var failure: String? = null
        var input: InputStream? = null
        if (granted) {
            // Framework conversion retains the selected-file grant. Never parse
            // IDs or query the whole gallery to bypass provider access controls.
            val mediaUri = if (uri.authority == "media") uri else runCatching { MediaStore.getMediaUri(context, uri) }.getOrNull()
            if (mediaUri != null) {
                originalRequested = true
                try {
                    input = resolver.openInputStream(MediaStore.setRequireOriginal(mediaUri))
                    originalOpened = input != null
                } catch (exc: Exception) {
                    failure = exc.javaClass.simpleName
                }
            }
        }
        if (input == null) input = resolver.openInputStream(uri)
        val photo = requireNotNull(input) { "Не удалось открыть выбранное фото" }.use {
            importStream(context, it, mime, clientStoryId)
        }
        val exif = runCatching { ExifInterface(File(photo.path)) }.getOrNull()
        val gps = photo.latitude != null && photo.longitude != null
        val hasTags = exif?.getAttribute(ExifInterface.TAG_GPS_LATITUDE) != null || exif?.getAttribute(ExifInterface.TAG_GPS_LONGITUDE) != null
        PhotoImportTelemetry.record(context, clientStoryId, mapOf(
            "status" to if (gps) "gps_present" else if (!granted) "media_location_permission_missing" else if (hasTags) "gps_redacted_or_invalid" else "gps_missing",
            "read_mode" to if (originalOpened) "require_original" else "selected_uri",
            "media_location_granted" to granted, "original_requested" to originalRequested,
            "original_opened" to originalOpened, "fallback_reason" to failure,
            "provider_kind" to if (uri.authority == "media") "media" else "document_or_cloud",
            "gps_present" to gps,
            "gps_lat_tag" to (exif?.getAttribute(ExifInterface.TAG_GPS_LATITUDE) != null),
            "gps_lon_tag" to (exif?.getAttribute(ExifInterface.TAG_GPS_LONGITUDE) != null),
            "exif_orientation" to exif?.getAttributeInt(ExifInterface.TAG_ORIENTATION, 1),
            "photo_sha256" to photo.sha256, "photo_bytes" to File(photo.path).length(),
            "mime_type" to mime, "elapsed_ms" to (SystemClock.elapsedRealtime() - started),
        ))
        return photo
    }

    fun importStream(context: Context, input: InputStream, mimeType: String, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        require(clientStoryId.matches(Regex("[A-Za-z0-9_-]{1,120}"))) { "Invalid story identifier" }
        val ext = when (mimeType) { "image/png" -> "png"; "image/webp" -> "webp"; "image/heic", "image/heif" -> "heic"; else -> "jpg" }
        val dir = File(context.filesDir, "stories/$clientStoryId").apply { mkdirs() }
        val part = File(dir, "photo.$ext.part")
        val target = File(dir, "photo.$ext")
        require(!target.exists()) { "Фото темы уже сохранено" }
        val digest = MessageDigest.getInstance("SHA-256")
        try {
            var total = 0
            FileOutputStream(part).use { out ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    total += count
                    require(total <= MAX_BYTES) { "Фото больше 16 МБ" }
                    out.write(buffer, 0, count); digest.update(buffer, 0, count)
                }
                require(total > 0) { "Пустой файл фото" }
                out.fd.sync()
            }
            check(part.renameTo(target)) { "Не удалось сохранить выбранное фото" }
        } catch (exc: Exception) {
            part.delete()
            throw exc
        }
        val coords = runCatching { ExifInterface(target).latLong }.getOrNull()?.takeIf {
            it.size == 2 && it[0].isFinite() && it[1].isFinite() && it[0] in -90.0..90.0 && it[1] in -180.0..180.0
        }
        return ImportedPhoto(clientStoryId, target.absolutePath,
            digest.digest().joinToString("") { "%02x".format(it) }, mimeType,
            coords?.get(0), coords?.get(1))
    }
}
