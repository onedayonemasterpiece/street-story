package com.onedayonemasterpiece.streetstory

import kotlin.math.max

/** Interactive admission policy on top of WebRTC VAD, not a speech recognizer.
 * Accumulate evidence BEFORE opening a provider activity; retain existing preroll.
 * Tuning is conservative and must be evaluated on the owner's physical device.
 */
internal class LiveSpeechAdmission {
    private val votes = BooleanArray(16)
    private var index = 0
    private var opened = false
    var noiseFloorRms = 24.0
        private set
    var rawPositiveFrames = 0L
        private set
    var acceptedFrames = 0L
        private set
    var rejectedFrames = 0L
        private set

    fun accept(rawSpeech: Boolean, rms: Double): Boolean {
        if (rawSpeech) rawPositiveFrames++
        if (!rawSpeech && rms.isFinite()) {
            noiseFloorRms = .995 * noiseFloorRms + .005 * rms.coerceIn(0.0, noiseFloorRms * 3 + 100)
        }
        val threshold = max(64.0, noiseFloorRms * if (opened) 1.6 else 2.2)
        val eligible = rawSpeech && rms.isFinite() && rms >= threshold
        votes[index] = eligible
        index = (index + 1) % votes.size
        if (!opened && votes.count { it } >= 12) opened = true
        val accepted = opened && eligible
        if (accepted) acceptedFrames++ else rejectedFrames++
        return accepted
    }

    /** Discard pre-playback VAD history, but not the learned quiet noise floor. */
    fun resetEvidence() {
        votes.fill(false)
        index = 0
        opened = false
    }

    fun metrics(): Map<String, Any> = mapOf(
        "policy" to "live-speech-admission-v1",
        "raw_positive_frames" to rawPositiveFrames,
        "admitted_positive_frames" to acceptedFrames,
        "rejected_frames" to rejectedFrames,
        "noise_floor_rms" to noiseFloorRms.toInt(),
        "attack_window_frames" to 16,
        "required_positive_frames" to 12,
    )
}

/** Counts frames actually consumed by AudioTrack, not merely accepted by write(). */
internal class PlaybackDrainTracker {
    private var written = 0L
    private var wraps = 0L
    private var previous = 0L
    @Synchronized fun wrote(frames: Long) { written += frames.coerceAtLeast(0) }
    @Synchronized fun pending(rawHead: Int): Long {
        val unsigned = rawHead.toLong() and 0xffffffffL
        if (unsigned < previous && previous - unsigned > 0x80000000L) wraps += 0x100000000L
        previous = unsigned
        return (written - (wraps + unsigned)).coerceAtLeast(0)
    }
    @Synchronized fun reset() { written = 0; wraps = 0; previous = 0 }
}
