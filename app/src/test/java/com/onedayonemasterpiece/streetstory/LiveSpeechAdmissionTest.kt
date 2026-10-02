package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class LiveSpeechAdmissionTest {
    @Test fun isolatedRustlesDoNotOpenProviderActivity() {
        val gate = LiveSpeechAdmission()
        repeat(100) { i -> assertFalse(gate.accept(i % 10 < 2, if(i % 10 < 2) 400.0 else 8.0)) }
        assertEquals(0L, gate.acceptedFrames)
    }
    @Test fun nearSilentNativeFalsePositiveIsNotSpeech() {
        val gate = LiveSpeechAdmission()
        repeat(1000) { assertFalse(gate.accept(true, 9.0)) }
    }
    @Test fun sustainedVotesAdmitSpeechWithoutLosingExistingPreroll() {
        val gate = LiveSpeechAdmission()
        repeat(5) { assertFalse(gate.accept(true, 350.0)) }
        assertTrue(gate.accept(true, 350.0))
        assertTrue(gate.accept(true, 180.0))
    }
    @Test fun playbackRecoveryRequiresFreshVotes() {
        val gate = LiveSpeechAdmission()
        repeat(8) { gate.accept(true, 350.0) }
        gate.resetEvidence()
        assertFalse(gate.accept(true, 350.0))
        repeat(4) { assertFalse(gate.accept(true, 350.0)) }
        assertTrue(gate.accept(true, 350.0))
    }
    @Test fun nonFiniteEnergyNeverOpensInput() {
        val gate = LiveSpeechAdmission()
        repeat(20) { assertFalse(gate.accept(true, Double.NaN)) }
        assertFalse(gate.accept(true, Double.POSITIVE_INFINITY))
    }
    @Test fun audioAcceptedByWriteIsNotYetPlayed() {
        val tracker = PlaybackDrainTracker()
        tracker.wrote(12000)
        assertEquals(12000L, tracker.pending(0))
        assertEquals(6000L, tracker.pending(6000))
        assertEquals(0L, tracker.pending(12000))
        tracker.reset()
        assertEquals(0L, tracker.pending(0))
    }
    @Test fun playbackHeadUsesUnsigned32bitWrap() {
        val tracker = PlaybackDrainTracker()
        tracker.wrote(0x100000000L + 100L)
        assertEquals(116L, tracker.pending(-16))
        assertEquals(0L, tracker.pending(100))
    }
}
