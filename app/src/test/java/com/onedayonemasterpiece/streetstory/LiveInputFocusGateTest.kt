package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveInputFocusGateTest {
    @Test fun researchFocusOwnsSuppressionAndAdvancesEpochOnEachBoundary() {
        val gate = LiveInputFocusGate()

        val initial = gate.snapshot(playbackSuppressed = false)
        assertFalse(initial.active)
        assertEquals(0, initial.epoch)

        val started = gate.setResearchActive(true)
        assertTrue(started.active)
        assertEquals("research", started.reason)
        assertEquals(1, started.epoch)
        assertTrue(gate.isResearchActive())

        val repeated = gate.setResearchActive(true)
        assertEquals(1, repeated.epoch)

        val duringPlayback = gate.snapshot(playbackSuppressed = true)
        assertEquals("research", duringPlayback.reason)
        assertEquals(1, duringPlayback.epoch)

        val resumed = gate.setResearchActive(false)
        assertFalse(resumed.active)
        assertEquals(2, resumed.epoch)
        assertFalse(gate.isResearchActive())
    }

    @Test fun playbackSuppressionDoesNotChangeResearchInputEpoch() {
        val gate = LiveInputFocusGate()

        val playback = gate.snapshot(playbackSuppressed = true)
        assertTrue(playback.active)
        assertEquals("playback", playback.reason)
        assertEquals(0, playback.epoch)

        val drained = gate.snapshot(playbackSuppressed = false)
        assertFalse(drained.active)
        assertEquals(0, drained.epoch)
    }

    @Test fun resetEndsResearchFocusAndCreatesFreshCaptureEpoch() {
        val gate = LiveInputFocusGate()
        gate.setResearchActive(true)

        val reset = gate.reset()
        assertFalse(reset.active)
        assertEquals(2, reset.epoch)
        assertFalse(gate.isResearchActive())
    }
}
