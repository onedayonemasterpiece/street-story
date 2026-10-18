package com.onedayonemasterpiece.streetstory

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import java.io.File
import java.net.URI
import java.nio.file.Files

internal data class DebugProvisioningValues(
    val backendUrl: String,
    val deviceToken: String,
)

internal object DebugProvisioningPolicy {
    const val EXTRA_BACKEND_URL = "street_story_backend_url"
    const val EXTRA_DEVICE_TOKEN = "street_story_device_token"
    const val EXTRA_DEVICE_TOKEN_STAGED = "street_story_device_token_staged"
    const val STAGED_DEVICE_TOKEN_FILE = "adb-device-token"
    private const val MIN_TOKEN_LENGTH = 32
    private const val MAX_TOKEN_LENGTH = 256

    fun parse(rawBackendUrl: String?, rawDeviceToken: String?): DebugProvisioningValues? {
        if (rawBackendUrl == null || rawDeviceToken == null) return null
        val backendUrl = rawBackendUrl.trim().trimEnd('/')
        if (!validBackendUrl(backendUrl) || !validDeviceToken(rawDeviceToken)) return null
        return DebugProvisioningValues(backendUrl, rawDeviceToken)
    }

    fun validBackendUrl(value: String): Boolean {
        val uri = runCatching { URI(value) }.getOrNull() ?: return false
        return uri.scheme.equals("https", ignoreCase = true) &&
            !uri.host.isNullOrBlank() &&
            uri.rawUserInfo == null &&
            uri.rawQuery == null &&
            uri.rawFragment == null
    }

    fun validDeviceToken(value: String): Boolean =
        value.length in MIN_TOKEN_LENGTH..MAX_TOKEN_LENGTH &&
            value.all { !it.isWhitespace() && !it.isISOControl() }
}

class DebugProvisioningActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val rawBackendUrl = intent.getStringExtra(DebugProvisioningPolicy.EXTRA_BACKEND_URL)
        val inlineDeviceToken = intent.getStringExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN)
        val useStagedToken = intent.getBooleanExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN_STAGED, false)
        Log.i("StreetStoryProvisioning", "event=received staged=$useStagedToken inline=${inlineDeviceToken != null} recreated=${savedInstanceState != null}")
        intent.removeExtra(DebugProvisioningPolicy.EXTRA_BACKEND_URL)
        intent.removeExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN)
        intent.removeExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN_STAGED)

        val rawDeviceToken = when {
            inlineDeviceToken != null && useStagedToken -> {
                consumeStagedDeviceToken()
                null
            }
            useStagedToken -> consumeStagedDeviceToken()
            else -> inlineDeviceToken
        }
        val values = DebugProvisioningPolicy.parse(rawBackendUrl, rawDeviceToken)
        if (values == null) {
            Log.i("StreetStoryProvisioning", "event=validation_failed backend_valid=${rawBackendUrl?.let(DebugProvisioningPolicy::validBackendUrl) == true} token_valid=${rawDeviceToken?.let(DebugProvisioningPolicy::validDeviceToken) == true}")
            Toast.makeText(this, "ADB-настройка отклонена: проверь HTTPS URL и device token", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        val config = AppGraph.config(applicationContext)
        if (config.backendUrl != values.backendUrl) config.backendUrl = values.backendUrl
        if (config.deviceToken != values.deviceToken) config.deviceToken = values.deviceToken
        if (!config.configured) {
            Log.i("StreetStoryProvisioning", "event=config_failed backend_present=${!config.backendUrl.isNullOrBlank()} token_present=${!config.deviceToken.isNullOrBlank()}")
            Toast.makeText(this, "ADB-настройка не завершена", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        SyncScheduler.enqueue(this)
        Log.i("StreetStoryProvisioning", "event=configured staged=$useStagedToken")
        Toast.makeText(this, "Street Story backend настроен через ADB", Toast.LENGTH_SHORT).show()
        startActivity(
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),
        )
        finish()
    }

    private fun consumeStagedDeviceToken(): String? {
        val staged = File(filesDir, DebugProvisioningPolicy.STAGED_DEVICE_TOKEN_FILE)
        return try {
            if (!staged.isFile || Files.isSymbolicLink(staged.toPath())) {
                null
            } else {
                staged.readText(Charsets.UTF_8).trim()
            }
        } catch (_: Exception) {
            null
        } finally {
            runCatching { staged.delete() }
        }
    }
}
