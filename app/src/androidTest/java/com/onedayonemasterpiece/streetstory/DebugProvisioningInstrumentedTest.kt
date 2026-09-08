package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
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
    private val device get() = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())

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

        startProvisioning(backendUrl, token)
        assertTrue(waitForConfig(backendUrl, token))
        assertTrue(device.wait(Until.hasObject(By.text("Истории города")), 5_000))
        device.waitForIdle()

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

        startProvisioning(backendUrl, token)
        assertTrue(waitForConfig(backendUrl, token))
        assertTrue(device.wait(Until.hasObject(By.text("Истории города")), 5_000))
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
        device.waitForIdle()
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
