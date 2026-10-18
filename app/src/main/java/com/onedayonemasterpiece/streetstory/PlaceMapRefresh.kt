package com.onedayonemasterpiece.streetstory

/** Whether a settled camera can show buildings outside the loaded server area. */
object PlaceMapRefresh {
    private const val PAN_METERS = 120.0
    private const val ZOOM_OUT = .4

    /**
     * Zooming in only narrows the loaded area, so it never re-downloads it and
     * never disables confirmation of a contour the author is about to tap.
     */
    fun needed(movedMeters: Double?, zoom: Double, loadedZoom: Double?): Boolean =
        movedMeters == null || movedMeters > PAN_METERS || (loadedZoom ?: zoom) - zoom > ZOOM_OUT
}
