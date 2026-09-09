package com.onedayonemasterpiece.streetstory

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import java.net.URI

internal data class DebugProvisioningValues(
    val backendUrl: String,
    val deviceToken: String,
)

internal object DebugProvisioningPolicy {
    const val EXTRA_BACKEND_URL = "street_story_backend_url"
    const val EXTRA_DEVICE_TOKEN = "street_story_device_token"
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
        val rawDeviceToken = intent.getStringExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN)
        intent.removeExtra(DebugProvisioningPolicy.EXTRA_BACKEND_URL)
        intent.removeExtra(DebugProvisioningPolicy.EXTRA_DEVICE_TOKEN)

        val values = DebugProvisioningPolicy.parse(rawBackendUrl, rawDeviceToken)
        if (values == null) {
            Toast.makeText(this, "ADB-настройка отклонена: проверь HTTPS URL и device token", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        val config = AppGraph.config(applicationContext)
        if (config.backendUrl != values.backendUrl) config.backendUrl = values.backendUrl
        if (config.deviceToken != values.deviceToken) config.deviceToken = values.deviceToken
        if (!config.configured) {
            Toast.makeText(this, "ADB-настройка не завершена", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        SyncScheduler.enqueue(this)
        Toast.makeText(this, "Street Story backend настроен через ADB", Toast.LENGTH_SHORT).show()
        // Stay in the existing app task. Clearing the whole task can destroy the
        // provisioning launch before MainActivity handoff is observable and also
        // throws away unrelated UI state. CLEAR_TOP gives us a freshly rendered
        // MainActivity for the new backend configuration without wiping the task.
        startActivity(
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),
        )
        finish()
    }
}
