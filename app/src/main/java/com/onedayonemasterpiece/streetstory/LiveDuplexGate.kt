package com.onedayonemasterpiece.streetstory

internal class LiveDuplexGate(private val postPlaybackGuardMs: Long = 300L) {
    @Volatile private var guardUntilMs: Long = 0L

    fun shouldSuppress(nowMs: Long, pendingPlaybackBytes: Int): Boolean =
        pendingPlaybackBytes > 0 || nowMs < guardUntilMs

    fun onPlaybackDrained(nowMs: Long) {
        guardUntilMs = maxOf(guardUntilMs, nowMs + postPlaybackGuardMs)
    }

    fun reset() {
        guardUntilMs = 0L
    }

    fun pcmDurationMs(pcmBytes: Int, sampleRate: Int): Long {
        if (pcmBytes <= 0 || sampleRate <= 0) return 0L
        return pcmBytes.toLong() * 1000L / (sampleRate.toLong() * 2L)
    }

    fun isUnexpectedlySlowWrite(writeMs: Long, pcmBytes: Int, sampleRate: Int): Boolean =
        writeMs > pcmDurationMs(pcmBytes, sampleRate) + SLOW_WRITE_SLACK_MS

    companion object {
        private const val SLOW_WRITE_SLACK_MS = 250L
    }
}
