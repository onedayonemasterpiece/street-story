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
import java.security.MessageDigest
import java.time.OffsetDateTime
import java.util.Locale

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
        require(baseUrl.startsWith("https://"))
        require(token.isNotBlank())
        require(isExplicitTestAlias(safeAlias))

        val photoFile = File(root, "photo.jpg")
        val audioFiles = (1..4).map { File(root, "voice-$it.m4a") }
        require(photoFile.isFile && audioFiles.all { it.isFile })
        val photoSha = sha256(photoFile.readBytes())
        val photoUri = insertIntoMediaStore(photoFile)
        val imported = PhotoImporter.import(context, photoUri)
        assertTrue("Photo must traverse the picker-compatible MediaStore URI", imported.path.isNotBlank())
        assertEquals(photoSha, imported.sha256)
        assertNotNull("Golden Commons JPEG must keep GPS", imported.latitude)
        assertNotNull("Golden Commons JPEG must keep GPS", imported.longitude)
        assertTrue(kotlin.math.abs(requireNotNull(imported.latitude) - FIXTURE_LAT) < 0.0025)
        assertTrue(kotlin.math.abs(requireNotNull(imported.longitude) - FIXTURE_LON) < 0.0025)

        val localStore = AppGraph.store(context)
        val local = localStore.createStory(imported)
        val api = ApiClient(baseUrl, token)
        val created = api.createStory(local)
        require(created.id.isNotBlank())
        localStore.setServerIdentity(local.clientStoryId, created.id)
        val storyId = created.id
        val initialSessions = mutableListOf<String>()
        var scheduledPublicationId: String? = null
        var cancelConfirmed = false
        val evidence = linkedMapOf<String, Any?>()
        try {
            for (index in 0..2) {
                val session = persistPreparedVoice(localStore, local.clientStoryId, "initial", audioFiles[index])
                initialSessions += session.sessionId
                syncVoice(api, storyId, session, localStore.chunks(session.sessionId))
            }
            val beforeResearch = api.getStory(storyId)
            assertEquals(StoryStage.VOICE_READY, beforeResearch.state)

            var story = api.mutate(
                storyId,
                "facts",
                gson.toJson(mapOf("action" to "research")),
                newRequestKey("live-research", "${local.clientStoryId}-initial"),
            )
            story = pollStory(api, storyId) { it.state == StoryStage.REVIEW || it.state == StoryStage.NEEDS_REVIEW }
            var identity = story.visualIdentity
            if (identity?.status !in setOf("match", "owner_confirmed")) {
                val candidate = identity?.candidates?.firstOrNull {
                    it.name.lowercase(Locale.ROOT).contains("brandenburg") ||
                        it.name.lowercase(Locale.ROOT).contains("бранденбург")
                }
                require(candidate != null) { "Expected Brandenburg Gate candidate is absent" }
                story = api.mutate(
                    storyId,
                    "facts",
                    gson.toJson(mapOf("action" to "research", "candidate_id" to candidate.candidateId)),
                    newRequestKey("live-research", "${local.clientStoryId}-confirmed-${candidate.candidateId}"),
                )
                story = pollStory(api, storyId) { it.state == StoryStage.REVIEW }
                identity = story.visualIdentity
            }
            assertTrue(identity?.status in setOf("match", "owner_confirmed"))
            assertEquals(initialSessions, story.researchVoiceIds)
            assertTrue(story.sourceCount > 0)
            assertTrue(story.sources.all { it.title?.isNotBlank() == true && it.url.startsWith("https://") })

            val supported = story.facts.filter { it.evidenceSupported }
            require(supported.isNotEmpty()) { "No evidence-supported facts" }
            val removed = supported.first()
            val keep = supported.drop(1).take(2).map { it.factId }
            story = api.mutate(
                storyId,
                "facts",
                gson.toJson(mapOf("selected_fact_ids" to keep)),
                newRequestKey("live-facts", local.clientStoryId),
            )
            assertFalse(story.draftText.orEmpty().contains(removed.text))

            val refinement = persistPreparedVoice(localStore, local.clientStoryId, "refinement", audioFiles[3])
            syncVoice(api, storyId, refinement, localStore.chunks(refinement.sessionId))
            val beforeUpdate = api.getStory(storyId)
            assertEquals(StoryStage.REVIEW, beforeUpdate.state)
            assertFalse(beforeUpdate.researchVoiceIds.contains(refinement.sessionId))
            story = api.mutate(
                storyId,
                "facts",
                gson.toJson(mapOf("action" to "research")),
                newRequestKey("live-research", "${local.clientStoryId}-refinement"),
            )
            story = pollStory(api, storyId) { it.state == StoryStage.REVIEW }
            val allSessions = initialSessions + refinement.sessionId
            assertEquals(allSessions, story.researchVoiceIds)
            story.facts.firstOrNull { it.factId == removed.factId }?.let { assertFalse(it.selected) }
            assertFalse(story.draftText.orEmpty().contains(removed.text))

            val selectedIds = story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }
            api.mutate(
                storyId,
                "visual",
                gson.toJson(mapOf("selected_fact_ids" to selectedIds)),
                newRequestKey("live-visual", local.clientStoryId),
            )
            story = pollStory(api, storyId, VISUAL_TIMEOUT_MS) { it.state == StoryStage.READY_TO_PUBLISH }
            val rawReady = rawStory(baseUrl, token, storyId)
            val visual = rawReady.requireObject("visual")
            assertEquals(OWNER_PROMPT_SHA256, visual.requireString("prompt_sha256"))
            val imageSha = visual.requireString("selected_sha256")
            val imageOperation = visual.requireString("operation_id")
            val imageAsset = visual.requireString("selected_asset_ref")
            val imagePath = File(root, "processed.img")
            api.downloadAsset(requireNotNull(story.processedImageUrl), imagePath)
            assertEquals(imageSha, sha256(imagePath.readBytes()))

            localStore.setServerSnapshot(
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
            localStore.replaceFacts(story.clientStoryIdOr(local.clientStoryId), story.facts.map { it.local() })
            ResearchProjectionStore(context).replace(local.clientStoryId, story)
            localStore.setProcessedImagePath(local.clientStoryId, imagePath.absolutePath)
            val manualCaption = (story.draftText.orEmpty() + "\n\nРучная правка live E2E.").take(1024)
            localStore.setDraftText(local.clientStoryId, manualCaption)

            val capabilities = api.capabilities()
            val safeDestination = capabilities.destinations.singleOrNull {
                it.alias == safeAlias && it.provider.equals("telegram", ignoreCase = true) && it.status == "supported"
            }
            require(safeDestination != null) { "Safe Telegram alias is not uniquely supported" }
            require(isExplicitTestAlias("${safeDestination.alias} ${safeDestination.label}"))
            localStore.replaceDestinations(local.clientStoryId, listOf(safeDestination.local(selected = true)))

            api.mutate(
                storyId,
                "publish",
                gson.toJson(
                    mapOf(
                        "destinations" to listOf(safeAlias),
                        "delay_minutes" to 1440,
                        "text_override" to manualCaption,
                    )
                ),
                newRequestKey("live-publish", local.clientStoryId),
            )
            val scheduled = pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString in setOf("scheduled", "verified")
            }
            val rawScheduled = rawStory(baseUrl, token, storyId)
            val publication = rawScheduled.requireObject("publication")
            scheduledPublicationId = publication.requireString("publication_id")
            val scheduledRows = scheduled.destinations.filter { it.alias == safeAlias }
            require(scheduledRows.size == 1 && scheduledRows.first().status in setOf("scheduled", "verified"))
            localStore.setServerSnapshot(
                local.clientStoryId,
                StoryStage.SCHEDULED,
                scheduled.placeName,
                scheduled.summary,
                scheduled.draftText,
                scheduled.processedImageUrl,
                scheduled.scheduledFor,
                scheduled.publishedAt,
                scheduled.error?.message,
                scheduled.revision,
            )
            localStore.replaceDestinations(local.clientStoryId, scheduled.destinations.map { it.local() })
            assertEquals(manualCaption, requireNotNull(localStore.story(local.clientStoryId)).draftText)

            launchAndScreenshot()
            evidence.putAll(
                mapOf(
                    "schema_version" to 1,
                    "client_story_id" to local.clientStoryId,
                    "server_story_id" to storyId,
                    "fixture_photo_sha256" to photoSha,
                    "voice_session_ids" to allSessions,
                    "research_revision" to story.researchRevision,
                    "visual_identity_status" to identity?.status,
                    "visual_candidate_id" to identity?.candidateId,
                    "source_count" to story.sourceCount,
                    "source_titles" to story.sources.map { it.title ?: it.url },
                    "selected_fact_ids" to selectedIds,
                    "prompt_sha256" to OWNER_PROMPT_SHA256,
                    "image_operation_id" to imageOperation,
                    "image_asset_ref" to imageAsset,
                    "image_sha256" to imageSha,
                    "publication_id" to scheduledPublicationId,
                    "publication_operation_id" to publication.get("operation_id")?.asString,
                    "scheduled_for" to scheduled.scheduledFor,
                    "destination_alias" to safeAlias,
                    "physical_mic" to false,
                    "prepared_audio" to true,
                    "photo_via_media_store_importer" to true,
                )
            )

            api.mutate(
                storyId,
                "cancel",
                "{}",
                newRequestKey("live-cancel", local.clientStoryId),
            )
            pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
            }
            val cancelled = rawStory(baseUrl, token, storyId)
            assertEquals("cancelled", cancelled.requireObject("publication").requireString("state"))
            cancelConfirmed = true
            evidence["cancel_confirmed"] = true
            evidence["cancel_operation_id"] = cancelled.requireObject("publication").get("cancel_operation_id")?.asString
        } finally {
            if (scheduledPublicationId != null && !cancelConfirmed) {
                runCatching {
                    api.mutate(
                        storyId,
                        "cancel",
                        "{}",
                        newRequestKey("live-cleanup", local.clientStoryId),
                    )
                    pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                        rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
                    }
                    evidence["best_effort_cancel_confirmed"] = true
                }
            }
            File(root, "evidence.json").writeText(gson.toJson(evidence))
        }
        assertTrue(cancelConfirmed)
    }

    private fun persistPreparedVoice(
        store: StoryStore,
        storyId: String,
        kind: String,
        audio: File,
    ): VoiceSessionSnapshot {
        val session = store.createVoiceSession(storyId, kind, "github-actions-prepared-audio")
        val data = audio.readBytes()
        val sha = sha256(data)
        store.addChunk(
            session.sessionId,
            0,
            0,
            6000,
            0,
            6100,
            audio.absolutePath,
            sha,
            AudioProfile.MIME_M4A,
        )
        store.finishVoiceSession(session.sessionId, OffsetDateTime.now().toString(), 6100, 100)
        return requireNotNull(store.voiceSession(session.sessionId))
    }

    private fun syncVoice(
        api: ApiClient,
        storyId: String,
        session: VoiceSessionSnapshot,
        chunks: List<ChunkRecord>,
    ) {
        api.openVoiceSession(storyId, session)
        chunks.forEach { api.uploadChunk(storyId, session, it) }
        val receipt = api.completeVoice(storyId, session, chunks)
        assertTrue(receipt.recordingFinished)
        assertEquals(chunks.map { it.sha256.lowercase() }, receipt.received.sortedBy { it.index }.map { it.sha256.lowercase() })
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
            if (last.state == StoryStage.NEEDS_REVIEW && code != "visual_identity_uncertain") {
                error("Story needs review: $code ${last.error?.message.orEmpty()}")
            }
            Thread.sleep(5_000)
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
        )
    ).also { uri ->
        context.contentResolver.openOutputStream(uri, "w")!!.use { out -> photo.inputStream().use { it.copyTo(out) } }
        context.contentResolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
    }

    private fun launchAndScreenshot() {
        context.startActivity(
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
        Thread.sleep(2_500)
        val bitmap: Bitmap = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        FileOutputStream(File(root, "preview.png")).use { output ->
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
        }
    }

    private fun sha256(data: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

    private fun JsonObject.requireString(name: String): String =
        get(name)?.takeIf { !it.isJsonNull }?.asString?.takeIf { it.isNotBlank() }
            ?: error("Missing $name")

    private fun JsonObject.requireObject(name: String): JsonObject =
        get(name)?.takeIf { it.isJsonObject }?.asJsonObject ?: error("Missing object $name")

    private fun FactWire.local() = FactSnapshot(
        factId,
        text,
        confidence,
        evidenceSupported,
        selected && evidenceSupported,
        gson.toJson(sources),
    )

    private fun DestinationWire.local(selected: Boolean = this.selected) = DestinationSnapshot(
        alias,
        label,
        provider,
        status,
        selected,
    )

    private fun StoryWire.clientStoryIdOr(fallback: String): String = clientStoryId.ifBlank { fallback }

    private fun isExplicitTestAlias(value: String): Boolean {
        val lowered = value.lowercase(Locale.ROOT)
        return listOf("test", "тест", "safe", "e2e").any { it in lowered }
    }

    companion object {
        private const val FIXTURE_LAT = 54.697111
        private const val FIXTURE_LON = 20.494111
        private const val OWNER_PROMPT_SHA256 = "4eab6d0cfcafc84881cad86380baa9920785b7e18e9a934923966995802380a3"
        private const val RESEARCH_TIMEOUT_MS = 12L * 60 * 1000
        private const val VISUAL_TIMEOUT_MS = 12L * 60 * 1000
        private const val SOCIAL_TIMEOUT_MS = 6L * 60 * 1000
    }
}
