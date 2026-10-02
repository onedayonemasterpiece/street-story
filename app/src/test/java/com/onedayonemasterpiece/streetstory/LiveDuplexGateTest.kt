package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveDuplexGateTest {
    @Test fun pendingPlaybackSuppressesMicrophoneImmediately() {
        val gate = LiveDuplexGate(300)
        assertTrue(gate.shouldSuppress(nowMs = 1000, pendingPlaybackBytes = 3200))
        assertFalse(gate.shouldSuppress(nowMs = 1000, pendingPlaybackBytes = 0))
    }

    @Test fun postPlaybackGuardPreventsEchoTailFromOpeningANewTurn() {
        val gate = LiveDuplexGate(300)
        gate.onPlaybackDrained(1000)
        assertTrue(gate.shouldSuppress(nowMs = 1299, pendingPlaybackBytes = 0))
        assertFalse(gate.shouldSuppress(nowMs = 1300, pendingPlaybackBytes = 0))
        gate.reset()
        assertFalse(gate.shouldSuppress(nowMs = 1001, pendingPlaybackBytes = 0))
    }

    @Test fun slowWriteThresholdAccountsForPcmDuration() {
        val gate = LiveDuplexGate()
        assertFalse(gate.isUnexpectedlySlowWrite(writeMs = 356, pcmBytes = 23040, sampleRate = 24000))
        assertTrue(gate.isUnexpectedlySlowWrite(writeMs = 800, pcmBytes = 23040, sampleRate = 24000))
    }
}
