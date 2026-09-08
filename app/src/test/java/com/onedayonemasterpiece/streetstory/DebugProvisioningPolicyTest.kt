package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DebugProvisioningPolicyTest {
    @Test
    fun acceptsStrictHttpsAndBoundedToken() {
        val token = "t".repeat(32)
        val parsed = DebugProvisioningPolicy.parse("  https://street-story.example.test/  ", token)
        assertEquals("https://street-story.example.test", parsed?.backendUrl)
        assertEquals(token, parsed?.deviceToken)
    }

    @Test
    fun rejectsNonHttpsMissingAndWhitespaceSecrets() {
        assertNull(DebugProvisioningPolicy.parse("http://street-story.example.test", "t".repeat(32)))
        assertNull(DebugProvisioningPolicy.parse("https://street-story.example.test", null))
        assertNull(DebugProvisioningPolicy.parse(null, "t".repeat(32)))
        assertNull(DebugProvisioningPolicy.parse("https://street-story.example.test", "t".repeat(31) + " "))
    }

    @Test
    fun rejectsOutOfBoundsSecretsAndNonOriginCredentials() {
        assertNull(DebugProvisioningPolicy.parse("https://street-story.example.test", "t".repeat(31)))
        assertNull(DebugProvisioningPolicy.parse("https://street-story.example.test", "t".repeat(257)))
        assertNull(DebugProvisioningPolicy.parse("https://user:pass@street-story.example.test", "t".repeat(32)))
        assertTrue(DebugProvisioningPolicy.validDeviceToken("x".repeat(256)))
    }
}
