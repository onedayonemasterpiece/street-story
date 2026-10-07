package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.SystemClock
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.InputStream

object PhotoImporter {
    fun import(context: Context, uri: Uri, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        require(clientStoryId.matches(Regex("[A-Za-z0-9_-]{1,120}"))) { "Invalid story identifier" }
        val resolver = context.contentResolver
        val persistent = if (uri.scheme == "content") {
            // Persist the already selected provider grant; never enumerate or copy the gallery.
            runCatching { resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }.isSuccess
        } else false
        val mime = resolver.getType(uri) ?: "image/jpeg"
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_MEDIA_LOCATION) == PackageManager.PERMISSION_GRANTED
        val started = SystemClock.elapsedRealtime()
        var originalRequested = false
        var originalOpened = false
        var failure: String? = null
        var input: InputStream? = null
        if (granted) {
            val mediaUri = if (uri.authority == "media") uri else runCatching { MediaStore.getMediaUri(context, uri) }.getOrNull()
            if (mediaUri != null) {
                originalRequested = true
                try {
                    input = resolver.openInputStream(MediaStore.setRequireOriginal(mediaUri))
                    originalOpened = input != null
                } catch (exc: Exception) { failure = exc.javaClass.simpleName }
            }
        }
        if (input == null) input = resolver.openInputStream(uri)
        val bytes = requireNotNull(input) { "Не удалось открыть выбранное фото" }.use(::readPhotoBytes)
        val exif = runCatching { ExifInterface(ByteArrayInputStream(bytes)) }.getOrNull()
        val uploadId = newPhotoUploadId()
        // Preserve the selected URI even for a temporary share grant. Reopen it
        // after RAM expiry when the provider still grants access; RAM is fallback.
        val path = if (persistent) uri.toString() else if (uri.scheme == "content")
            PhotoAssets.retainSelectedUri(uri.toString(), bytes) else PhotoAssets.retainTemporary(bytes, uploadId)
        val photo = imported(clientStoryId, path, uploadId, mime, exif)
        val gps = photo.latitude != null && photo.longitude != null
        val hasTags = exif?.getAttribute(ExifInterface.TAG_GPS_LATITUDE) != null || exif?.getAttribute(ExifInterface.TAG_GPS_LONGITUDE) != null
        PhotoImportTelemetry.record(context, clientStoryId, mapOf(
            "status" to if (gps) "gps_present" else if (!granted) "media_location_permission_missing" else if (hasTags) "gps_redacted_or_invalid" else "gps_missing",
            "read_mode" to if (originalOpened) "require_original" else "selected_uri",
            "media_location_granted" to granted, "original_requested" to originalRequested,
            "original_opened" to originalOpened, "fallback_reason" to failure,
            "provider_kind" to if (uri.authority == "media") "media" else "document_or_cloud",
            "persistent_uri_grant" to persistent,
            "gps_present" to gps,
            "gps_lat_tag" to (exif?.getAttribute(ExifInterface.TAG_GPS_LATITUDE) != null),
            "gps_lon_tag" to (exif?.getAttribute(ExifInterface.TAG_GPS_LONGITUDE) != null),
            "exif_orientation" to exif?.getAttributeInt(ExifInterface.TAG_ORIENTATION, 1),
            "photo_sha256" to photo.sha256, "photo_identity_kind" to "opaque_upload_id", "photo_bytes" to bytes.size,
            "mime_type" to mime, "elapsed_ms" to (SystemClock.elapsedRealtime() - started),
        ))
        return photo
    }

    /** For in-memory fixtures/intake; production gallery import keeps its selected URI. */
    fun importStream(context: Context, input: InputStream, mimeType: String, clientStoryId: String = newClientStoryId()): ImportedPhoto {
        require(clientStoryId.matches(Regex("[A-Za-z0-9_-]{1,120}"))) { "Invalid story identifier" }
        val bytes = readPhotoBytes(input)
        val id = newPhotoUploadId()
        val exif = runCatching { ExifInterface(ByteArrayInputStream(bytes)) }.getOrNull()
        val photo = imported(clientStoryId, PhotoAssets.retainTemporary(bytes, id), id, mimeType, exif)
        PhotoImportTelemetry.record(context, clientStoryId, mapOf(
            "status" to if (photo.latitude != null && photo.longitude != null) "gps_present" else "gps_missing",
            "read_mode" to "temporary_ram", "gps_present" to (photo.latitude != null && photo.longitude != null),
            "photo_identity_kind" to "opaque_upload_id", "photo_sha256" to id, "photo_bytes" to bytes.size,
            "mime_type" to mimeType,
        ))
        return photo
    }

    private fun imported(clientStoryId: String, path: String, id: String, mime: String, exif: ExifInterface?): ImportedPhoto {
        val coords = exif?.latLong?.takeIf {
            it.size == 2 && it[0].isFinite() && it[1].isFinite() && it[0] in -90.0..90.0 && it[1] in -180.0..180.0
        }
        return ImportedPhoto(clientStoryId, path, id, mime, coords?.get(0), coords?.get(1))
    }
}
