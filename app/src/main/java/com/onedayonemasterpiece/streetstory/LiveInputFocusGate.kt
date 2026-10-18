package com.onedayonemasterpiece.streetstory

import java.util.concurrent.atomic.AtomicInteger

internal data class LiveInputSuppression(
    val active: Boolean,
    val reason: String? = null,
    val epoch: Int = 0,
)

internal class LiveInputFocusGate {
    @Volatile private var researchActive = false
    private val inputEpoch = AtomicInteger(0)

    @Synchronized
    fun setResearchActive(active: Boolean): LiveInputSuppression {
        if (researchActive != active) {
            researchActive = active
            inputEpoch.incrementAndGet()
        }
        return snapshot(playbackSuppressed = false)
    }

    fun isResearchActive(): Boolean = researchActive

    fun snapshot(playbackSuppressed: Boolean): LiveInputSuppression {
        val reason = when {
            researchActive -> "research"
            playbackSuppressed -> "playback"
            else -> null
        }
        return LiveInputSuppression(
            active = reason != null,
            reason = reason,
            epoch = inputEpoch.get(),
        )
    }

    @Synchronized
    fun reset(): LiveInputSuppression {
        if (researchActive) {
            researchActive = false
            inputEpoch.incrementAndGet()
        }
        return snapshot(playbackSuppressed = false)
    }
}
