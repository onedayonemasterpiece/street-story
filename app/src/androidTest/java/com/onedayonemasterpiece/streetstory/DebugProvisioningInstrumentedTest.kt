package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.Intent
import android.view.View
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebugProvisioningInstrumentedTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val device get() = UiDevice.getInstance(instrumentation)

    @Before
    fun resetProvisioningState() {
        context.getSharedPreferences("street_story_config", Context.MODE_PRIVATE).edit().clear().commit()
        context.getSharedPreferences("street_story_secrets", Context.MODE_PRIVATE).edit().clear().commit()
        context.getSharedPreferences("street_story_ui", Context.MODE_PRIVATE).edit().clear().commit()
    }

    @Test
    fun validAdbIntentConfiguresEncryptedTokenRefreshesUiAndReplaysSafely() {
        val backendUrl = "https://street-story-provisioning.example.test"
        val token = "p".repeat(40)

        val firstMain = startProvisioningAndWaitForMain(backendUrl, token)
        assertMainFeedRendered(firstMain)

        val config = ConfigStore(context)
        assertTrue(config.configured)
        assertEquals(backendUrl, config.backendUrl)
        assertEquals(token, config.deviceToken)
        val encryptedBefore = context.getSharedPreferences("street_story_secrets", Context.MODE_PRIVATE)
            .getString("device_token", null)
        assertNotNull(encryptedBefore)
        assertNotEquals(token, encryptedBefore)
        assertFalse(requireNotNull(encryptedBefore).contains(token))
        assertNull(device.findObject(By.text(token)))
        assertNull(device.findObject(By.text("Настроить backend")))

        val replayMain = startProvisioningAndWaitForMain(backendUrl, token)
        assertMainFeedRendered(replayMain)
        val encryptedAfter = context.getSharedPreferences("street_story_secrets", Context.MODE_PRIVATE)
            .getString("device_token", null)
        assertEquals(encryptedBefore, encryptedAfter)
    }

    @Test
    fun invalidProvisioningDoesNotEraseWorkingSecret() {
        val originalUrl = "https://street-story-existing.example.test"
        val originalToken = "s".repeat(40)
        ConfigStore(context).apply {
            backendUrl = originalUrl
            deviceToken = originalToken
        }
        val encryptedBefore = context.getSharedPreferences("street_story_secrets", Context.MODE_PRIVATE)
            .getString("device_token", null)

        startProvisioning("https://street-story-changed.example.test", "bad token")
        instrumentation.waitForIdleSync()
        Thread.sleep(300)

        val after = ConfigStore(context)
        assertTrue(after.configured)
        assertEquals(originalUrl, after.backendUrl)
        assertEquals(originalToken, after.deviceToken)
        assertEquals(
            encryptedBefore,
            context.getSharedPreferences("street_story_secrets", Context.MODE_PRIVATE).getString("device_token", null),
        )
    }

    private fun startProvisioningAndWaitForMain(backendUrl: String, token: String): MainActivity {
        startProvisioning(backendUrl, token)
        assertTrue(waitForConfig(backendUrl, token))
        val launched = waitForResumedMainActivity()
        assertNotNull("ADB provisioning must hand off to a resumed MainActivity", launched)
        return requireNotNull(launched)
    }

    private fun waitForResumedMainActivity(): MainActivity? {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            var found: MainActivity? = null
            instrumentation.runOnMainSync {
                found = ActivityLifecycleMonitorRegistry.getInstance()
                    .getActivitiesInStage(Stage.RESUMED)
                    .filterIsInstance<MainActivity>()
                    .firstOrNull()
            }
            if (found != null) return found
            Thread.sleep(50)
        }
        return null
    }

    private fun assertMainFeedRendered(activity: MainActivity) {
        var rendered = false
        instrumentation.runOnMainSync {
            rendered = containsText(activity.findViewById(android.R.id.content), "Городские истории")
        }
        assertTrue("MainActivity must render the unified feed after provisioning", rendered)
    }

    private fun containsText(view: View?, expected: String): Boolean {
        if (view == null) return false
        if (view is TextView && view.text?.toString() == expected) return true
        if (view is android.view.ViewGroup) {
            for (index in 0 until view.childCount) {
                if (containsText(view.getChildAt(index), expected)) return true
            }
        }
        return false
    }

    private fun startProvisioning(backendUrl: String, token: String) {
        context.startActivity(
            Intent().setClassName(context.packageName, DebugProvisioningActivity::class.java.name)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra(DebugProvisioningPolicy.EXTRA_BACKEND_URL, backendUrl)
                .putExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN, token),
        )
    }

    private fun waitForConfig(backendUrl: String, token: String): Boolean {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            val config = ConfigStore(context)
            if (config.configured && config.backendUrl == backendUrl && config.deviceToken == token) return true
            Thread.sleep(50)
        }
        return false
    }
}
