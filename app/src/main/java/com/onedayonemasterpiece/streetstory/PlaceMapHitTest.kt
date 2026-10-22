package com.onedayonemasterpiece.streetstory

import kotlin.math.hypot

/** Small-footprint tap recovery after the normal MapLibre filled-layer hit-test. */
internal object PlaceMapHitTest {
    data class Pixel(val x: Double, val y: Double)
    data class Footprint(val candidateId: String, val outline: List<Pixel>)

    /**
     * Do NOT guess an OSM building from the raster basemap or nearest street.
     * Only a real observed building in the server's selectable GeoJSON qualifies.
     * Large buildings still use MapLibre's exact fill hit; this recovers circles
     * and other tiny shapes which are hard to target with a fingertip.
     */
    fun nearestSmall(tap: Pixel, footprints: List<Footprint>, radiusPx: Double): String? {
        if (!radiusPx.isFinite() || radiusPx <= 0.0) return null
        val near = footprints.mapNotNull { footprint ->
            val points = footprint.outline
            if (footprint.candidateId.isBlank() || points.size < 4 ||
                points.any { !it.x.isFinite() || !it.y.isFinite() }) return@mapNotNull null
            val xmin = points.minOf { it.x }
            val xmax = points.maxOf { it.x }
            val ymin = points.minOf { it.y }
            val ymax = points.maxOf { it.y }
            if (xmax-xmin > radiusPx * 2 || ymax-ymin > radiusPx * 2) return@mapNotNull null
            val distance = hypot((xmin+xmax)/2-tap.x, (ymin+ymax)/2-tap.y)
            if (distance > radiusPx) return@mapNotNull null
            footprint.candidateId to distance
        }.sortedWith(compareBy<Pair<String, Double>> { it.second }.thenBy { it.first })
        val first = near.firstOrNull() ?: return null
        // Ambiguous tiny neighbors must never be silently exchanged.
        if (near.size > 1 && near[1].second-first.second < radiusPx / 4) return null
        return first.first
    }
}
