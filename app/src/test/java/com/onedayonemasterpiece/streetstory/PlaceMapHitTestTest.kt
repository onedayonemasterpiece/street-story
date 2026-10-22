package com.onedayonemasterpiece.streetstory

import org.junit.Assert.*
import org.junit.Test

class PlaceMapHitTestTest {
    // Actual OSM way 133724496 (Мурариум), captured from OSM API 2026-10-22:
    // 31 closed nodes, NOT the neighboring rectangular hotel (way 382545007).
    private val murarium = listOf(
        20.4825858 to 54.9598162,
        20.4825949 to 54.9598097,
        20.4826014 to 54.9598023,
        20.4826052 to 54.9597942,
        20.4826059 to 54.9597859,
        20.4826036 to 54.9597777,
        20.4825983 to 54.9597699,
        20.4825904 to 54.9597629,
        20.4825801 to 54.959757,
        20.4825679 to 54.9597525,
        20.4825543 to 54.9597495,
        20.48254 to 54.9597483,
        20.4825255 to 54.9597487,
        20.4825114 to 54.9597509,
        20.4824985 to 54.9597547,
        20.4824872 to 54.95976,
        20.4824781 to 54.9597665,
        20.4824716 to 54.9597739,
        20.4824678 to 54.959782,
        20.4824671 to 54.9597903,
        20.4824694 to 54.9597985,
        20.4824747 to 54.9598063,
        20.4824826 to 54.9598133,
        20.4824929 to 54.9598192,
        20.4825051 to 54.9598237,
        20.4825187 to 54.9598267,
        20.4825331 to 54.959828,
        20.4825476 to 54.9598275,
        20.4825616 to 54.9598253,
        20.4825745 to 54.9598215,
        20.4825858 to 54.9598162,
    )
    private fun px(points: List<Pair<Double, Double>>): List<PlaceMapHitTest.Pixel> =
        points.map { (lon, lat) ->
            PlaceMapHitTest.Pixel((lon - 20.48254)*100_000.0, (lat - 54.95979)*100_000.0)
        }
    private val circle = PlaceMapHitTest.Footprint("osm:way:133724496", px(murarium))
    private val paradox = PlaceMapHitTest.Footprint("osm:way:382545007", listOf(
        PlaceMapHitTest.Pixel(3.2, 24.4), PlaceMapHitTest.Pixel(14.8, 12.7),
        PlaceMapHitTest.Pixel(20.1, 7.5), PlaceMapHitTest.Pixel(19.8, -2.4),
        PlaceMapHitTest.Pixel(33.1, -3.0), PlaceMapHitTest.Pixel(46.7, -3.6),
        PlaceMapHitTest.Pixel(47.4, 8.7), PlaceMapHitTest.Pixel(22.7, 31.0),
        PlaceMapHitTest.Pixel(3.2, 24.4)
    ))

    @Test fun murariumIsSelectedNotTheNeighboringHotelAtNormalZoom() {
        assertEquals("osm:way:133724496", PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(0.0, 0.0), listOf(paradox, circle), 18.0))
        assertEquals("osm:way:133724496", PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(-6.0, -3.0), listOf(paradox, circle), 18.0))
        // If the server forgot the circular contour, NEVER replace it with a
        // plausible adjacent rectangle just because the OSM basemap draws one.
        assertNull(PlaceMapHitTest.nearestSmall(PlaceMapHitTest.Pixel(0.0, 0.0), listOf(paradox), 18.0))
    }

    @Test fun otherTinyRoundFootprintIsTappableAndOrdinaryPolygonStillWorks() {
        val other = PlaceMapHitTest.Footprint("osm:way:second-round",
            circle.outline.map { it.copy(x = it.x + 60.0) })
        val ordinary = PlaceMapHitTest.Footprint("osm:way:regular-small", listOf(
            PlaceMapHitTest.Pixel(95.0, -5.0), PlaceMapHitTest.Pixel(105.0, -5.0),
            PlaceMapHitTest.Pixel(105.0, 5.0), PlaceMapHitTest.Pixel(95.0, 5.0),
            PlaceMapHitTest.Pixel(95.0, -5.0)))
        assertEquals("osm:way:second-round", PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(60.0, 0.0), listOf(circle, other, ordinary), 18.0))
        assertEquals("osm:way:regular-small", PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(101.0, 1.0), listOf(circle, other, ordinary), 18.0))
        assertNull(PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(250.0, 250.0), listOf(circle, other, ordinary), 18.0))
    }

    @Test fun ambiguousTinyContoursNeverGetSwappedByArbitraryProviderOrder() {
        val neighbor = PlaceMapHitTest.Footprint("osm:way:another", circle.outline.map { it.copy(x=2.0) })
        assertNull(PlaceMapHitTest.nearestSmall(
            PlaceMapHitTest.Pixel(0.0, 0.0), listOf(neighbor, circle), 18.0))
    }
}
