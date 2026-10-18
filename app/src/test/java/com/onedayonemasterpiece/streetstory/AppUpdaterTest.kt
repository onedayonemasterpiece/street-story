package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class AppUpdaterTest {
    @Test
    fun selectsNewestStableStreetStoryApkAboveCurrentVersion() {
        val json = """
            [
              {
                "tag_name":"android-v12",
                "draft":false,
                "prerelease":false,
                "assets":[{"name":"street-story.apk","browser_download_url":"https://github.com/onedayonemasterpiece/street-story/releases/download/android-v12/street-story.apk"}]
              },
              {
                "tag_name":"android-v14",
                "draft":false,
                "prerelease":false,
                "assets":[{"name":"street-story.apk","browser_download_url":"https://github.com/onedayonemasterpiece/street-story/releases/download/android-v14/street-story.apk"}]
              },
              {
                "tag_name":"android-v99",
                "draft":true,
                "prerelease":false,
                "assets":[{"name":"street-story.apk","browser_download_url":"https://github.com/onedayonemasterpiece/street-story/releases/download/android-v99/street-story.apk"}]
              }
            ]
        """.trimIndent()

        val update = AppUpdater.parseLatestRelease(json, 12)
        requireNotNull(update)
        assertEquals(14, update.versionCode)
        assertEquals("0.1.14", update.versionName)
    }

    @Test
    fun rejectsNonCanonicalDownloadAndOlderVersions() {
        val json = """
            [
              {
                "tag_name":"android-v13",
                "draft":false,
                "prerelease":false,
                "assets":[{"name":"street-story.apk","browser_download_url":"https://example.com/street-story.apk"}]
              },
              {
                "tag_name":"android-v11",
                "draft":false,
                "prerelease":false,
                "assets":[{"name":"street-story.apk","browser_download_url":"https://github.com/onedayonemasterpiece/street-story/releases/download/android-v11/street-story.apk"}]
              }
            ]
        """.trimIndent()

        assertNull(AppUpdater.parseLatestRelease(json, 12))
    }
}
