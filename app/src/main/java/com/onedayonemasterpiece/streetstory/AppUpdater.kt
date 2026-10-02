package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.google.gson.JsonParser
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

data class UpdateInfo(
    val versionCode: Int,
    val versionName: String,
    val apkUrl: String,
)

object AppUpdater {
    private const val RELEASES_URL =
        "https://api.github.com/repos/onedayonemasterpiece/street-story/releases?per_page=20"
    private const val DOWNLOAD_PREFIX =
        "https://github.com/onedayonemasterpiece/street-story/releases/download/"
    private const val APK_NAME = "street-story.apk"
    private const val USER_AGENT = "StreetStoryAndroidUpdater/1.0"
    private const val MAX_APK_BYTES = 250L * 1024L * 1024L
    private val TAG = Regex("^android-v(\\d+)$")

    fun parseLatestRelease(json: String, currentVersionCode: Int): UpdateInfo? {
        val releases = runCatching { JsonParser.parseString(json).asJsonArray }.getOrNull() ?: return null
        var best: UpdateInfo? = null
        for (element in releases) {
            val release = element.takeIf { it.isJsonObject }?.asJsonObject ?: continue
            if (release.get("draft")?.asBoolean == true || release.get("prerelease")?.asBoolean == true) continue
            val tag = release.get("tag_name")?.asString.orEmpty()
            val versionCode = TAG.matchEntire(tag)?.groupValues?.getOrNull(1)?.toIntOrNull() ?: continue
            if (versionCode <= currentVersionCode || versionCode <= (best?.versionCode ?: currentVersionCode)) continue
            val assets = release.getAsJsonArray("assets") ?: continue
            val asset = assets.firstOrNull { item ->
                item.isJsonObject && item.asJsonObject.get("name")?.asString == APK_NAME
            }?.asJsonObject ?: continue
            val url = asset.get("browser_download_url")?.asString.orEmpty()
            if (!url.startsWith(DOWNLOAD_PREFIX) || !url.endsWith("/$APK_NAME")) continue
            best = UpdateInfo(versionCode, "0.1.$versionCode", url)
        }
        return best
    }

    fun checkLatest(currentVersionCode: Int, callback: (Result<UpdateInfo?>) -> Unit) {
        Thread {
            callback(
                runCatching {
                    val connection = open(URL(RELEASES_URL), "application/vnd.github+json")
                    try {
                        val status = connection.responseCode
                        check(status in 200..299) { "GitHub Releases: HTTP $status" }
                        val text = connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
                        parseLatestRelease(text, currentVersionCode)
                    } finally {
                        connection.disconnect()
                    }
                }
            )
        }.start()
    }

    fun canInstallPackages(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O || context.packageManager.canRequestPackageInstalls()

    fun installPermissionIntent(context: Context): Intent =
        Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${context.packageName}"),
        )

    fun download(context: Context, info: UpdateInfo, callback: (Result<File>) -> Unit) {
        Thread {
            callback(
                runCatching {
                    require(info.apkUrl.startsWith(DOWNLOAD_PREFIX) && info.apkUrl.endsWith("/$APK_NAME"))
                    val directory = File(context.cacheDir, "updates").apply { mkdirs() }
                    val target = File(directory, "street-story-${info.versionCode}.apk")
                    val partial = File(directory, target.name + ".part")
                    partial.delete()
                    val connection = open(URL(info.apkUrl), "application/vnd.android.package-archive")
                    try {
                        val status = connection.responseCode
                        check(status in 200..299) { "APK download: HTTP $status" }
                        val declared = connection.contentLengthLong
                        check(declared <= 0L || declared <= MAX_APK_BYTES) { "APK is unexpectedly large" }
                        var total = 0L
                        connection.inputStream.use { input ->
                            partial.outputStream().use { output ->
                                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                                while (true) {
                                    val count = input.read(buffer)
                                    if (count < 0) break
                                    total += count
                                    check(total <= MAX_APK_BYTES) { "APK exceeds size limit" }
                                    output.write(buffer, 0, count)
                                }
                            }
                        }
                        check(total > 4L) { "APK download is empty" }
                        partial.inputStream().use { input ->
                            check(input.read() == 0x50 && input.read() == 0x4b) { "Downloaded file is not an APK archive" }
                        }
                        if (target.exists()) target.delete()
                        check(partial.renameTo(target)) { "Cannot finalize downloaded APK" }
                        target
                    } finally {
                        connection.disconnect()
                        if (partial.exists()) partial.delete()
                    }
                }
            )
        }.start()
    }

    fun install(context: Context, apk: File) {
        val uri = FileProvider.getUriForFile(
            context,
            "${context.packageName}.files",
            apk,
        )
        context.startActivity(
            Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        )
    }

    private fun open(url: URL, accept: String): HttpURLConnection =
        (url.openConnection() as HttpURLConnection).apply {
            connectTimeout = 15_000
            readTimeout = 60_000
            instanceFollowRedirects = true
            setRequestProperty("User-Agent", USER_AGENT)
            setRequestProperty("Accept", accept)
        }
}
