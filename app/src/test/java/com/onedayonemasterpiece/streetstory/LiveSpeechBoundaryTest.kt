package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveSpeechBoundaryTest {
    @Test
    fun opensOncePerSpeechAndClosesOnce() {
        val boundary = LiveSpeechBoundary()
        assertTrue(boundary.beforeAudio())
        assertFalse(boundary.beforeAudio())
        assertTrue(boundary.end())
        assertFalse(boundary.end())
        assertTrue(boundary.beforeAudio())
        assertTrue(boundary.end())
    }

    @Test
    fun audioBatchingLeavesNetworkHeadroomWithoutStaleSpeech() {
        val batchMs = LiveAudioTransportPolicy.TARGET_PCM_BYTES / 2 * 1000 / 16_000
        assertTrue(batchMs in 700..900)
        assertTrue(LiveAudioTransportPolicy.OUTBOUND_CAPACITY >= 8)
        assertTrue(LiveAudioTransportPolicy.MAX_AUDIO_AGE_MS > batchMs * 2)
    }

    @Test
    fun resetRequiresANewActivityStart() {
        val boundary = LiveSpeechBoundary()
        assertTrue(boundary.beforeAudio())
        boundary.reset()
        assertTrue(boundary.beforeAudio())
    }
}
