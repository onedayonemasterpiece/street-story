package com.onedayonemasterpiece.streetstory

class SpeechLatch(private val attackFrames: Int = 3, private val hangoverFrames: Int = 60) {
    init { require(attackFrames >= 1); require(hangoverFrames >= 0) }
    private var positiveFrames = 0
    private var remainingHangover = 0
    var active: Boolean = false
        private set

    fun onFrame(rawSpeech: Boolean): Boolean {
        if (rawSpeech) {
            positiveFrames++
            if (!active && positiveFrames >= attackFrames) active = true
            if (active) remainingHangover = hangoverFrames
        } else {
            positiveFrames = 0
            if (active) {
                if (remainingHangover > 0) remainingHangover-- else active = false
            }
        }
        return active
    }

    fun reset() { positiveFrames = 0; remainingHangover = 0; active = false }
}
