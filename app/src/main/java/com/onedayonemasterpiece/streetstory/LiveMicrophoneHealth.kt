package com.onedayonemasterpiece.streetstory

/** Measured local capture health, independent of VAD and server readiness. */
data class MicrophoneReading(
    val level: Int,
    val warning: String? = null,
    val playbackSuppressed: Boolean = false,
)

internal class LiveMicrophoneHealth {
    private var quietSince: Long? = null

    fun observe(
        nowMs: Long,
        rms: Double,
        systemMuted: Boolean = false,
        clientSilenced: Boolean = false,
        playbackSuppressed: Boolean = false,
    ): MicrophoneReading {
        if (playbackSuppressed) {
            quietSince = null
            return MicrophoneReading(0, playbackSuppressed = true)
        }
        if (systemMuted || clientSilenced) {
            quietSince = null
            return MicrophoneReading(0, "Android отключил звук микрофона")
        }
        val signal = if (rms.isFinite()) rms.coerceAtLeast(0.0) else 0.0
        if (signal >= QUIET_RMS) quietSince = null
        else if (quietSince == null || nowMs < quietSince!!) quietSince = nowMs
        val quietFor = quietSince?.let { nowMs - it } ?: 0L
        val level = when {
            signal >= 1024 -> 4
            signal >= 256 -> 3
            signal >= 64 -> 2
            signal >= 16 -> 1
            else -> 0
        }
        return MicrophoneReading(
            level,
            if (quietFor >= QUIET_NOTICE_MS) "Очень тихий сигнал · проверьте микрофон" else null,
        )
    }

    companion object {
        const val QUIET_RMS = 32.0
        const val QUIET_NOTICE_MS = 8000L
    }
}
