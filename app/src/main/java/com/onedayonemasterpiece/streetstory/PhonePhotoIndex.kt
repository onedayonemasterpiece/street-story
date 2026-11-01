package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.database.ContentObserver
import android.location.Location
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.exifinterface.media.ExifInterface
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.atan2
import kotlin.math.abs

/** Camera coordinates in the original EXIF, never the phone's current location. */
data class PhotoPoint(val lat: Double, val lon: Double)

data class PhonePhoto(
    val mediaId: Long,
    val uri: String,
    val capturedAt: Long,
    val latitude: Double,
    val longitude: Double,
)

data class PhonePhotoVersion(val modified: Long, val size: Long, val gpsState: String) {
    val hasGps: Boolean get() = gpsState == "gps"
}

data class LocalStoryPhoto(val photoId: String, val uri: String, val position: Int)

data class IndexProgress(val processed: Int, val total: Int, val withGps: Int, val running: Boolean)

object PhonePhotoIndex {
    const val ACTION_UPDATED = "com.onedayonemasterpiece.streetstory.PHONE_PHOTO_INDEX"
    @Volatile var progress = IndexProgress(0, 0, 0, false)
        private set

    fun galleryGranted(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_IMAGES
            else Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED ||
            (Build.VERSION.SDK_INT >= 34 &&
                ContextCompat.checkSelfPermission(context, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED) == PackageManager.PERMISSION_GRANTED)

    fun partialAccess(context: Context): Boolean = Build.VERSION.SDK_INT >= 34 &&
        ContextCompat.checkSelfPermission(context, Manifest.permission.READ_MEDIA_IMAGES) != PackageManager.PERMISSION_GRANTED &&
        ContextCompat.checkSelfPermission(context, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED) == PackageManager.PERMISSION_GRANTED

    /** Immediate at app open / permission grant; observer batches camera bursts for five seconds. */
    fun enqueue(context: Context, delaySeconds: Long = 0L) {
        if (!galleryGranted(context)) return
        val request = OneTimeWorkRequestBuilder<PhonePhotoIndexWorker>()
            .apply { if (delaySeconds > 0) setInitialDelay(delaySeconds, TimeUnit.SECONDS) }
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            "street-story-phone-index-now", ExistingWorkPolicy.REPLACE, request,
        )
    }

    fun start(context: Context) {
        if (!galleryGranted(context)) return
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "street-story-phone-index-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<PhonePhotoIndexWorker>(6, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiresBatteryNotLow(true).build())
                .build(),
        )
    }

    /** Only while the process is alive. MediaStore bursts coalesce into one delayed scan. */
    fun observe(context: Context): ContentObserver = object : ContentObserver(Handler(Looper.getMainLooper())) {
        override fun onChange(selfChange: Boolean, uri: Uri?) { enqueue(context, 5) }
        override fun onChange(selfChange: Boolean) { enqueue(context, 5) }
    }.also {
        context.contentResolver.registerContentObserver(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, true, it)
    }

    internal fun report(context: Context, processed: Int, total: Int, gps: Int, running: Boolean) {
        progress = IndexProgress(processed, total, gps, running)
        context.sendBroadcast(Intent(ACTION_UPDATED).setPackage(context.packageName))
    }

    /**
     * Full-circle bounding rectangles in SQLite, then precise great-circle distance.
     * No LIMIT or pagination before filtering. Two circles form a union, not an intersection.
     */
    fun nearby(store: StoryStore, camera: PhotoPoint?, objectPoint: PhotoPoint?): List<PhonePhoto> {
        val centers = listOfNotNull(camera, objectPoint).distinct()
        if (centers.isEmpty()) return emptyList()
        val matched = linkedMapOf<Long, PhonePhoto>()
        val distance = FloatArray(1)
        for (center in centers) {
            for (photo in store.photosInBoundingBox(center, 150.0)) {
                Location.distanceBetween(center.lat, center.lon, photo.latitude, photo.longitude, distance)
                if (distance[0] <= 150.0f) matched[photo.mediaId] = photo
            }
        }
        val midpoint = if (centers.size == 1) centers.first() else {
            val meanLon = Math.toDegrees(atan2(
                centers.sumOf { sin(Math.toRadians(it.lon)) },
                centers.sumOf { cos(Math.toRadians(it.lon)) },
            ))
            PhotoPoint(centers.map { it.lat }.average(), meanLon)
        }
        return matched.values.sortedWith(compareBy<PhonePhoto> {
            Location.distanceBetween(midpoint.lat, midpoint.lon, it.latitude, it.longitude, distance)
            distance[0]
        }.thenBy { it.mediaId })
    }

    /** The longitude radius grows toward the poles, so the rectangle never clips the circle. */
    fun bounds(center: PhotoPoint, radiusMeters: Double): DoubleArray {
        val deltaLat = radiusMeters / 110_000.0 * 1.001
        val furthestAbsLat = (abs(center.lat) + deltaLat).coerceAtMost(89.999999)
        val deltaLon = (radiusMeters / (110_000.0 * cos(Math.toRadians(furthestAbsLat)))).coerceAtMost(180.0) * 1.001
        return doubleArrayOf(
            (center.lat - deltaLat).coerceAtLeast(-90.0),
            (center.lat + deltaLat).coerceAtMost(90.0),
            center.lon - deltaLon,
            center.lon + deltaLon,
        )
    }
}

class PhonePhotoIndexWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val context = applicationContext
        if (!PhonePhotoIndex.galleryGranted(context)) return@withContext Result.success()
        val store = AppGraph.store(context)
        val versions = store.phonePhotoVersions()
        val seen = HashSet<Long>()
        val grantedLocation = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_MEDIA_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        val projection = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DATE_MODIFIED,
            MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.MIME_TYPE,
        )
        try {
            val cursor = context.contentResolver.query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI, projection, null, null, null,
            ) ?: return@withContext Result.retry()
            cursor.use {
                val total = it.count
                var processed = 0
                var exifOpened = 0
                val startedAt = android.os.SystemClock.elapsedRealtime()
                var gps = versions.values.count { v -> v.hasGps }
                var lastReportedAt = android.os.SystemClock.elapsedRealtime()
                PhonePhotoIndex.report(context, 0, total, gps, true)
                while (it.moveToNext()) {
                    if (isStopped) return@withContext Result.retry()
                    val id = it.getLong(0)
                    val modified = it.getLong(1)
                    val size = it.getLong(2)
                    val taken = if (it.isNull(3)) modified * 1_000L else it.getLong(3)
                    val mime = if (it.isNull(4)) "image/jpeg" else it.getString(4)
                    seen.add(id)
                    val prior = versions[id]
                    if (prior == null || prior.modified != modified || prior.size != size ||
                        prior.gpsState == "retry" || (prior.gpsState == "hidden" && grantedLocation)) {
                        val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
                        var coords: DoubleArray? = null
                        var state = if (grantedLocation) "none" else "hidden"
                        try {
                            val original = if (grantedLocation) MediaStore.setRequireOriginal(uri) else uri
                            exifOpened++
                            context.contentResolver.openInputStream(original).use { stream ->
                                requireNotNull(stream) { "No readable photo stream" }
                                coords = ExifInterface(stream).latLong?.takeIf { pair ->
                                    pair.size == 2 && pair.all { n -> n.isFinite() } &&
                                        pair[0] in -90.0..90.0 && pair[1] in -180.0..180.0
                                }
                            }
                            if (coords != null) state = "gps"
                        } catch (exc: Exception) {
                            // One inaccessible file must not abort the whole gallery.
                            Log.w("PhonePhotoIndex", "EXIF failed for media_id=$id", exc)
                            state = "retry"
                        }
                        store.upsertPhonePhoto(id, modified, size, taken, mime, coords, state)
                        if (prior?.hasGps == true) gps--
                        if (coords != null) gps++
                    }
                    processed++
                    if (processed == total ||
                        (processed % 100 == 0 &&
                         android.os.SystemClock.elapsedRealtime() - lastReportedAt >= 2_000L)) {
                        PhonePhotoIndex.report(context, processed, total, gps, true)
                        lastReportedAt = android.os.SystemClock.elapsedRealtime()
                    }
                }
                store.removeMissingPhonePhotos(versions.keys - seen)
                Log.i("PhonePhotoIndex", "scan_complete photos=$total exif_opened=$exifOpened elapsed_ms=${android.os.SystemClock.elapsedRealtime() - startedAt}")
                PhonePhotoIndex.report(context, total, total, store.phonePhotoGpsCount(), false)
            }
            Result.success()
        } catch (exc: SecurityException) {
            Log.i("PhonePhotoIndex", "Gallery permission changed", exc)
            PhonePhotoIndex.report(context, 0, 0, store.phonePhotoGpsCount(), false)
            Result.success()
        } catch (exc: Exception) {
            Log.e("PhonePhotoIndex", "Full MediaStore reconciliation failed", exc)
            PhonePhotoIndex.report(context, 0, 0, store.phonePhotoGpsCount(), false)
            Result.retry()
        }
    }
}
