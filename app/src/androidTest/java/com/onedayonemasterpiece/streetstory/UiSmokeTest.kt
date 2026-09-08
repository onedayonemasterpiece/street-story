package com.onedayonemasterpiece.streetstory

import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class UiSmokeTest {
    @Test fun feedLaunchesAndRenders(){ActivityScenario.launch(MainActivity::class.java).use{scenario->scenario.onActivity{activity->assertNotNull(activity.findViewById<android.view.View>(android.R.id.content))}}}
}
