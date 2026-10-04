package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FactEligibilityTest {
    @Test
    fun literalEvidenceDoesNotAdmitWithheldOrUnreviewedClaim() {
        val gson = Gson()
        val rejected = listOf("withheld", "unreviewed")
        rejected.forEach { state ->
            val fact = gson.fromJson("""{"fact_id":"own-span-exists","evidence_supported":true,"selected":true,"eligibility":"$state"}""", FactWire::class.java)
            assertFalse(fact.eligibleForSelection)
        }
        val admitted = gson.fromJson("""{"evidence_supported":true,"eligibility":"eligible"}""", FactWire::class.java)
        assertTrue(admitted.eligibleForSelection)
        val legacy = gson.fromJson("""{"evidence_supported":true}""", FactWire::class.java)
        assertFalse(legacy.eligibleForSelection)
    }
}
