package com.onedayonemasterpiece.streetstory

/** Source-of-truth policy for reading previously persisted owner map snapshots. */
internal object PlaceMapCacheVersion {
    private const val NEAREST_V2 = "related-map-observations-v2-nearest-buildings"

    fun needsRefresh(cacheKey: String?): Boolean =
        cacheKey.isNullOrBlank() || !cacheKey.contains(NEAREST_V2)
}
