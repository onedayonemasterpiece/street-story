package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class PlaceMapRefreshTest {
    @Test fun zooming_in_on_a_nearby_contour_keeps_the_loaded_area() {
        // Owner104: contour centre ~73 m from the GPS point, zoom 17 -> 18.
        assertFalse(PlaceMapRefresh.needed(73.0, 18.0, 17.0))
    }

    @Test fun zooming_out_can_reveal_unloaded_buildings() {
        assertTrue(PlaceMapRefresh.needed(0.0, 16.0, 17.0))
        assertFalse(PlaceMapRefresh.needed(0.0, 16.7, 17.0))
    }

    @Test fun panning_away_or_unknown_loaded_centre_loads_a_new_area() {
        assertTrue(PlaceMapRefresh.needed(150.0, 17.0, 17.0))
        assertFalse(PlaceMapRefresh.needed(100.0, 17.0, 17.0))
        assertTrue(PlaceMapRefresh.needed(null, 17.0, 17.0))
    }
}
