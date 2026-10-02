package com.onedayonemasterpiece.streetstory

import android.content.Context
import com.google.gson.Gson
import java.util.UUID

/** Only import diagnostics, never the selected content URI or actual coordinates. */
object PhotoImportTelemetry {
    private val gson = Gson()
    private fun prefs(context: Context) = context.getSharedPreferences("street_story_photo_import", Context.MODE_PRIVATE)

    fun record(context: Context, storyId: String, fields: Map<String, Any?>) {
        val payload = linkedMapOf<String, Any?>(
            "event_id" to UUID.randomUUID().toString(),
            "app_version" to BuildConfig.VERSION_NAME,
            "source_sha" to BuildConfig.SOURCE_SHA,
        ).apply { putAll(fields) }
        prefs(context).edit().putString(storyId, gson.toJson(payload)).apply()
    }

    fun pending(context: Context, storyId: String): String? = prefs(context).getString(storyId, null)

    @Synchronized fun acknowledge(context: Context, storyId: String, sent: String) {
        if (pending(context, storyId) == sent) prefs(context).edit().remove(storyId).apply()
    }
}
