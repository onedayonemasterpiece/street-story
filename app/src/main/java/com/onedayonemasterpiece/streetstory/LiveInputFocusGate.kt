package com.onedayonemasterpiece.streetstory

import java.util.concurrent.atomic.AtomicInteger

internal data class LiveInputSuppression(
    val active: Boolean,
    val reason: String? = null,
    val epoch: Int = 0,
)

internal class LiveInputFocusGate {
    @Volatile private var researchActive = false
    @Volatile private var capabilityPending = false
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
    fun isCapabilityPending(): Boolean = capabilityPending

    @Synchronized
    fun setCapabilityPending(pending: Boolean): LiveInputSuppression {
        if (capabilityPending != pending) {
            capabilityPending = pending
            inputEpoch.incrementAndGet()
        }
        return snapshot(playbackSuppressed = false)
    }

    fun snapshot(playbackSuppressed: Boolean): LiveInputSuppression {
        val reason = when {
            capabilityPending -> "capability"
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
        if (researchActive || capabilityPending) {
            researchActive = false
            capabilityPending = false
            inputEpoch.incrementAndGet()
        }
        return snapshot(playbackSuppressed = false)
    }
}
