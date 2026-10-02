package com.onedayonemasterpiece.streetstory

internal data class LiveReconnectDecision(val attempt: Int, val delayMs: Long)

internal class LiveReconnectPolicy(private val maxAttempts: Int = 2) {
    private var attempts = 0

    @Synchronized
    fun next(code: String): LiveReconnectDecision? {
        if (code !in TRANSIENT_CODES || attempts >= maxAttempts) return null
        attempts += 1
        return LiveReconnectDecision(attempts, if (attempts == 1) 250L else 650L)
    }

    @Synchronized
    fun reset() {
        attempts = 0
    }

    companion object {
        private val TRANSIENT_CODES = setOf(
            "LIVE_SOCKET_IO",
            "LIVE_SOCKET_CLOSED",
            "LIVE_SOCKET_ACK_TIMEOUT",
            "LIVE_SOCKET_HEARTBEAT_TIMEOUT",
            "LIVE_SOCKET_HELLO_TIMEOUT",
        )
    }
}
