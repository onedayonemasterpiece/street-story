package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class LiveMicrophoneHealthTest {
    @Test fun quietCaptureIsVisibleWithoutPretendingSpeechWasSent() {
        val health = LiveMicrophoneHealth()
        assertNull(health.observe(0, 2.0).warning)
        assertNull(health.observe(7999, 4.0).warning)
        assertNotNull(health.observe(8000, 2.0).warning)
        assertEquals(0, health.observe(9000, 2.0).level)
        val speech = health.observe(9100, 1000.0)
        assertNull(speech.warning)
        assertEquals(3, speech.level)
    }

    @Test fun playbackIsNotMisreportedAsABrokenMicrophone() {
        val health = LiveMicrophoneHealth()
        health.observe(0, 0.0)
        val playback = health.observe(9000, 0.0, playbackSuppressed = true)
        assertTrue(playback.playbackSuppressed)
        assertNull(playback.warning)
        assertNull(health.observe(9100, 0.0).warning)
        assertNull(health.observe(17000, 0.0).warning)
        assertNotNull(health.observe(17100, 0.0).warning)
    }

    @Test fun systemMuteAndClientSilencingAreDistinctFromNaturalSilence() {
        val health = LiveMicrophoneHealth()
        assertNotNull(health.observe(0, 1000.0, systemMuted = true).warning)
        assertNotNull(health.observe(1, 1000.0, clientSilenced = true).warning)
        assertNull(health.observe(2, 1000.0).warning)
    }

    @Test fun levelsStayBoundedAndInvalidMetricsDoNotInventSignal() {
        val health = LiveMicrophoneHealth()
        assertEquals(0, health.observe(0, Double.NaN).level)
        assertEquals(0, health.observe(1, Double.POSITIVE_INFINITY).level)
        assertEquals(0, health.observe(2, -100.0).level)
        assertEquals(4, health.observe(3, 32768.0).level)
        assertEquals(1, health.observe(4, 20.0).level)
        assertEquals(2, health.observe(5, 80.0).level)
    }
}
