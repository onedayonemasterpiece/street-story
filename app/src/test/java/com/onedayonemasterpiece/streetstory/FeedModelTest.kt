package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Test

class FeedModelTest {
    @Test fun latestTenHidesButDoesNotDeleteOlderItems() {
        val durable = (0 until 14).toList()
        val visible = FeedModel.latest(durable)
        assertEquals((0 until 10).toList(), visible)
        assertEquals(14, durable.size)
        assertEquals(listOf(10, 11, 12, 13), durable.drop(visible.size))
    }
}
