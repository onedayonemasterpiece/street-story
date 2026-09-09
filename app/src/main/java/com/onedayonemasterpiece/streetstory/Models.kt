package com.onedayonemasterpiece.streetstory

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.UUID

object StoryStage {
    const val PHOTO_READY = "photo_ready"
    const val RECORDING = "recording"
    const val QUEUED = "queued"
    const val RESEARCHING = "researching"
    const val REVIEW = "review"
    const val VISUAL_PROCESSING = "visual_processing"
    const val VISUAL_BLOCKED = "visual_blocked"
    const val READY_TO_PUBLISH = "ready_to_publish"
    const val SCHEDULING = "scheduling"
    const val SCHEDULED = "scheduled"
    const val PUBLISHED = "published"
    const val NEEDS_REVIEW = "needs_review"
}

object RecordingKind {
    const val INITIAL = "initial"
    const val REFINEMENT = "refinement"
}

object CaptureState {
    const val RECORDING = "recording"
    const val PAUSED = "paused"
    const val FINISHED = "finished"
    const val DISCARDED = "discarded"
}

object CaptureActivity {
    const val IDLE = "idle"
    const val VOICE = "voice"
    const val AUTO_SILENCE = "auto_silence"
    const val MANUAL_PAUSE = "manual_pause"
    const val FALLBACK_CONTINUOUS = "fallback_continuous"
}

object CapturePolicy {
    const val VOICE_ACTIVITY_AUTO_PAUSE_V1 = "voice_activity_auto_pause_v1"
}

object VoiceRemoteState {
    const val LOCAL_ONLY = "local_only"
    const val RECEIVING = "receiving"
    const val COMPLETE = "complete"
    const val RECONCILIATION_REQUIRED = "reconciliation_required"
}

object AudioProfile {
    const val MIME_M4A = "audio/mp4"
    const val CONTAINER = "mp4"
    const val CODEC = "aac_lc"
    const val SAMPLE_RATE_HZ = 16_000
    const val CHANNELS = 1
    const val BITRATE_BPS = 32_000
}

data class ImportedPhoto(
    val clientStoryId: String,
    val path: String,
    val sha256: String,
    val mimeType: String,
    val latitude: Double?,
    val longitude: Double?,
)

data class StorySnapshot(
    val clientStoryId: String,
    val serverStoryId: String?,
    val createdAt: Long,
    val photoPath: String,
    val photoSha256: String,
    val photoMimeType: String,
    val latitude: Double?,
    val longitude: Double?,
    val stage: String,
    val placeName: String?,
    val summary: String?,
    val draftText: String?,
    val processedImagePath: String?,
    val processedImageUrl: String?,
    val scheduledFor: String?,
    val publishedAt: String?,
    val lastError: String?,
    val backendRevision: Int,
)

data class VoiceSessionSnapshot(
    val sessionId: String,
    val storyId: String,
    val kind: String,
    val startedAt: String,
    val endedAt: String?,
    val timezone: String,
    val deviceLabel: String,
    val durationMs: Long,
    val wallElapsedMs: Long,
    val manualPauseMs: Long,
    val autoSilenceSkippedMs: Long,
    val chunkCount: Int,
    val captureState: String,
    val captureActivity: String,
    val capturePolicy: String,
    val vadEngine: String?,
    val remoteState: String,
    val serverInitialized: Boolean,
    val completeSent: Boolean,
    val lastError: String?,
    val createdAt: Long,
)

data class ChunkRecord(
    val sessionId: String,
    val chunkIndex: Int,
    val startMs: Long,
    val endMs: Long,
    val wallStartMs: Long,
    val wallEndMs: Long,
    val path: String,
    val sha256: String,
    val mimeType: String,
    val uploaded: Boolean,
)

data class FactSnapshot(
    val factId: String,
    val text: String,
    val confidence: Double,
    val evidenceSupported: Boolean,
    val selected: Boolean,
    val sourcesJson: String,
)

data class DestinationSnapshot(
    val alias: String,
    val label: String,
    val provider: String,
    val status: String,
    val selected: Boolean,
)

data class PendingOperation(
    val id: String,
    val storyId: String,
    val kind: String,
    val requestKey: String,
    val payloadJson: String,
    val state: String,
    val lastError: String?,
)

data class RuntimeSnapshot(
    val durationMs: Long,
    val wallElapsedMs: Long,
    val autoSilenceSkippedMs: Long,
    val captureActivity: String,
)

fun newClientStoryId(now: OffsetDateTime = OffsetDateTime.now()): String {
    val stamp = now.format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss", Locale.US))
    return "story-$stamp-${UUID.randomUUID().toString().replace("-", "").take(8)}".lowercase(Locale.US)
}

fun newVoiceSessionId(now: OffsetDateTime = OffsetDateTime.now()): String {
    val stamp = now.format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss", Locale.US))
    return "voice-$stamp-${UUID.randomUUID().toString().replace("-", "").take(8)}".lowercase(Locale.US)
}

fun newOperationId(): String = "op-${UUID.randomUUID().toString()}"
fun newRequestKey(prefix: String, stable: String): String = "ss-$prefix-${stable.take(96)}"
fun currentTimezone(): String = ZoneId.systemDefault().id

fun refinementPayload(sessionId: String, facts: List<FactSnapshot>): Map<String, Any> = mapOf(
    "voice_session_id" to sessionId,
    "selected_fact_ids" to facts.filter { it.selected && it.evidenceSupported }.map { it.factId },
)

fun formatDuration(durationMs: Long): String {
    val seconds = (durationMs.coerceAtLeast(0L) / 1000L).toInt()
    val hours = seconds / 3600
    val minutes = (seconds % 3600) / 60
    val remainder = seconds % 60
    return if (hours > 0) String.format(Locale.US, "%02d:%02d:%02d", hours, minutes, remainder)
    else String.format(Locale.US, "%02d:%02d", minutes, remainder)
}
