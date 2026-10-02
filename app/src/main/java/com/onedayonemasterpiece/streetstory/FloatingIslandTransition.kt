package com.onedayonemasterpiece.streetstory

internal data class FloatingIslandState(
    val progress: Float,
    val previewScale: Float,
    val previewAlpha: Float,
    val islandAlpha: Float,
)

internal object FloatingIslandTransition {
    fun state(scrollY: Int, imageTop: Int, imageHeight: Int, targetHeight: Int): FloatingIslandState {
        val travel = (imageHeight - targetHeight).coerceAtLeast(1)
        val progress = ((scrollY - imageTop).toFloat() / travel).coerceIn(0f, 1f)
        return FloatingIslandState(
            progress = progress,
            previewScale = 1f - 0.22f * progress,
            previewAlpha = 1f - 0.82f * progress,
            islandAlpha = progress,
        )
    }
}