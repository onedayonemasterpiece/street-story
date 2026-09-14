package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AdaptiveEnergyGateTest {
    @Test fun digitalSilenceNeverInvokesVad() { val gate=AdaptiveEnergyGate(probeEveryFrames=2); repeat(10){assertFalse(gate.shouldRunVad(0.0))} }
    @Test fun quietFramesReceivePeriodicProbe() {
        val gate=AdaptiveEnergyGate(initialNoiseFloorRms=100.0,thresholdMultiplier=2.0,absoluteMarginRms=100.0,probeEveryFrames=5)
        assertEquals(listOf(false,false,false,false,true),(1..5).map{gate.shouldRunVad(60.0)})
    }
    @Test fun elevatedEnergyRunsImmediately(){assertTrue(AdaptiveEnergyGate(initialNoiseFloorRms=50.0).shouldRunVad(500.0))}
    @Test fun onlyNonSpeechUpdatesNoiseFloor(){val gate=AdaptiveEnergyGate(initialNoiseFloorRms=100.0,probeEveryFrames=1);gate.shouldRunVad(300.0);gate.observeVadResult(300.0,true);assertEquals(100.0,gate.noiseFloorRms,0.0001);gate.observeVadResult(300.0,false);assertTrue(gate.noiseFloorRms>100.0)}
    @Test fun rmsUsesPcmAmplitude(){assertEquals(0.0,frameRms(shortArrayOf()),0.0);assertEquals(100.0,frameRms(shortArrayOf(100,-100)),0.0001)}
}
