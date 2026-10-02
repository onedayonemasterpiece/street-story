package com.onedayonemasterpiece.streetstory

import com.konovalov.vad.webrtc.VadWebRTC
import com.konovalov.vad.webrtc.config.FrameSize
import com.konovalov.vad.webrtc.config.Mode
import com.konovalov.vad.webrtc.config.SampleRate
import java.io.Closeable
import kotlin.math.max
import kotlin.math.sqrt

class EfficientVad(private val enabled: Boolean, private val interactive: Boolean = false) : Closeable {
    private fun createDetector(): VadWebRTC? = if (enabled) runCatching {
        VadWebRTC(
            sampleRate = SampleRate.SAMPLE_RATE_16K,
            frameSize = FrameSize.FRAME_SIZE_480,
            mode = if (interactive) Mode.VERY_AGGRESSIVE else Mode.LOW_BITRATE,
            speechDurationMs = 0,
            silenceDurationMs = 0,
        )
    }.getOrNull() else null
    private var detector: VadWebRTC? = createDetector()
    private val energyGate = AdaptiveEnergyGate()
    private var failedOpen = enabled && detector == null

    val isFailOpen: Boolean get() = failedOpen && !interactive
    val isUnavailable: Boolean get() = failedOpen && interactive

    fun resetAfterPlayback() {
        detector?.let { runCatching { it.close() } }
        detector = createDetector()
        failedOpen = enabled && detector == null
    }

    fun isSpeech(frame: ShortArray): Boolean {
        if (!enabled) return true
        if (failedOpen) return !interactive
        val activeDetector = detector ?: return !interactive
        val rms = frameRms(frame)
        // Interactive admission needs a continuous vote window; the recording
        // energy-probe optimization would leave gaps and suppress quiet speech.
        if (!interactive && !energyGate.shouldRunVad(rms)) return false
        return try {
            activeDetector.isSpeech(frame).also { speech -> energyGate.observeVadResult(rms, speech) }
        } catch (_: Throwable) {
            failedOpen = true
            runCatching { activeDetector.close() }
            detector = null
            !interactive
        }
    }

    override fun close() {
        detector?.let { runCatching { it.close() } }
        detector = null
    }

    companion object {
        const val ENGINE_NAME = "webrtc_vad"
        const val ENGINE_VERSION = "2.0.10-cf.4"
        const val CONFIG_VERSION = "vad-auto-pause-efficient-v1"
        const val MODE = 1
        const val FRAME_SAMPLES = 480
        const val FRAME_MS = 30L
    }
}

internal class AdaptiveEnergyGate(
    initialNoiseFloorRms: Double = 80.0,
    private val thresholdMultiplier: Double = 1.18,
    private val absoluteMarginRms: Double = 12.0,
    private val probeEveryFrames: Int = 5,
    private val digitalSilenceRms: Double = 20.0,
) {
    var noiseFloorRms: Double = initialNoiseFloorRms
        private set
    private var frameCounter = 0L

    init {
        require(initialNoiseFloorRms >= 0.0)
        require(thresholdMultiplier >= 1.0)
        require(absoluteMarginRms >= 0.0)
        require(probeEveryFrames >= 1)
        require(digitalSilenceRms >= 0.0)
    }

    fun shouldRunVad(rms: Double): Boolean {
        frameCounter++
        if (rms <= digitalSilenceRms) {
            observeNonSpeech(rms)
            return false
        }
        val threshold = max(noiseFloorRms * thresholdMultiplier, noiseFloorRms + absoluteMarginRms)
        val periodicProbe = frameCounter % probeEveryFrames == 0L
        if (rms < threshold && !periodicProbe) {
            observeNonSpeech(rms)
            return false
        }
        return true
    }

    fun observeVadResult(rms: Double, speech: Boolean) { if (!speech) observeNonSpeech(rms) }

    private fun observeNonSpeech(rms: Double) {
        val bounded = rms.coerceIn(0.0, noiseFloorRms * 3.0 + 200.0)
        noiseFloorRms = noiseFloorRms * 0.995 + bounded * 0.005
    }
}

internal fun frameRms(frame: ShortArray): Double {
    if (frame.isEmpty()) return 0.0
    var sum = 0.0
    for (sample in frame) { val value = sample.toDouble(); sum += value * value }
    return sqrt(sum / frame.size)
}
