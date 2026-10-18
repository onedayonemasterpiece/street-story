package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LiveReconnectPolicyTest {
    @Test fun retriesOnlyTransientSocketFailuresAndIsBounded() {
        val policy = LiveReconnectPolicy(2)
        assertEquals(1, policy.next("LIVE_SOCKET_IO")?.attempt)
        assertEquals(2, policy.next("LIVE_SOCKET_CLOSED")?.attempt)
        assertNull(policy.next("LIVE_SOCKET_IO"))
    }

    @Test fun protocolAndAudioPolicyFailuresDoNotReconnect() {
        val policy = LiveReconnectPolicy()
        assertNull(policy.next("LIVE_SOCKET_PROTOCOL"))
        assertNull(policy.next("LIVE_AUDIO_BACKPRESSURE"))
        assertNull(policy.next("LIVE_AUDIO_STALE"))
    }

    @Test fun resetStartsANewLiveSessionBudget() {
        val policy = LiveReconnectPolicy(1)
        assertEquals(1, policy.next("LIVE_SOCKET_HEARTBEAT_TIMEOUT")?.attempt)
        assertNull(policy.next("LIVE_SOCKET_IO"))
        policy.reset()
        assertEquals(1, policy.next("LIVE_SOCKET_IO")?.attempt)
    }
}
