package com.onedayonemasterpiece.streetstory

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.net.HttpURLConnection
import java.net.URL

@RunWith(AndroidJUnit4::class)
class PhysicalDeviceLiveSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    @Test
    fun provisionedPhoneReachesExactLiveBackend() {
        val config = ConfigStore(context)
        assertTrue("Phone must be provisioned before live smoke", config.configured)
        val backend = requireNotNull(config.backendUrl) { "Configured backend URL is missing" }.trimEnd('/')
        assertTrue("Physical acceptance requires HTTPS backend", backend.startsWith("https://"))

        val health = requestJson("$backend/healthz")
        assertEquals(true, health.optBoolean("ok"))
        assertEquals(
            "Installed APK source SHA must equal deployed backend SHA",
            BuildConfig.SOURCE_SHA,
            health.optString("source_sha"),
        )

        val capabilities = requestJson(
            "$backend/v1/capabilities",
            bearer = config.deviceToken,
        )
        val destinations = capabilities.optJSONArray("destinations")
        requireNotNull(destinations) { "Capabilities must contain destinations" }
        var telegramSupported = false
        for (index in 0 until destinations.length()) {
            val row = destinations.optJSONObject(index) ?: continue
            if (
                row.optString("provider") == "telegram" &&
                row.optString("status") == "supported"
            ) {
                telegramSupported = true
            }
        }
        assertTrue("Live backend must expose a supported Telegram destination", telegramSupported)
    }

    private fun requestJson(url: String, bearer: String? = null): JSONObject {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 15_000
            connection.readTimeout = 20_000
            connection.setRequestProperty("Accept", "application/json")
            if (!bearer.isNullOrBlank()) {
                connection.setRequestProperty("Authorization", "Bearer $bearer")
            }
            val status = connection.responseCode
            assertTrue("HTTP $status from $url", status in 200..299)
            return JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
        } finally {
            connection.disconnect()
        }
    }
}
