package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechLatchTest {
    @Test fun requiresAttackAndKeepsHangover(){val latch=SpeechLatch(3,2);assertFalse(latch.onFrame(true));assertFalse(latch.onFrame(true));assertTrue(latch.onFrame(true));assertTrue(latch.onFrame(false));assertTrue(latch.onFrame(false));assertFalse(latch.onFrame(false))}
    @Test fun resetReturnsToSilence(){val latch=SpeechLatch(1,0);assertTrue(latch.onFrame(true));latch.reset();assertFalse(latch.active);assertFalse(latch.onFrame(false))}
}
