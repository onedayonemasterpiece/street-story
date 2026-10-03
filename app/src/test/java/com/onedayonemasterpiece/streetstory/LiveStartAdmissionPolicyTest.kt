package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LiveStartAdmissionPolicyTest {
    @Test fun budgetAndServiceBusyWaitForAQuotaWindow() {
        val policy = LiveStartAdmissionPolicy(maxAttempts = 3, retryDelayMs = 5_000)
        assertEquals(LiveStartAdmissionDecision(1, 5_000), policy.next(503, "live_provider_error"))
        assertEquals(LiveStartAdmissionDecision(2, 5_000), policy.next(503, "RESOURCE_TOKEN_BUDGET"))
        assertEquals(LiveStartAdmissionDecision(3, 5_000), policy.next(429, "quota"))
        assertNull(policy.next(503, "live_provider_error"))
    }

    @Test fun clientAndProtocolErrorsNeverLoop() {
        val policy = LiveStartAdmissionPolicy()
        assertNull(policy.next(400, "invalid_argument"))
        assertNull(policy.next(401, "unauthorized"))
        assertNull(policy.next(409, "live_session_not_found"))
    }
}
