package com.onedayonemasterpiece.streetstory

import android.content.Context

class ConfigStore(context: Context) {
    private val prefs = context.getSharedPreferences("street_story_config", Context.MODE_PRIVATE)
    private val secrets = SecretStore(context)
    var backendUrl: String?
        get() = prefs.getString("backend_url", BuildConfig.DEFAULT_BACKEND_URL).orEmpty().trim().trimEnd('/').ifBlank { null }
        set(value) { prefs.edit().putString("backend_url", value.orEmpty().trim().trimEnd('/')).apply() }
    var deviceToken: String?
        get() = secrets.get("device_token")
        set(value) { if (value.isNullOrBlank()) secrets.remove("device_token") else secrets.put("device_token", value.trim()) }
    val configured: Boolean get() = !backendUrl.isNullOrBlank() && !deviceToken.isNullOrBlank()
    val autoSilenceEnabled: Boolean get() = true
}
