package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.onedayonemasterpiece.live.LiveSocketTransport

class LiveSpeechBoundaryTest {
    @Test fun opensOncePerSpeechAndClosesOnce() {
        val boundary = LiveSpeechBoundary()
        assertTrue(boundary.beforeAudio())
        assertFalse(boundary.beforeAudio())
        assertTrue(boundary.end())
        assertFalse(boundary.end())
        assertTrue(boundary.beforeAudio())
        assertTrue(boundary.end())
    }
    @Test fun wssUsesSmallFramesAndBoundedOutstandingAudio() {
        val batchMs = LiveAudioTransportPolicy.TARGET_PCM_BYTES / 2 * 1000 / 16_000
        assertTrue(batchMs in 20..100)
        assertTrue(LiveAudioTransportPolicy.OUTBOUND_CAPACITY in 1..4)
        assertEquals(2500L, LiveAudioTransportPolicy.MAX_AUDIO_AGE_MS)
        assertEquals(48000, LiveSocketTransport.MAX_PENDING_PCM_BYTES)
        assertEquals("wl-live-v1", LiveSocketTransport.PROTOCOL)
    }
    @Test fun resetRequiresANewActivityStart() {
        val boundary = LiveSpeechBoundary()
        assertTrue(boundary.beforeAudio())
        boundary.reset()
        assertTrue(boundary.beforeAudio())
    }
}
