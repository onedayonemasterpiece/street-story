package com.onedayonemasterpiece.streetstory

internal data class LiveStartAdmissionDecision(
    val attempt: Int,
    val delayMs: Long,
)

internal class LiveStartAdmissionPolicy(
    private val maxAttempts: Int = 14,
    private val retryDelayMs: Long = 5_000L,
) {
    private var attempts = 0

    @Synchronized
    fun next(status: Int, code: String): LiveStartAdmissionDecision? {
        val normalized = code.trim().uppercase()
        val retryable = status == 429 || status == 503 ||
            normalized in setOf("RESOURCE_TOKEN_BUDGET", "LIVE_PROVIDER_ERROR", "LIVE_UNAVAILABLE", "HTTP_429", "HTTP_503")
        if (!retryable || attempts >= maxAttempts) return null
        attempts += 1
        return LiveStartAdmissionDecision(attempts, retryDelayMs)
    }
}
