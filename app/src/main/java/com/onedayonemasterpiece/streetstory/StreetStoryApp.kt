package com.onedayonemasterpiece.streetstory

import android.app.Application

class StreetStoryApp : Application() {
    override fun onCreate() {
        super.onCreate()
        AppGraph.store(this).markInterruptedRecordingsPaused()
        SyncScheduler.enqueue(this)
    }
}
