package com.onedayonemasterpiece.streetstory

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class LiveGoldenInstrumentedTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val root = File(context.filesDir, "live-golden")
    private val gson = Gson()

    @Test
    fun androidClientToNativeTelegramGoldenPath() {
        val configFile = File(root, "config.json")
        assumeTrue("Live golden fixture is not provisioned", configFile.isFile)
        val config = JsonParser.parseString(configFile.readText()).asJsonObject
        val baseUrl = config.requireString("backend_url")
        val safeAlias = config.requireString("safe_alias")
        val token = File(root, "token.txt").readText().trim()
        require(baseUrl.startsWith("https://") && token.isNotBlank())
        require(isExplicitTestAlias(safeAlias))

        val photoFile = File(root, "photo.jpg")
        val pcmFiles = (1..10).map { File(root, "voice-$it.pcm") }
        require(photoFile.isFile && pcmFiles.all(File::isFile))

        val appConfig = AppGraph.config(context)
        appConfig.backendUrl = baseUrl
        appConfig.deviceToken = token

        val photoSha = sha256(photoFile.readBytes())
        val imported = PhotoImporter.import(context, insertIntoMediaStore(photoFile))
        assertEquals(photoSha, imported.sha256)
        assertNotNull(imported.latitude)
        assertNotNull(imported.longitude)
        assertTrue(kotlin.math.abs(requireNotNull(imported.latitude) - FIXTURE_LAT) < 0.0025)
        assertTrue(kotlin.math.abs(requireNotNull(imported.longitude) - FIXTURE_LON) < 0.0025)

        val store = AppGraph.store(context)
        val local = store.createStory(imported)
        val api = ApiClient(baseUrl, token)
        val created = api.createStory(local)
        require(created.id.isNotBlank())
        store.setServerIdentity(local.clientStoryId, created.id)
        val storyId = created.id
        val live = AppGraph.live(context)
        val evidence = linkedMapOf<String, Any?>()
        var publicationScheduled = false
        var cancelConfirmed = false

        try {
            val ready = CountDownLatch(1)
            var liveError: String? = null
            live.start(local.clientStoryId) { ok, error ->
                if (!ok) liveError = error ?: "Live start failed"
                ready.countDown()
            }
            assertTrue("Live start timed out", ready.await(45, TimeUnit.SECONDS))
            check(liveError == null) { liveError.orEmpty() }
            assertTrue(live.isActiveFor(local.clientStoryId))

            speak(live, pcmFiles[0])
            awaitAnswer(live, "initial context")

            speak(live, pcmFiles[1])
            awaitAnswer(live, "research request")
            var story = pollStory(api, storyId, RESEARCH_TIMEOUT_MS) {
                it.state in setOf(StoryStage.REVIEW, StoryStage.NEEDS_REVIEW)
            }

            if (story.visualIdentity?.status !in setOf("match", "owner_confirmed")) {
                speak(live, pcmFiles[2])
                awaitAnswer(live, "identity confirmation")
                story = pollStory(api, storyId, RESEARCH_TIMEOUT_MS) {
                    it.state == StoryStage.REVIEW &&
                        it.visualIdentity?.status in setOf("match", "owner_confirmed") &&
                        it.sourceCount > 0
                }
            }
            assertTrue(story.visualIdentity?.status in setOf("match", "owner_confirmed"))
            assertTrue(story.sourceCount > 0)
            assertTrue(story.sources.all { !it.title.isNullOrBlank() && it.url.startsWith("https://") })
            require(story.facts.count { it.evidenceSupported } >= 2)

            speak(live, pcmFiles[3])
            awaitAnswer(live, "fact selection")
            story = pollStory(api, storyId) {
                it.facts.count { fact -> fact.selected && fact.evidenceSupported } >= 1 &&
                    !it.draftText.isNullOrBlank()
            }
            val selectedFactIds = story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }

            val beforeEdit = requireNotNull(story.draftText)
            speak(live, pcmFiles[4])
            awaitAnswer(live, "text edit")
            story = pollStory(api, storyId) { !it.draftText.isNullOrBlank() && it.draftText != beforeEdit }
            val editedText = requireNotNull(story.draftText)

            speak(live, pcmFiles[5])
            waitUntil(60_000, "literal mode did not start") { live.snapshot().literalMode }
            speak(live, pcmFiles[6])
            speak(live, pcmFiles[7])
            waitUntil(90_000, "literal mode did not finish") { !live.snapshot().literalMode }
            awaitAnswer(live, "literal finish")
            story = api.getStory(storyId)
            val literalText = LITERAL_TEXT
            assertTrue("Literal text is absent", story.draftText.orEmpty().contains(literalText, ignoreCase = true))

            val afterLiteral = requireNotNull(story.draftText)
            speak(live, pcmFiles[8])
            awaitAnswer(live, "post-literal edit")
            story = pollStory(api, storyId) {
                !it.draftText.isNullOrBlank() &&
                    it.draftText != afterLiteral &&
                    it.draftText!!.contains(literalText, ignoreCase = true)
            }
            val afterProtectedEdit = requireNotNull(story.draftText)

            live.sendText("Верни предыдущую правку.")
            awaitAnswer(live, "undo")
            story = pollStory(api, storyId) { it.draftText == afterLiteral }
            assertTrue(story.draftText.orEmpty().contains(literalText, ignoreCase = true))

            val textBeforeVisual = requireNotNull(story.draftText)
            speak(live, pcmFiles[9])
            awaitAnswer(live, "visual-only edit")
            story = pollStory(api, storyId, VISUAL_TIMEOUT_MS) {
                it.state == StoryStage.READY_TO_PUBLISH && !it.processedImageUrl.isNullOrBlank()
            }
            assertEquals("Visual-only change rewrote text", textBeforeVisual, story.draftText)
            val rawReady = rawStory(baseUrl, token, storyId)
            val visual = rawReady.requireObject("visual")
            assertEquals(OWNER_PROMPT_SHA256, visual.requireString("prompt_sha256"))
            val imageOperation = visual.requireString("operation_id")
            val imageAsset = visual.requireString("selected_asset_ref")
            val imageSha = visual.requireString("selected_sha256")

            val processed = File(root, "processed.img")
            api.downloadAsset(requireNotNull(story.processedImageUrl), processed)
            assertEquals(imageSha, sha256(processed.readBytes()))
            store.setServerSnapshot(
                local.clientStoryId,
                story.state,
                story.placeName,
                story.summary,
                story.draftText,
                story.processedImageUrl,
                story.scheduledFor,
                story.publishedAt,
                story.error?.message,
                story.revision,
            )
            store.replaceFacts(local.clientStoryId, story.facts.map { it.local() })
            ResearchProjectionStore(context).replace(local.clientStoryId, story)
            store.setProcessedImagePath(local.clientStoryId, processed.absolutePath)
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
                .edit().putString("active_story_id", local.clientStoryId).apply()
            launchAndScreenshot()

            val safe = api.capabilities().destinations.singleOrNull {
                it.alias == safeAlias && it.provider.equals("telegram", true) && it.status == "supported"
            } ?: error("Safe Telegram alias is not uniquely supported")
            require(isExplicitTestAlias("${safe.alias} ${safe.label}"))

            val scheduledAt = OffsetDateTime.now(ZoneId.of("Europe/Kaliningrad"))
                .plusHours(24)
                .withSecond(0)
                .withNano(0)
            live.sendText(
                "Подготовь публикацию именно текущих текста и картинки в канал alias $safeAlias " +
                    "на ${scheduledAt.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)}, " +
                    "timezone Europe/Kaliningrad. Ничего пока не публикуй."
            )
            waitUntil(90_000, "publication confirmation was not prepared") {
                live.snapshot().confirmation != null
            }
            val confirmation = requireNotNull(live.snapshot().confirmation)
            assertEquals(story.draftText, confirmation.text)
            assertEquals(listOf(safeAlias), confirmation.destinations)

            live.sendText("Подтверждаю именно показанную карточку публикации.")
            awaitAnswer(live, "publication confirmation")
            val scheduled = pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId)
                    .getAsJsonObject("publication")?.get("state")?.asString in setOf("scheduled", "verified")
            }
            val rawScheduled = rawStory(baseUrl, token, storyId)
            val publication = rawScheduled.requireObject("publication")
            val publicationId = publication.requireString("publication_id")
            publicationScheduled = true

            live.sendText("Отмени текущую запланированную публикацию.")
            awaitAnswer(live, "publication cancel")
            pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId)
                    .getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
            }
            val cancelled = rawStory(baseUrl, token, storyId).requireObject("publication")
            assertEquals("cancelled", cancelled.requireString("state"))
            cancelConfirmed = true

            evidence.putAll(
                mapOf(
                    "schema_version" to 3,
                    "client_story_id" to local.clientStoryId,
                    "server_story_id" to storyId,
                    "fixture_photo_sha256" to photoSha,
                    "live_provider" to "gemini-3.8-live",
                    "prepared_pcm_after_capture_boundary" to true,
                    "physical_mic" to false,
                    "legacy_voice_endpoint_used" to false,
                    "research_revision" to scheduled.researchRevision,
                    "visual_identity_status" to scheduled.visualIdentity?.status,
                    "source_count" to scheduled.sourceCount,
                    "selected_fact_ids" to selectedFactIds,
                    "literal_text_preserved" to true,
                    "visual_only_text_preserved" to true,
                    "prompt_sha256" to OWNER_PROMPT_SHA256,
                    "image_operation_id" to imageOperation,
                    "image_asset_ref" to imageAsset,
                    "image_sha256" to imageSha,
                    "publication_id" to publicationId,
                    "scheduled_for" to scheduled.scheduledFor,
                    "destination_alias" to safeAlias,
                    "cancel_confirmed" to true,
                    "cancel_operation_id" to cancelled.get("cancel_operation_id")?.asString,
                )
            )
        } finally {
            if (publicationScheduled && !cancelConfirmed) {
                runCatching {
                    if (live.isActiveFor(local.clientStoryId)) {
                        live.sendText("Аварийная очистка теста: отмени текущую запланированную публикацию.")
                        waitUntil(90_000, "Live cleanup cancel failed") {
                            rawStory(baseUrl, token, storyId)
                                .getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
                        }
                        evidence["best_effort_live_cancel_confirmed"] = true
                    } else {
                        api.mutate(storyId, "cancel", "{}", newRequestKey("emergency-cleanup", local.clientStoryId))
                        pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                            rawStory(baseUrl, token, storyId)
                                .getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
                        }
                        evidence["best_effort_legacy_cleanup_only"] = true
                    }
                }
            }
            live.stopLocal(sendRemote = true)
            File(root, "evidence.json").writeText(gson.toJson(evidence))
            store.close()
        }
        assertTrue(cancelConfirmed)
    }

    private fun speak(live: LiveSessionController, pcm: File) {
        val bytes = pcm.readBytes()
        require(bytes.size % 2 == 0)
        val shorts = ShortArray(bytes.size / 2)
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(shorts)
        var offset = 0
        while (offset < shorts.size) {
            val end = minOf(offset + PCM_CHUNK_SAMPLES, shorts.size)
            live.submitPcm(shorts.copyOfRange(offset, end))
            offset = end
            Thread.sleep(PCM_CHUNK_SLEEP_MS)
        }
        live.endSpeech()
    }

    private fun awaitAnswer(live: LiveSessionController, label: String) {
        val before = live.snapshot().assistantText
        waitUntil(120_000, "Live answer timed out: $label") {
            val state = live.snapshot()
            state.error?.let { error("Live failed during $label: $it") }
            state.active && state.status == "Слушаю" &&
                !state.assistantText.isNullOrBlank() &&
                state.assistantText != before
        }
    }

    private fun waitUntil(timeoutMs: Long, message: String, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return
            Thread.sleep(250)
        }
        error(message)
    }

    private fun pollStory(
        api: ApiClient,
        storyId: String,
        timeoutMs: Long = RESEARCH_TIMEOUT_MS,
        predicate: (StoryWire) -> Boolean,
    ): StoryWire {
        val deadline = System.currentTimeMillis() + timeoutMs
        var last: StoryWire? = null
        while (System.currentTimeMillis() < deadline) {
            last = api.getStory(storyId)
            if (predicate(last)) return last
            val code = last.error?.code.orEmpty()
            if (last.state == StoryStage.NEEDS_REVIEW && code !in setOf("", "visual_identity_uncertain")) {
                error("Story needs review: $code ${last.error?.message.orEmpty()}")
            }
            Thread.sleep(2_000)
        }
        error("Timed out waiting for story; last=${last?.state}")
    }

    private fun rawStory(baseUrl: String, token: String, storyId: String): JsonObject {
        val connection = (URL(baseUrl.trimEnd('/') + "/v1/stories/$storyId").openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 15_000
            readTimeout = 35_000
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/json")
        }
        require(connection.responseCode in 200..299)
        return JsonParser.parseString(connection.inputStream.bufferedReader().use { it.readText() }).asJsonObject
    }

    private fun insertIntoMediaStore(photo: File) = requireNotNull(
        context.contentResolver.insert(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            ContentValues().apply {
                put(MediaStore.Images.Media.DISPLAY_NAME, "street-story-golden-${System.currentTimeMillis()}.jpg")
                put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                put(MediaStore.Images.Media.IS_PENDING, 1)
            },
        ),
    ).also { uri ->
        context.contentResolver.openOutputStream(uri, "w")!!.use { out -> photo.inputStream().use { it.copyTo(out) } }
        context.contentResolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
    }

    private fun launchAndScreenshot() {
        context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        Thread.sleep(2_500)
        val bitmap: Bitmap = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        FileOutputStream(File(root, "preview.png")).use { out ->
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out))
        }
    }

    private fun sha256(data: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

    private fun JsonObject.requireString(name: String): String =
        get(name)?.takeIf { !it.isJsonNull }?.asString?.takeIf { it.isNotBlank() } ?: error("Missing $name")

    private fun JsonObject.requireObject(name: String): JsonObject =
        get(name)?.takeIf { it.isJsonObject }?.asJsonObject ?: error("Missing object $name")

    private fun FactWire.local() =
        FactSnapshot(factId, text, confidence, evidenceSupported, selected && evidenceSupported, gson.toJson(sources))

    private fun isExplicitTestAlias(value: String): Boolean {
        val lowered = value.lowercase(Locale.ROOT)
        return listOf("test", "тест", "safe", "e2e").any { it in lowered }
    }

    companion object {
        private const val FIXTURE_LAT = 54.697111
        private const val FIXTURE_LON = 20.494111
        private const val OWNER_PROMPT_SHA256 = "4eab6d0cfcafc84881cad86380baa9920785b7e18e9a934923966995802380a3"
        private const val LITERAL_TEXT = "Я люблю этот город за моменты, когда знакомая улица вдруг становится незнакомой"
        private const val PCM_CHUNK_SAMPLES = 4096
        private const val PCM_CHUNK_SLEEP_MS = 260L
        private const val RESEARCH_TIMEOUT_MS = 12L * 60 * 1000
        private const val VISUAL_TIMEOUT_MS = 12L * 60 * 1000
        private const val SOCIAL_TIMEOUT_MS = 6L * 60 * 1000
    }
}
