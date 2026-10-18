package com.onedayonemasterpiece.streetstory

import android.view.ViewGroup
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class UiSmokeTest {
    @Test
    fun feedLaunchesAndRespectsSystemBars() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val content = activity.findViewById<ViewGroup>(android.R.id.content)
                assertNotNull(content)
                val appRoot = content.getChildAt(0)
                assertNotNull(appRoot)
                val bars = ViewCompat.getRootWindowInsets(appRoot)
                    ?.getInsets(WindowInsetsCompat.Type.systemBars())
                if (bars != null) {
                    assertTrue("top content must clear the status bar", appRoot.paddingTop >= bars.top)
                    assertTrue("bottom dock must clear Android navigation", appRoot.paddingBottom >= bars.bottom)
                }
            }
        }
    }
}
