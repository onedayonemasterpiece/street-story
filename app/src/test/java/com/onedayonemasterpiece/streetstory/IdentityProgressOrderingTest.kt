package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

class IdentityProgressOrderingTest {
    private fun progress(generation: Int, count: Int, updated: Double) = IdentityProgressWire().apply {
        this.generation = generation
        imagesReviewedCount = count
        updatedAt = updated
    }

    @Test fun `late HTTP snapshot cannot roll back a pushed live counter`() {
        val live = progress(1, 3, 30.0)
        live.visualComparisonVerified = true
        assertSame(live, newestIdentityProgress(live, progress(1, 1, 10.0)))
        assertSame(live, newestIdentityProgress(progress(1, 1, 10.0), live))
    }

    @Test fun `new photo generation resets its counter even after a completed match`() {
        val prior = progress(1, 6, 30.0)
        val next = progress(2, 0, 40.0)
        assertSame(next, newestIdentityProgress(prior, next))
        assertSame(next, newestIdentityProgress(next, prior))
    }

    @Test fun `pushed product state carries image count and verdict`() {
        val event = Gson().fromJson("""{"type":"product_state","state":{"identity_progress":{
          "generation":1,"updated_at":30,"images_reviewed_count":3,"visual_comparison_verified":true}}}""", LiveEventWire::class.java)
        val wire = Gson().fromJson(event.state!!.asJsonObject["identity_progress"], IdentityProgressWire::class.java)
        assertEquals(3, wire.imagesReviewedCount)
        assertEquals(true, wire.visualComparisonVerified)
    }
}
