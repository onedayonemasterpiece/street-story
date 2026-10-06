package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts.PickMultipleVisualMedia
import androidx.activity.result.contract.ActivityResultContracts.PickVisualMedia
import androidx.core.content.IntentCompat

/** Intake is list-shaped for a future multi-photo story; today's story uses one image. */
internal object PhotoIntake {
    const val CURRENT_LIMIT = 1
    const val FUTURE_LIMIT = 9

    fun pickerIntent(context: Context, limit: Int = CURRENT_LIMIT): Intent {
        require(limit in 1..FUTURE_LIMIT)
        val request = PickVisualMediaRequest(PickVisualMedia.ImageOnly)
        return (if (limit == 1) PickVisualMedia().createIntent(context, request)
        else PickMultipleVisualMedia(limit).createIntent(context, request)).apply {
            // New picker modules can ask the owner to include EXIF location. Older
            // modules ignore this extra; keep the importer's original-URI path too.
            putExtra("android.provider.extra.REQUEST_LOCATION_METADATA_ACCESS", true)
        }
    }

    fun sharedPhotos(intent: Intent): List<Uri> {
        if (intent.action != Intent.ACTION_SEND || intent.type?.startsWith("image/") != true) return emptyList()
        val uri = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java)
            ?: intent.clipData?.takeIf { it.itemCount == 1 }?.getItemAt(0)?.uri
            ?: return emptyList()
        return checked(listOf(uri))
    }

    fun pickedPhotos(data: Intent?): List<Uri> {
        if (data == null) return emptyList()
        val uris = data.clipData?.let { clip -> List(clip.itemCount) { clip.getItemAt(it).uri } }
            ?: listOfNotNull(data.data)
        return checked(uris)
    }

    fun checked(uris: List<Uri>, limit: Int = CURRENT_LIMIT): List<Uri> {
        require(limit in 1..FUTURE_LIMIT)
        require(uris.size <= limit) { "Сейчас выберите одно фото" }
        require(uris.all { it.scheme == "content" }) { "Галерея не предоставила доступ к фото" }
        // No image/URI deduplication: a new owner action can tell another story.
        return uris
    }
}
