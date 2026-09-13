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
        require(baseUrl.startsWith("https://") && token.isNotBlank())
        require(isExplicitTestAlias(safeAlias))

        val photoFile = File(root, "photo.jpg")
        val audioFiles = (1..4).map { File(root, "voice-$it.m4a") }
        require(photoFile.isFile && audioFiles.all(File::isFile))

        val photoSha = sha256(photoFile.readBytes())
        val imported = PhotoImporter.import(context, insertIntoMediaStore(photoFile))
        assertEquals(photoSha, imported.sha256)
        assertNotNull(imported.latitude)
        assertNotNull(imported.longitude)
        assertTrue(kotlin.math.abs(requireNotNull(imported.latitude) - FIXTURE_LAT) < 0.0025)
        assertTrue(kotlin.math.abs(requireNotNull(imported.longitude) - FIXTURE_LON) < 0.0025)

        val store = StoryStore(context)
        val local = store.createStory(imported)
        val api = ApiClient(baseUrl, token)
        val created = api.createStory(local)
        require(created.id.isNotBlank())
        store.setServerIdentity(local.clientStoryId, created.id)
        val storyId = created.id
        val initialIds = mutableListOf<String>()
        val evidence = linkedMapOf<String, Any?>()
        var publicationId: String? = null
        var cancelConfirmed = false

        try {
            repeat(3) { index ->
                val session = preparedVoice(store, local.clientStoryId, RecordingKind.INITIAL, audioFiles[index])
                initialIds += session.sessionId
                syncVoice(api, storyId, session, store.chunks(session.sessionId))
            }
            assertEquals(StoryStage.VOICE_READY, api.getStory(storyId).state)

            var story = startResearch(api, storyId, local.clientStoryId, "initial")
            var identity = story.visualIdentity
            if (identity?.status !in setOf("match", "owner_confirmed")) {
                val candidate = identity?.candidates?.firstOrNull {
                    val name = it.name.lowercase(Locale.ROOT)
                    "brandenburg" in name || "бранденбург" in name
                } ?: error("Expected Brandenburg Gate candidate is absent")
                story = startResearch(api, storyId, local.clientStoryId, "confirmed", candidate.candidateId)
                identity = story.visualIdentity
            }
            assertTrue(identity?.status in setOf("match", "owner_confirmed"))
            assertEquals(initialIds, story.researchVoiceIds)
            assertTrue(story.sourceCount > 0)
            assertTrue(story.sources.all { !it.title.isNullOrBlank() && it.url.startsWith("https://") })

            val supported = story.facts.filter { it.evidenceSupported }
            require(supported.size >= 2) { "Golden run requires at least two supported claims" }
            val removed = supported.first()
            val keptIds = supported.drop(1).take(2).map { it.factId }
            story = api.mutate(
                storyId,
                "facts",
                gson.toJson(mapOf("selected_fact_ids" to keptIds)),
                newRequestKey("live-facts", local.clientStoryId),
            )
            assertFalse(story.draftText.orEmpty().contains(removed.text))
            assertFalse(rawStory(baseUrl, token, storyId).get("image_notes")?.asString.orEmpty().contains(removed.text))

            val refinement = preparedVoice(store, local.clientStoryId, RecordingKind.REFINEMENT, audioFiles[3])
            syncVoice(api, storyId, refinement, store.chunks(refinement.sessionId))
            val beforeUpdate = api.getStory(storyId)
            assertEquals(StoryStage.REVIEW, beforeUpdate.state)
            assertFalse(refinement.sessionId in beforeUpdate.researchVoiceIds)

            story = startResearch(api, storyId, local.clientStoryId, "refined")
            val allVoiceIds = initialIds + refinement.sessionId
            assertEquals(allVoiceIds, story.researchVoiceIds)
            story.facts.firstOrNull { it.factId == removed.factId }?.let { assertFalse(it.selected) }
            assertFalse(story.draftText.orEmpty().contains(removed.text))
            val rawRefined = rawStory(baseUrl, token, storyId)
            assertFalse(rawRefined.get("image_notes")?.asString.orEmpty().contains(removed.text))

            val selectedIds = story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }
            require(selectedIds.isNotEmpty())
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
            val imageOperation = visual.requireString("operation_id")
            val imageAsset = visual.requireString("selected_asset_ref")
            val imageSha = visual.requireString("selected_sha256")
            val processed = File(root, "processed.img")
            api.downloadAsset(requireNotNull(story.processedImageUrl), processed)
            assertEquals(imageSha, sha256(processed.readBytes()))

            val manualCaption = (story.draftText.orEmpty() + "\n\nРучная правка live E2E.").take(1024)
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
            store.setDraftText(local.clientStoryId, manualCaption)

            val safe = api.capabilities().destinations.singleOrNull {
                it.alias == safeAlias && it.provider.equals("telegram", true) && it.status == "supported"
            } ?: error("Safe Telegram alias is not uniquely supported")
            require(isExplicitTestAlias("${safe.alias} ${safe.label}"))
            store.replaceDestinations(local.clientStoryId, listOf(safe.local(true)))

            api.mutate(
                storyId,
                "publish",
                gson.toJson(mapOf(
                    "destinations" to listOf(safeAlias),
                    "delay_minutes" to 1440,
                    "text_override" to manualCaption,
                )),
                newRequestKey("live-publish", local.clientStoryId),
            )
            val scheduled = pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString in setOf("scheduled", "verified")
            }
            val rawScheduled = rawStory(baseUrl, token, storyId)
            val publication = rawScheduled.requireObject("publication")
            publicationId = publication.requireString("publication_id")
            val providerRows = scheduled.destinations.filter { it.alias == safeAlias }
            require(providerRows.size == 1 && providerRows.single().status in setOf("scheduled", "verified"))

            // Mirror the real SyncWorker policy: manual caption wins over the stale research draft
            // once the provider-native post has been scheduled.
            val effectiveDraft = effectiveSnapshotDraft(StoryStage.SCHEDULED, manualCaption, scheduled.draftText)
            store.setServerSnapshot(
                local.clientStoryId,
                StoryStage.SCHEDULED,
                scheduled.placeName,
                scheduled.summary,
                effectiveDraft,
                scheduled.processedImageUrl,
                scheduled.scheduledFor,
                scheduled.publishedAt,
                scheduled.error?.message,
                scheduled.revision,
            )
            store.replaceDestinations(local.clientStoryId, scheduled.destinations.map { it.local() })
            assertEquals(manualCaption, requireNotNull(store.story(local.clientStoryId)).draftText)
            launchAndScreenshot()

            evidence.putAll(mapOf(
                "schema_version" to 2,
                "client_story_id" to local.clientStoryId,
                "server_story_id" to storyId,
                "fixture_photo_sha256" to photoSha,
                "voice_session_ids" to allVoiceIds,
                "research_revision" to story.researchRevision,
                "visual_identity_status" to identity?.status,
                "visual_candidate_id" to identity?.candidateId,
                "source_count" to story.sourceCount,
                "sources" to story.sources.map { mapOf("title" to (it.title ?: it.url), "url" to it.url) },
                "selected_fact_ids" to selectedIds,
                "removed_fact_id" to removed.factId,
                "prompt_sha256" to OWNER_PROMPT_SHA256,
                "image_operation_id" to imageOperation,
                "image_asset_ref" to imageAsset,
                "image_sha256" to imageSha,
                "publication_id" to publicationId,
                "publication_operation_id" to publication.get("operation_id")?.asString,
                "scheduled_for" to scheduled.scheduledFor,
                "destination_alias" to safeAlias,
                "manual_caption_sha256" to sha256(manualCaption.toByteArray()),
                "physical_mic" to false,
                "prepared_audio" to true,
                "photo_via_media_store_importer" to true,
            ))

            api.mutate(storyId, "cancel", "{}", newRequestKey("live-cancel", local.clientStoryId))
            pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
            }
            val cancelled = rawStory(baseUrl, token, storyId).requireObject("publication")
            assertEquals("cancelled", cancelled.requireString("state"))
            cancelConfirmed = true
            evidence["cancel_confirmed"] = true
            evidence["cancel_operation_id"] = cancelled.get("cancel_operation_id")?.asString
        } finally {
            if (publicationId != null && !cancelConfirmed) {
                runCatching {
                    api.mutate(storyId, "cancel", "{}", newRequestKey("live-cleanup", local.clientStoryId))
                    pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                        rawStory(baseUrl, token, storyId).getAsJsonObject("publication")?.get("state")?.asString == "cancelled"
                    }
                    evidence["best_effort_cancel_confirmed"] = true
                }
            }
            File(root, "evidence.json").writeText(gson.toJson(evidence))
            store.close()
        }
        assertTrue(cancelConfirmed)
    }

    private fun startResearch(
        api: ApiClient,
        storyId: String,
        clientId: String,
        ordinal: String,
        candidateId: String? = null,
    ): StoryWire {
        val payload = linkedMapOf<String, Any>("action" to "research")
        candidateId?.let { payload["candidate_id"] = it }
        api.mutate(storyId, "facts", gson.toJson(payload), newRequestKey("live-research", "$clientId-$ordinal"))
        return pollStory(api, storyId) { it.state in setOf(StoryStage.REVIEW, StoryStage.NEEDS_REVIEW) }
    }

    private fun preparedVoice(store: StoryStore, storyId: String, kind: String, audio: File): VoiceSessionSnapshot {
        val session = store.createVoiceSession(storyId, kind, "github-actions-prepared-audio")
        val sha = sha256(audio.readBytes())
        store.addChunk(session.sessionId, 0, 0, 6000, 0, 6100, audio.absolutePath, sha, AudioProfile.MIME_M4A)
        store.finishVoiceSession(session.sessionId, OffsetDateTime.now().toString(), 6100, 100)
        return requireNotNull(store.voiceSession(session.sessionId))
    }

    private fun syncVoice(api: ApiClient, storyId: String, session: VoiceSessionSnapshot, chunks: List<ChunkRecord>) {
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
        ),
    ).also { uri ->
        context.contentResolver.openOutputStream(uri, "w")!!.use { out -> photo.inputStream().use { it.copyTo(out) } }
        context.contentResolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
    }

    private fun launchAndScreenshot() {
        context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        Thread.sleep(2500)
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

    private fun FactWire.local() = FactSnapshot(factId, text, confidence, evidenceSupported, selected && evidenceSupported, gson.toJson(sources))

    private fun DestinationWire.local(selected: Boolean = this.selected) =
        DestinationSnapshot(alias, label, provider, status, selected)

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
