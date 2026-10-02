package com.onedayonemasterpiece.streetstory

/** Measured local capture health, independent of VAD and server readiness. */
data class MicrophoneReading(
    val level: Int,
    val warning: String? = null,
    val playbackSuppressed: Boolean = false,
)

internal class LiveMicrophoneHealth {
    fun observe(
        nowMs: Long,
        rms: Double,
        systemMuted: Boolean = false,
        clientSilenced: Boolean = false,
        playbackSuppressed: Boolean = false,
    ): MicrophoneReading {
        @Suppress("UNUSED_VARIABLE")
        val observedAt = nowMs
        if (playbackSuppressed) {
            return MicrophoneReading(0, playbackSuppressed = true)
        }
        if (systemMuted || clientSilenced) {
            return MicrophoneReading(0, "Android отключил звук микрофона")
        }
        val signal = if (rms.isFinite()) rms.coerceAtLeast(0.0) else 0.0
        val level = when {
            signal >= 1024 -> 4
            signal >= 256 -> 3
            signal >= 64 -> 2
            signal >= 16 -> 1
            else -> 0
        }
        // Quiet room / pause between phrases is normal. VAD already decides when
        // speech is present; do not paint natural silence as a microphone failure.
        return MicrophoneReading(level)
    }
}
