package com.onedayonemasterpiece.streetstory

import android.content.Context

class RecordingRuntime(context: Context) {
    private val preferences = context.getSharedPreferences("street_story_recording_runtime", Context.MODE_PRIVATE)
    fun update(sessionId: String, durationMs: Long, wallElapsedMs: Long, autoSilenceSkippedMs: Long, captureActivity: String) {
        preferences.edit().putString("session_id", sessionId).putLong("duration_ms", durationMs.coerceAtLeast(0L))
            .putLong("wall_elapsed_ms", wallElapsedMs.coerceAtLeast(0L)).putLong("auto_silence_skipped_ms", autoSilenceSkippedMs.coerceAtLeast(0L))
            .putString("capture_activity", captureActivity).apply()
    }
    fun snapshotFor(sessionId: String): RuntimeSnapshot? = if (preferences.getString("session_id", null) == sessionId) RuntimeSnapshot(
        preferences.getLong("duration_ms", 0L), preferences.getLong("wall_elapsed_ms", 0L), preferences.getLong("auto_silence_skipped_ms", 0L),
        preferences.getString("capture_activity", CaptureActivity.IDLE) ?: CaptureActivity.IDLE) else null
    fun clear(sessionId: String) { if (preferences.getString("session_id", null) == sessionId) preferences.edit().clear().apply() }
}
