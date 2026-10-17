package com.onedayonemasterpiece.streetstory

import com.google.gson.JsonArray
import com.google.gson.JsonParser
import org.maplibre.geojson.Geometry
import org.maplibre.geojson.Point

/** A displayed marker inside the observed footprint, never a replacement contour. */
object PlaceFootprintGeometry {
    fun interiorPoint(geometry: Geometry): Point? = runCatching {
        if (geometry is Point) return@runCatching geometry.takeIf {
            it.longitude().isFinite() && it.latitude().isFinite() &&
                it.longitude() in -180.0..180.0 && it.latitude() in -90.0..90.0
        }
        val json = JsonParser.parseString(geometry.toJson()).asJsonObject
        val coordinates = json.getAsJsonArray("coordinates")
        val polygons = when (json["type"].asString) {
            "Polygon" -> listOf(coordinates)
            "MultiPolygon" -> coordinates.map { it.asJsonArray }
            else -> emptyList()
        }
        var best: Point? = null
        var bestWidth = 0.0
        for (polygon in polygons) {
            if (polygon.size() == 0) continue
            val anchor = polygon[0].asJsonArray[0].asJsonArray[0].asDouble
            fun ring(values: JsonArray) = values.map {
                val pair = it.asJsonArray
                val lon = pair[0].asDouble
                val lat = pair[1].asDouble
                require(lon.isFinite() && lat.isFinite() && lon in -180.0..180.0 && lat in -90.0..90.0)
                var x = lon
                while (x-anchor > 180) x -= 360
                while (x-anchor < -180) x += 360
                x to lat
            }
            val rings = polygon.map { ring(it.asJsonArray) }
            val outer = rings.first()
            if (rings.any { it.size < 4 || it.first() != it.last() }) continue
            val y = (outer.minOf { it.second } + outer.maxOf { it.second }) / 2
            val crossings = rings.flatMap { points ->
                points.zipWithNext().mapNotNull { (a, b) ->
                    if ((a.second > y) == (b.second > y)) null
                    else a.first + (y-a.second)*(b.first-a.first)/(b.second-a.second)
                }
            }.sorted()
            for (interval in crossings.chunked(2)) {
                if (interval.size != 2) continue
                val width = interval[1]-interval[0]
                if (width > bestWidth) {
                    bestWidth = width
                    var x = (interval[0]+interval[1])/2
                    while (x > 180) x -= 360
                    while (x < -180) x += 360
                    best = Point.fromLngLat(x, y)
                }
            }
        }
        best
    }.getOrNull()
}
