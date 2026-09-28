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
    fun resetRequiresANewActivityStart() {
        val boundary = LiveSpeechBoundary()
        assertTrue(boundary.beforeAudio())
        boundary.reset()
        assertTrue(boundary.beforeAudio())
    }
}
