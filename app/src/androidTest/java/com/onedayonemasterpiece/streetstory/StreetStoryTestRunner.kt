package com.onedayonemasterpiece.streetstory

import android.app.Application
import android.os.Bundle
import androidx.test.runner.AndroidJUnitRunner
import androidx.work.testing.WorkManagerTestInitHelper

class StreetStoryTestRunner : AndroidJUnitRunner() {
    private var fixtureSync = false

    override fun onCreate(arguments: Bundle?) {
        fixtureSync = arguments?.getString("street_story_fixture_sync") == "true"
        super.onCreate(arguments)
    }

    override fun callApplicationOnCreate(app: Application) {
        if (fixtureSync) {
            // StreetStoryApp.onCreate already enqueues the first constrained sync.
            // TestScheduler holds it until a test explicitly satisfies constraints.
            WorkManagerTestInitHelper.initializeTestWorkManager(app)
        }
        super.callApplicationOnCreate(app)
    }
}
