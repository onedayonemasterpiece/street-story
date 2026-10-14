package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test
import org.maplibre.geojson.Feature

class PlaceFootprintGeometryTest {
    private fun marker(type: String, coordinates: String) = PlaceFootprintGeometry.interiorPoint(
        requireNotNull(Feature.fromJson("""{"type":"Feature","properties":{},"geometry":{"type":"$type","coordinates":$coordinates}}""").geometry()))

    @Test fun concave_footprint_marker_stays_in_a_wing_instead_of_empty_bbox_center() {
        val point = requireNotNull(marker("Polygon", """[[[0,0],[5,0],[5,5],[4,5],[4,1],[1,1],[1,5],[0,5],[0,0]]]"""))
        assertTrue(point.longitude() in 0.0..1.0 || point.longitude() in 4.0..5.0)
        assertEquals(2.5, point.latitude(), .00001)
    }

    @Test fun courtyard_hole_does_not_receive_the_marker() {
        val point = requireNotNull(marker("Polygon", """[[[0,0],[10,0],[10,10],[0,10],[0,0]],[[4,4],[4,6],[6,6],[6,4],[4,4]]]"""))
        assertTrue(point.longitude() < 4 || point.longitude() > 6)
        assertEquals(5.0, point.latitude(), .00001)
    }

    @Test fun disjoint_components_do_not_receive_a_marker_in_the_gap() {
        val point = requireNotNull(marker("MultiPolygon", """[[[[0,0],[1,0],[1,1],[0,1],[0,0]]],[[[11,0],[13,0],[13,1],[11,1],[11,0]]]]"""))
        assertEquals(12.0, point.longitude(), .00001)
        assertEquals(.5, point.latitude(), .00001)
    }

    @Test fun antimeridian_coordinates_stay_with_the_footprint() {
        val point = requireNotNull(marker("Polygon", """[[[170,1],[-170,1],[-170,2],[170,2],[170,1]]]"""))
        assertEquals(180.0, kotlin.math.abs(point.longitude()), .00001)
        assertEquals(1.5, point.latitude(), .00001)
    }

    @Test fun malformed_or_degenerate_footprint_does_not_invent_a_marker() {
        assertNull(marker("Polygon", """[[[0,0],[1,0],[2,0],[0,0]]]"""))
        assertNull(marker("Polygon", """[[[0,0],[1,0],[1,1],[0,1]]]"""))
    }
}
