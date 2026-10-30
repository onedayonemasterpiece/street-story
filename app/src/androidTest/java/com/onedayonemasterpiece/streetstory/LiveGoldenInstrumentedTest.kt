package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.ContentValues
import android.net.Uri
import android.provider.MediaStore
import android.graphics.Rect
import android.view.View
import android.view.ViewGroup
import androidx.test.core.app.ApplicationProvider
import androidx.test.core.app.ActivityScenario
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.By
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class LiveGoldenInstrumentedTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val root = File(context.filesDir, "live-golden")
    private val gson = Gson()
    private var awaitingTurnAfter = 0
    private var preparedPcmTurnCount = 0
    private var transientStoryReadRetries = 0
    private var activeStage = "setup"
    private var stageStartedAt = 0L
    private var stageDeadline = Long.MAX_VALUE
    private var routeStartedAt = 0L
    private var observedLive: LiveSessionController? = null
    private var headlessIdentityActive = false
    private var unexpectedHeadlessLive = false
    private var headlessStoryId: String? = null
    private var identityStoryReads = 0
    private val stageTimings = mutableListOf<Map<String, Any?>>()
    private val identityProgressSamples = mutableListOf<Map<String, Any?>>()

    private fun beginStage(name: String, budgetMs: Long) {
        if (stageStartedAt != 0L) finishStage("passed")
        activeStage = name
        stageStartedAt = System.currentTimeMillis()
        if (routeStartedAt == 0L) routeStartedAt = stageStartedAt
        stageDeadline = stageStartedAt + budgetMs
        android.util.Log.i("StreetStoryGolden", "stage_started stage=$name budget_ms=$budgetMs")
        persistWatchdog()
    }

    private fun finishStage(outcome: String) {
        if (stageStartedAt == 0L) return
        val elapsed = System.currentTimeMillis() - stageStartedAt
        stageTimings.add(mapOf("stage" to activeStage, "duration_ms" to elapsed, "outcome" to outcome))
        android.util.Log.i("StreetStoryGolden", "stage_finished stage=$activeStage duration_ms=$elapsed outcome=$outcome")
        stageStartedAt = 0L
        persistWatchdog()
    }

    private fun persistWatchdog() {
        File(root, "stage-progress.json").writeText(gson.toJson(mapOf(
            "stage" to activeStage, "stage_started_at_ms" to stageStartedAt,
            "route_started_at_ms" to routeStartedAt, "stage_timings" to stageTimings,
        )))
    }

    private fun checkStageWatchdog() {
        check(System.currentTimeMillis() < stageDeadline) {
            "Stage watchdog failed: stage=$activeStage elapsed_ms=${System.currentTimeMillis() - stageStartedAt}"
        }
        observedLive?.snapshot()?.error?.let { error("Live failed: stage=$activeStage error=$it") }
        if (headlessIdentityActive) assertHeadlessIdentity()
        observedLive?.snapshot()?.identityProgress?.let(::sampleIdentityProgress)
    }

    private fun sampleIdentityProgress(progress: IdentityProgressWire) {
        val sample = mapOf("elapsed_ms" to (System.currentTimeMillis() - routeStartedAt),
            "reviewed" to progress.imagesReviewedCount, "verified" to progress.visualComparisonVerified,
            "generation" to progress.generation)
        val last = identityProgressSamples.lastOrNull()
        if (last == null || last["reviewed"] != sample["reviewed"] || last["verified"] != sample["verified"] || last["generation"] != sample["generation"]) {
            if (last != null && last["generation"] == sample["generation"]) {
                check(progress.imagesReviewedCount >= (last["reviewed"] as Int)) { "Identity counter went backwards" }
            }
            identityProgressSamples.add(sample)
        }
    }

    private fun localVoiceSessionCount(): Int {
        val id = headlessStoryId ?: error("Headless story is not bound")
        return AppGraph.store(context).readableDatabase.rawQuery(
            "SELECT COUNT(*) FROM voice_sessions WHERE story_id=?", arrayOf(id),
        ).use { it.moveToFirst(); it.getInt(0) }
    }

    private fun assertHeadlessIdentity() {
        val snapshot = requireNotNull(observedLive).snapshot()
        check(!unexpectedHeadlessLive && !snapshot.active && !snapshot.connecting && snapshot.transport == null) {
            "Photo identity unexpectedly started Live/microphone"
        }
        check(AppGraph.store(context).activeVoiceSession() == null && localVoiceSessionCount() == 0) {
            "Photo identity unexpectedly started RecordingService"
        }
    }

    @Test
    fun androidClientToNativeTelegramGoldenPath() {
        val configFile = File(root, "config.json")
        assumeTrue("Live golden fixture is not provisioned", configFile.isFile)
        val config = JsonParser.parseString(configFile.readText()).asJsonObject
        val ownerMapSelection = config.get("owner_map_selection")?.asBoolean == true
        val baseUrl = config.requireString("backend_url")
        val safeAlias = config.requireString("safe_alias")
        val token = File(root, "token.txt").readText().trim()
        require(baseUrl.startsWith("https://") && token.isNotBlank())
        val keepPublication = InstrumentationRegistry.getArguments().getString("keepPublication") == "true"
        val identityOnly = InstrumentationRegistry.getArguments().getString("identityOnly") == "true"
        val moreOnly = InstrumentationRegistry.getArguments().getString("moreOnly") == "true"
        val resumeStoryId = InstrumentationRegistry.getArguments().getString("resumeStoryId").orEmpty().trim()
        val resumeIdentityResearch = InstrumentationRegistry.getArguments().getString("resumeIdentityResearch") == "true"
        require(resumeStoryId.isEmpty() || Regex("story_[a-zA-Z0-9]{8,64}").matches(resumeStoryId))
        require(!resumeIdentityResearch || (identityOnly && resumeStoryId.isNotBlank()))
        require(!identityOnly || !keepPublication)
        require(!moreOnly || (!identityOnly && !keepPublication))
        require(!keepPublication || safeAlias == "street_story_e2e_20260928_tg")
        require(isExplicitTestAlias(safeAlias))
        require(!ownerMapSelection || (!identityOnly && !moreOnly && resumeStoryId.isEmpty())) {
            "The owner-map fixture exercises a fresh full-social path only"
        }

        val api = ApiClient(baseUrl, token) { PhotoAssets.open(context, it) }
        val resumed = if (resumeStoryId.isBlank()) null else api.getStory(resumeStoryId)
        val savedPreview = resumed?.sourceAvailable == true
        val selectedCount = resumed?.facts?.count { it.selected }?.takeIf { it > 0 }
            ?: if (ownerMapSelection) 3 else 2

        beginStage("photo", 120_000)
        val seededOwnerUri = if (ownerMapSelection) seedOwnerOriginal(config) else null
        val photoUri = seededOwnerUri?.toString() ?: config.get("photo_uri")?.takeIf { !it.isJsonNull }?.asString.orEmpty()
        val photoUrl = config.get("photo_url")?.takeIf { !it.isJsonNull }?.asString.orEmpty()
        require(savedPreview || photoUri.startsWith("content://") || photoUrl.startsWith("https://")) { "An approved gallery URI or public SOURCE URL is required" }
        val pcmFiles = (1..8).map { File(root, "voice-$it.pcm") }
        require(identityOnly || pcmFiles.all(File::isFile))

        val appConfig = AppGraph.config(context)
        appConfig.backendUrl = baseUrl
        appConfig.deviceToken = token

        // Resume the addressed topic without substituting the default fixture photo.
        // No local original is copied: the normal UI restores the server preview.
        val rawImported = if (savedPreview) ImportedPhoto(
            requireNotNull(resumed).clientStoryId, File(root, "unavailable-original").absolutePath,
            requireNotNull(resumed.photoSha256), "image/jpeg",
            resumed.cameraPoint?.latitude, resumed.cameraPoint?.longitude,
        ) else if (photoUri.isNotBlank()) PhotoImporter.import(context, Uri.parse(photoUri)) else {
            val connection = (URL(photoUrl).openConnection() as HttpURLConnection).apply {
                connectTimeout = 15_000; readTimeout = 35_000
            }
            require(connection.responseCode in 200..299)
            connection.inputStream.use { PhotoImporter.importStream(context, it,
                connection.contentType?.substringBefore(';') ?: "image/jpeg") }
        }
        // Only the selected provider's original EXIF supplies camera GPS.
        // A fixture without GPS is excluded instead of changing the input flow.
        val imported = rawImported
        if (!savedPreview) {
            assertNotNull(imported.latitude)
            assertNotNull(imported.longitude)
            assertTrue(kotlin.math.abs(requireNotNull(imported.latitude) - config.get("latitude").asDouble) < 0.0025)
            assertTrue(kotlin.math.abs(requireNotNull(imported.longitude) - config.get("longitude").asDouble) < 0.0025)
        }

        val store = AppGraph.store(context)
        if (resumed != null) {
            require(!resumed.photoSha256.isNullOrBlank()) { "Saved upload identity missing" }
            require(resumed.clientStoryId.isNotBlank())
            val raw = rawStory(baseUrl, token, resumeStoryId)
            val publication = raw.get("publication")
            require(publication == null || publication.isJsonNull ||
                (publication.isJsonObject && publication.asJsonObject.entrySet().isEmpty())) {
                "Resume must reconcile an existing publication separately; never schedule it twice"
            }
            require(resumed.state != StoryStage.VISUAL_PROCESSING) { "Read back the existing generation before continuing" }
        }
        val boundPhoto = if (resumed == null) imported else imported.copy(
            clientStoryId = resumed.clientStoryId, sha256 = requireNotNull(resumed.photoSha256))
        val photoSha = boundPhoto.sha256 // Opaque upload identity, not a hash of image bytes.
        val local = store.createStory(boundPhoto)
        val created = if (resumed == null || resumed.sourceAvailable == false) api.createStory(local) else resumed
        if (resumed != null) {
            assertEquals("Rehydration created a different story", resumed.id, created.id)
            assertEquals(resumed.identityGeneration, created.identityGeneration)
        }
        require(created.sourceAvailable != false) { "Original upload was not restored in backend RAM" }
        require(created.id.isNotBlank())
        store.setServerIdentity(local.clientStoryId, created.id)
        val storyId = created.id
        val live = AppGraph.live(context)
        observedLive = live
        headlessStoryId = local.clientStoryId
        headlessIdentityActive = true
        val headlessObserver: (LiveUiState) -> Unit = { state ->
            if (headlessIdentityActive && (state.active || state.connecting || state.transport != null)) {
                unexpectedHeadlessLive = true
            }
        }
        live.addListener(headlessObserver)
        val evidence = linkedMapOf<String, Any?>(
            "server_story_id" to storyId,
            "client_story_id" to local.clientStoryId,
            "fixture_photo_sha256" to photoSha,
            "photo_identity_kind" to "opaque_upload_id",
            "source_original_sha256" to config.get("source_sha256")?.asString,
            "source_rehydrated_same_upload" to (resumed?.sourceAvailable == false),
            "destination_alias" to safeAlias,
            "physical_mic" to false,
            "prepared_owner_photo" to true,
            "photo_coordinates_source" to if (savedPreview) "saved_topic" else "embedded_exif",
            "owner_map_selection" to ownerMapSelection,
            "source_input_uri_or_url" to if (savedPreview) "saved:$resumeStoryId" else photoUri.ifBlank { photoUrl },
            "server_preview_restored" to savedPreview,
            "discovery_seeded" to false,
            "owner_name_hint" to false,
            "seed_urls_supplied" to false,
            "prepared_pcm_after_capture_boundary" to false,
            "legacy_voice_endpoint_used" to false,
            "acceptance" to if (moreOnly) "more" else if (identityOnly) "identity" else "full_social",
            "more_acceptance_status" to "not_run",
            "resumed_story_id" to resumeStoryId.takeIf { it.isNotEmpty() },
            "fresh_full_pass" to (resumeStoryId.isEmpty() && !moreOnly && !identityOnly),
        )
        var publicationScheduled = false
        var cancelConfirmed = false
        val screenshots = mutableListOf<Map<String, Any?>>()
        evidence["stage_ui_receipts"] = screenshots
        evidence["stage_timings"] = stageTimings
        evidence["identity_progress_samples"] = identityProgressSamples
        fun capture(stage: String, story: StoryWire) {
            captureStage(stage, story, local.clientStoryId, store, screenshots)
        }

        try {
            beginStage("identity", 5L * 60 * 1000)
            var story: StoryWire
            if (ownerMapSelection) {
                require(resumed == null && !resumeIdentityResearch) { "Owner-map canary is a fresh upload, not an unknown-operation retry" }
                assertEquals("manual", created.identityMode)
                capture("01-photo-before-research", created)
                selectOwnerContour(api, created, local.clientStoryId, config.requireString("owner_selected_candidate_id"))
                story = pollStory(api, storyId) {
                    it.visualIdentity?.status == "owner_confirmed"
                }
                assertEquals(config.requireString("owner_selected_candidate_id"), story.visualIdentity?.candidateId)
                val selection = rawStory(baseUrl, token, storyId).requireObject("place_selection")
                assertEquals("exact", selection.requireString("mode"))
                assertEquals(photoSha, selection.requireString("photo_sha256"))
                assertEquals(story.identityGeneration, selection.get("generation").asInt)
                evidence["automatic_identity"] = false
                evidence["owner_selection"] = selection
                evidence["identity_scope"] = mapOf("photo_sha256" to photoSha, "identity_generation" to story.identityGeneration)
                evidence["one_physical_contour_tap"] = true
                evidence["owner_selection_reopened"] = true
            } else if (resumed?.visualIdentity?.status == "owner_confirmed") {
                story = resumed
                assertEquals(photoSha, story.photoSha256)
                evidence["automatic_identity"] = false
                evidence["owner_selection_history_reused"] = true
                capture("01-saved-photo-before-live", story)
            } else {
            if (resumeIdentityResearch) {
                val before = rawStory(baseUrl, token, storyId)
                assertEquals(photoSha, before.requireString("photo_sha256"))
                val generation = before.get("identity_generation").asInt
                val revision = before.get("research_control_revision").asInt
                val control = before.requireObject("research_controls").requireObject("identity")
                require(control.get("stopped").asBoolean) { "Explicit Resume requires a stopped saved scope" }
                assertEquals(photoSha, control.requireString("photo_sha256"))
                assertEquals(generation, control.get("identity_generation").asInt)
                val payload = gson.toJson(mapOf(
                    "action" to "resume", "purpose" to "identity",
                    "expected_photo_sha256" to photoSha,
                    "expected_identity_generation" to generation,
                    "expected_control_revision" to revision,
                ))
                // The same addressed request survives a lost response; no new job or generation is created.
                val requestKey = "golden-resume-identity-$storyId-$generation-$revision"
                File(root, "identity-resume-intent.json").writeText(gson.toJson(mapOf(
                    "story_id" to storyId, "request_key" to requestKey,
                    "photo_sha256" to photoSha, "identity_generation" to generation,
                    "research_control_revision" to revision,
                )))
                val after = api.mutate(storyId, "research-control", payload, requestKey)
                assertEquals(photoSha, after.photoSha256)
                assertEquals(generation, after.identityGeneration)
                require(after.researchControls["identity"]?.stopped == false)
                evidence["identity_explicit_resume"] = mapOf(
                    "request_key" to requestKey, "control_revision_before" to revision,
                    "photo_sha256" to photoSha, "identity_generation" to generation,
                )
            }
            assertHeadlessIdentity()
            capture("01-photo-before-research", api.getStory(storyId))
            story = pollStory(api, storyId, stageDeadline - System.currentTimeMillis(),
                allowedNeedsReviewCodes = setOf("visual_identity_uncertain", "visual_stale")) {
                it.visualIdentity?.status == "match"
            }
            assertEquals("Photo must be identified automatically without Live", "match", story.visualIdentity?.status)
            val rawIdentityStory = rawStory(baseUrl, token, storyId)
            val identity = rawIdentityStory.requireObject("visual_identity")
            assertEquals(photoSha, rawIdentityStory.requireString("photo_sha256"))
            assertEquals(photoSha, identity.requireString("photo_sha256"))
            assertEquals(story.visualIdentity?.candidateId, identity.requireString("candidate_id"))
            assertEquals(rawIdentityStory.get("identity_generation").asInt, identity.get("generation").asInt)
            assertTrue("Match lacks actual visual reference proof", identity.get("visual_reference_verified")?.asBoolean == true)
            val references = identity.getAsJsonArray("reference_evidence")?.map { it.asJsonObject }.orEmpty()
            assertTrue("Match has no reference image URL evidence", references.isNotEmpty())
            val modelImageUrls = references.map { reference ->
                val imageUrl = reference.get("image_url")?.takeIf { !it.isJsonNull }?.asString
                    ?: reference.requireString("model_image_url")
                check(imageUrl.startsWith("https://")) { "Reference public image URL missing" }
                check(reference.requireString("source_url").startsWith("https://")) { "Reference source provenance missing" }
                imageUrl
            }.distinct()
            val sourceUrls = (references.map { it.requireString("source_url") } +
                identity.getAsJsonArray("source_links")?.mapNotNull { item ->
                    item.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.startsWith("https://") }
                }.orEmpty()).distinct()
            assertTrue("Visual progress lacks verified reference readback", story.identityProgress?.visualComparisonVerified == true)
            val identityProof = JsonObject().apply {
                for (key in listOf("status", "candidate_id", "candidate_name", "confidence", "policy", "photo_sha256",
                    "generation", "visual_reference_verified", "comparison_model", "comparison_id", "reference_subject_binding")) {
                    identity.get(key)?.let { add(key, it.deepCopy()) }
                }
                add("reference_evidence", identity.get("reference_evidence").deepCopy())
            }
            val providerReceipt = JsonObject().apply {
                identity.get("provider_receipt")?.takeIf { it.isJsonObject }?.asJsonObject?.let { receipt ->
                    for (key in listOf("provider", "model", "model_id", "operation_id", "request_id", "status", "protocol",
                        "http_status", "input_tokens", "output_tokens", "total_tokens")) {
                        receipt.get(key)?.takeIf { it.isJsonPrimitive }?.let { add(key, it.deepCopy()) }
                    }
                }
            }
            evidence.putAll(mapOf(
                "automatic_identity" to true,
                "identity_without_live" to true,
                "identity_transport" to "headless_https_poll",
                "identity_scope" to mapOf("photo_sha256" to photoSha, "identity_generation" to story.identityGeneration),
                "visual_identity" to identityProof,
                "identity_reference_count" to references.size,
                "identity_model_image_count" to modelImageUrls.size,
                "identity_source_url_count" to sourceUrls.size,
                "identity_source_urls" to sourceUrls,
                "identity_progress" to story.identityProgress,
                "identity_candidate_count" to story.visualIdentity?.candidates?.size,
                "identity_article_candidate_count" to identity.getAsJsonArray("candidates")?.count {
                    it.asJsonObject.get("discovery")?.takeIf { value -> value.isJsonPrimitive }?.asString == "web_article_media"
                },
                "identity_provider_receipt" to providerReceipt,
                "identity_http_read_count" to identityStoryReads,
                "backend_live_message_count" to story.liveMessages.size,
                "backend_voice_message_count" to story.voiceMessages.size,
            ))
            }
            if (resumed == null) {
                assertTrue("Identity used owner voice or Live messages", story.liveMessages.isEmpty() && story.voiceMessages.isEmpty())
            } else {
                assertEquals("Resume started Live before opening the saved topic", resumed.liveMessages.size, story.liveMessages.size)
                assertEquals("Resume recorded new voice during identity readback", resumed.voiceMessages.size, story.voiceMessages.size)
                evidence["identity_history_reused"] = true
            }
            capture("02-object-identified", story)
            assertHeadlessIdentity()
            evidence["no_live_or_recording_started"] = true
            evidence["local_voice_session_count"] = localVoiceSessionCount()
            if (identityOnly) {
                evidence["identity_only"] = true
                finishStage("passed")
                return
            }

            // Full social acceptance explicitly starts the existing voice path
            // only AFTER the independent server identification has succeeded.
            headlessIdentityActive = false
            beginStage("live", 120_000)
            val ready = CountDownLatch(1)
            var liveError: String? = null
            live.start(local.clientStoryId) { ok, error ->
                if (!ok) liveError = error ?: "Live start failed"
                ready.countDown()
            }
            assertTrue("Live start timed out", ready.await(45, TimeUnit.SECONDS))
            check(liveError == null) { liveError.orEmpty() }
            assertTrue(live.isActiveFor(local.clientStoryId))
            assertEquals("wss", live.snapshot().transport)
            evidence["transport"] = live.transportEvidence()

            beginStage("research", 5L * 60 * 1000)
            story = readStory(api, storyId)
            // The headless path has already confirmed this photo. POI memory
            // and its eligible facts are ordinary product results: a first post
            // must not require another identification or discovery turn.
            val savedFactsReady = story.sourceCount > 0 && story.facts.count { it.eligibleForSelection } >= selectedCount
            evidence["initial_fact_inventory_reused"] = savedFactsReady
            evidence["initial_eligible_fact_count"] = story.facts.count { it.eligibleForSelection }
            if (!savedFactsReady && story.researchPending["facts"] != true) {
                check(story.researchOutcome?.outcome != "outcome_unknown") { "Observe the original unknown research operation; do not request another" }
                speak(live, pcmFiles[2])
                awaitAnswer(live, "research request")
            }
            story = pollWithOwnerClarification(
                api, storyId, live, evidence, "fact research",
                "Продолжи поиск фактов о подтверждённом объекте на фотографии. Прочитай текущее состояние темы, используй именно его подтверждённый POI ID и сохрани проверенные факты с источниками. Не выбирай факты и не готовь текст публикации.",
                allowedNeedsReviewCodes = setOf("visual_stale"),
            ) {
                it.sourceCount > 0 && it.facts.count { fact -> fact.eligibleForSelection } >= selectedCount
            }
            assertTrue(story.sourceCount > 0)
            assertTrue(story.sources.all { !it.title.isNullOrBlank() && it.url.startsWith("https://") })
            require(story.facts.count { it.evidenceSupported } >= selectedCount)
            capture("03-facts-after-research", story)

            beginStage("selection", 4L * 60 * 1000)
            val savedSelectionReady = resumed != null && story.facts.count { it.selected && it.eligibleForSelection } == selectedCount
            evidence["selection_history_reused"] = savedSelectionReady
            if (!savedSelectionReady) {
                speak(live, pcmFiles[3])
                awaitAnswer(live, "fact selection")
                story = pollWithOwnerClarification(api, storyId, live, evidence, "fact selection",
                    "Выбери для поста ровно $selectedCount самых надёжных подтверждённых факта из текущего списка и сохрани этот выбор. Сейчас заверши именно выбор фактов; платформу публикации я укажу позже.") {
                    it.facts.count { fact -> fact.selected && fact.evidenceSupported } == selectedCount
                }
            }
            capture("04-facts-selected", story)
            val selectedFactIds = story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }

            beginStage("concept", 3L * 60 * 1000)
            val savedConceptReady = resumed != null && !story.publicationConcept.isNullOrBlank()
            evidence["concept_history_reused"] = savedConceptReady
            if (!savedConceptReady) {
                ownerText(live, "Концепция поста: история здания на фотографии через $selectedCount выбранных подтверждённых факта. Сохрани эту концепцию и текущий выбор.", "publication concept")
                story = pollStory(api, storyId, allowedNeedsReviewCodes = setOf("visual_stale")) {
                    !it.publicationConcept.isNullOrBlank()
                }
            }
            assertEquals(selectedFactIds, story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId })
            capture("05-publication-concept", story)
            evidence["publication_concept"] = story.publicationConcept

            beginStage("draft", 4L * 60 * 1000)
            val savedDraftReady = resumed != null && savedSelectionReady && savedConceptReady && !story.draftText.isNullOrBlank()
            evidence["draft_history_reused"] = savedDraftReady
            if (story.draftText.isNullOrBlank()) {
                live.sendText(
                    "По уже выбранным подтверждённым фактам собери первый короткий городской пост. " +
                        "Сохрани текст; не ищи новые факты и не меняй выбранные источники.",
                )
                awaitAnswer(live, "initial draft")
                story = pollStory(api, storyId, allowedNeedsReviewCodes = setOf("visual_stale")) {
                    !it.draftText.isNullOrBlank()
                }
            }
            if (!savedDraftReady) {
                val beforeEdit = requireNotNull(story.draftText)
                speak(live, pcmFiles[4])
                awaitAnswer(live, "text edit")
                story = pollWithOwnerClarification(api, storyId, live, evidence, "text edit",
                    "Уточняю правку: сделай текущий текст короче и живее, без канцелярита. Используй только $selectedCount уже выбранных факта; выбор и изображение не меняй.") {
                    !it.draftText.isNullOrBlank() && it.draftText != beforeEdit
                }
            }

            capture("05-publication-text", story)

            if (moreOnly) {
                evidence["more_acceptance_status"] = "running"
                beginStage("more", 6L * 60 * 1000)
                val textBeforeMore = requireNotNull(story.draftText)
                val conceptBeforeMore = story.publicationConcept
                val factsBeforeMore = story.facts.associate { it.factId to it.sources.map { source -> source.url }.toSet() }
                val revisionsBeforeMore = story.facts.associate { it.factId to it.revisionDigest }
                val evidenceBeforeMore = story.facts.associate { it.factId to it.supportingEvidenceKeys.toSet() }
                val moreStarted = System.currentTimeMillis()
                ownerText(live,
                    "Найди ещё полезные проверяемые факты о подтверждённом объекте. Прочитай полную накопленную историю фактов и источников, " +
                        "продолжи незавершённые статьи и используй уже найденные материалы прежде нового поиска. " +
                        "Сохрани новые подтверждённые факты в общей памяти POI и в этой истории. " +
                        "Два выбранных факта, концепцию и текущий текст публикации оставь без изменений.", "more facts")
                story = pollWithOwnerClarification(api, storyId, live, evidence, "more facts",
                    "Продолжи именно дополнительное исследование. Сохрани новые факты с evidence через продуктовые инструменты; " +
                        "готовность в речи без сохранённых фактов недостаточна. Выбор, концепцию и текст публикации сохрани.",
                    allowedNeedsReviewCodes = setOf("visual_stale")) { candidate ->
                    candidate.facts.any { fact -> fact.eligibleForSelection &&
                        (fact.factId !in factsBeforeMore || fact.sources.any { it.url !in factsBeforeMore.getValue(fact.factId) } ||
                            fact.supportingEvidenceKeys.any { it !in evidenceBeforeMore.getValue(fact.factId) }) }
                }
                val addedFacts = story.facts.filter { it.eligibleForSelection && it.factId !in factsBeforeMore }.map { it.factId }
                val improvedFacts = story.facts.filter { fact -> fact.eligibleForSelection && fact.factId in factsBeforeMore &&
                    (fact.sources.any { it.url !in factsBeforeMore.getValue(fact.factId) } ||
                        fact.supportingEvidenceKeys.any { it !in evidenceBeforeMore.getValue(fact.factId) }) }.map { it.factId }
                assertTrue("More returned no new supported fact or evidence", addedFacts.isNotEmpty() || improvedFacts.isNotEmpty())
                assertEquals("More changed selected facts", selectedFactIds, story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId })
                assertEquals("More rewrote publication text", textBeforeMore, story.draftText)
                assertEquals("More changed publication concept", conceptBeforeMore, story.publicationConcept)
                capture("05-more-facts-preserved-draft", story)
                evidence["more_added_fact_ids"] = addedFacts
                evidence["more_improved_fact_ids"] = improvedFacts
                evidence["more_duration_ms"] = System.currentTimeMillis() - moreStarted
                evidence["more_fact_count"] = story.facts.size
                evidence["more_selection_and_draft_preserved"] = true
                evidence["more_revisions_before"] = revisionsBeforeMore
                evidence["more_revisions_after"] = story.facts.associate { it.factId to it.revisionDigest }
                evidence["more_evidence_before"] = evidenceBeforeMore
                evidence["more_evidence_after"] = story.facts.associate { it.factId to it.supportingEvidenceKeys.toSet() }
                evidence["more_acceptance_status"] = "passed"
                finishStage("passed")
                return
            }

            // Full-social acceptance covers the owner MVP path only. Literal mode,
            // protected-span editing and undo have dedicated tests and must not
            // consume real-provider budget before image/publication acceptance.
            beginStage("visual", 8L * 60 * 1000)
            val textBeforeVisual = requireNotNull(story.draftText)
            val visualBefore = rawStory(baseUrl, token, storyId).getAsJsonObject("visual")
            val previousImageOperation = visualBefore?.get("operation_id")?.takeIf { !it.isJsonNull }?.asString
            val freshFullPass = resumed == null && !identityOnly && !moreOnly
            if (freshFullPass) {
                require(previousImageOperation.isNullOrBlank() && story.processedImageUrl.isNullOrBlank() &&
                    story.state != StoryStage.VISUAL_PROCESSING) {
                    "Fresh visual stage already has an operation; observe that operation before any new request"
                }
            }
            val savedVisualReady = resumed != null && savedDraftReady && story.state == StoryStage.READY_TO_PUBLISH && !story.processedImageUrl.isNullOrBlank()
            evidence["visual_history_reused"] = savedVisualReady
            if (!savedVisualReady) {
                speak(live, pcmFiles[5])
                awaitAnswer(live, "visual-only edit")
            }
            val visualStarted = api.getStory(storyId)
            val observedVisual = rawStory(baseUrl, token, storyId).getAsJsonObject("visual")
            val observedVisualOperation = observedVisual?.get("operation_id")?.takeIf { !it.isJsonNull }?.asString
            if (observedVisualOperation.isNullOrBlank() && visualStarted.state != StoryStage.VISUAL_PROCESSING && visualStarted.processedImageUrl.isNullOrBlank()) {
                evidence["visual_text_clarification"] = true
                ownerText(live, "Создай инфографичную картинку по исходной фотографии, сохранённой концепции и $selectedCount выбранным фактам. Сделай её чуть теплее. Текущий текст поста оставь без изменений.", "visual clarification")
            }
            story = pollStory(
                api,
                storyId,
                VISUAL_TIMEOUT_MS,
                allowedNeedsReviewCodes = setOf("visual_stale"),
            ) {
                it.state == StoryStage.READY_TO_PUBLISH && !it.processedImageUrl.isNullOrBlank()
            }
            assertEquals("Visual-only change rewrote text", textBeforeVisual, story.draftText)
            capture("06-generated-visual", story)
            val rawReady = rawStory(baseUrl, token, storyId)
            val visual = rawReady.requireObject("visual")
            assertEquals(OWNER_PROMPT_SHA256, visual.requireString("prompt_sha256"))
            val imageOperation = visual.requireString("operation_id")
            val imageAsset = visual.requireString("selected_asset_ref")
            if (freshFullPass) {
                require(imageOperation != previousImageOperation)
                evidence["fresh_visual_operation"] = true
            }
            assertEquals("Visual changed the selected facts", selectedFactIds.toSet(),
                story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }.toSet())

            val imageUrl = requireNotNull(story.processedImageUrl)
            val processed = PhotoAssets.retainTemporary(api.readAsset(imageUrl))
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
            store.setProcessedImagePath(local.clientStoryId, processed)
            context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE)
                .edit().putString("active_story_id", local.clientStoryId).apply()
            beginStage("publication", 8L * 60 * 1000)
            val publicDestinations = api.capabilities().destinations
            assertEquals(
                "Owner MVP must expose exactly the configured test Telegram group",
                listOf(safeAlias),
                publicDestinations.map { it.alias },
            )
            assertTrue(publicDestinations.single().status in setOf("supported", "needs_review"))

            val scheduledAt = OffsetDateTime.now(ZoneId.of("Europe/Kaliningrad"))
                .let { now ->
                    if (keepPublication) now.plusMinutes(4L)
                    else now.plusMinutes(1500L).withSecond(0)
                }
                .withNano(0)
            val preparationTurnAfter = live.snapshot().completedTurns
            val previousConfirmationId = live.snapshot().confirmation?.confirmationId
            val preparationStarted = OffsetDateTime.now(ZoneId.of("Europe/Kaliningrad"))
            if (keepPublication) speak(live, pcmFiles[6]) else live.sendText(
                "Подготовь карточку публикации именно текущих текста и уже просмотренной картинки. " +
                    "Отправляем только в тестовую Telegram-группу street-story e2e (назначение $safeAlias) " +
                    "на ${scheduledAt.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)}, " +
                    "часовой пояс Europe/Kaliningrad. Покажи карточку и жди отдельного подтверждения; " +
                    "пока ничего не публикуй."
            )
            waitUntil(150_000, "publication confirmation was not prepared") {
                val current = live.snapshot().confirmation
                current != null && current.confirmationId != previousConfirmationId
            }
            // The provider can finish its tool-call turn before the card is
            // prepared. The card and tool result change the existing UI status;
            // wait for the response turn to finish before the separate consent.
            waitUntil(120_000, "publication preparation response did not complete") {
                val current = live.snapshot()
                check(current.error == null) { "Live failed during publication preparation: ${current.error}" }
                current.active && current.completedTurns > preparationTurnAfter && current.status == "Слушаю"
            }
            awaitingTurnAfter = live.snapshot().completedTurns
            capture("07-publication-confirmation", api.getStory(storyId))
            val confirmation = requireNotNull(live.snapshot().confirmation)
            assertEquals(story.draftText, confirmation.text)
            assertEquals(listOf(safeAlias), confirmation.destinations)
            assertEquals("Publication card changed the reviewed image", story.processedImageUrl, confirmation.imageUrl)
            val cardTime = OffsetDateTime.parse(requireNotNull(confirmation.scheduledFor)).toInstant()
            if (keepPublication) {
                // Spoken request: five minutes from now. Review before giving consent.
                assertTrue("Voice card time is not within the requested near-term window",
                    cardTime.isAfter(preparationStarted.plusMinutes(3).toInstant()) &&
                        cardTime.isBefore(OffsetDateTime.now().plusMinutes(7).toInstant()))
            } else assertEquals("Publication card changed the requested time", scheduledAt.toInstant(), cardTime)
            assertEquals("Europe/Kaliningrad", confirmation.timezone)
            evidence["confirmation_id"] = confirmation.confirmationId
            evidence["separate_confirmation_input"] = true
            evidence["exact_card_text_image_destination_time_review"] = true

            evidence["publication_input_mode"] = if (keepPublication) "prepared_pcm" else "text"
            if (keepPublication) speak(live, pcmFiles[7])
            else live.sendText("Подтверждаю именно показанную карточку публикации.")
            awaitAnswer(live, "publication confirmation")
            val scheduled = pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                rawStory(baseUrl, token, storyId)
                    .get("publication")?.takeIf { it.isJsonObject }?.asJsonObject?.get("state")?.asString in setOf("scheduled", "verified")
            }
            capture("08-test-publication-scheduled", scheduled)
            val rawScheduled = rawStory(baseUrl, token, storyId)
            val publication = rawScheduled.requireObject("publication")
            val publicationId = publication.requireString("publication_id")
            publicationScheduled = true

            val cancelled = if (keepPublication) null else {
                live.sendText("Отмени текущую запланированную публикацию.")
                awaitAnswer(live, "publication cancel")
                pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                    rawStory(baseUrl, token, storyId)
                        .get("publication")?.takeIf { it.isJsonObject }?.asJsonObject?.get("state")?.asString == "cancelled"
                }
                val result = rawStory(baseUrl, token, storyId).requireObject("publication")
                assertEquals("cancelled", result.requireString("state"))
                cancelConfirmed = true
                result
            }

            evidence.putAll(
                mapOf(
                    "schema_version" to 3,
                    "client_story_id" to local.clientStoryId,
                    "server_story_id" to storyId,
                    "fixture_photo_sha256" to photoSha,
            "photo_identity_kind" to "opaque_upload_id",
            "source_rehydrated_same_upload" to (resumed?.sourceAvailable == false),
                    "live_provider" to "gemini-3.8-live",
                    "prepared_pcm_after_capture_boundary" to (preparedPcmTurnCount > 0),
                    "physical_mic" to false,
                    "legacy_voice_endpoint_used" to false,
                    "research_revision" to scheduled.researchRevision,
                    "visual_identity_status" to scheduled.visualIdentity?.status,
                    "source_count" to scheduled.sourceCount,
                    "selected_fact_ids" to selectedFactIds,
                    "visual_only_text_preserved" to true,
                    "prompt_sha256" to OWNER_PROMPT_SHA256,
                    "image_operation_id" to imageOperation,
                    "image_asset_ref" to imageAsset,
                    "image_url" to imageUrl,
                    "image_identity_kind" to "operation_asset_url",
                    "publication_id" to publicationId,
                    "scheduled_for" to scheduled.scheduledFor,
                    "destination_alias" to safeAlias,
                    "publication_kept" to keepPublication,
                    "cancel_confirmed" to cancelConfirmed,
                    "cancel_operation_id" to cancelled?.get("cancel_operation_id")?.asString,
                )
            )
            finishStage("passed")
        } finally {
            finishStage("failed")
            if (evidence["more_acceptance_status"] == "running") evidence["more_acceptance_status"] = "failed"
            evidence["last_stage"] = activeStage
            evidence["route_duration_ms"] = System.currentTimeMillis() - routeStartedAt
            if (publicationScheduled && !cancelConfirmed && !keepPublication) {
                runCatching {
                    if (live.isActiveFor(local.clientStoryId)) {
                        live.sendText("Аварийная очистка теста: отмени текущую запланированную публикацию.")
                        waitUntil(90_000, "Live cleanup cancel failed") {
                            rawStory(baseUrl, token, storyId)
                                .get("publication")?.takeIf { it.isJsonObject }?.asJsonObject?.get("state")?.asString == "cancelled"
                        }
                        evidence["best_effort_live_cancel_confirmed"] = true
                    } else {
                        api.mutate(storyId, "cancel", "{}", newRequestKey("emergency-cleanup", local.clientStoryId))
                        pollStory(api, storyId, SOCIAL_TIMEOUT_MS) {
                            rawStory(baseUrl, token, storyId)
                                .get("publication")?.takeIf { it.isJsonObject }?.asJsonObject?.get("state")?.asString == "cancelled"
                        }
                        evidence["best_effort_legacy_cleanup_only"] = true
                    }
                }
            }
            runCatching { capture("99-final-state", readStory(api, storyId)) }
            evidence["transient_story_read_retries"] = transientStoryReadRetries
            evidence["last_live_error"] = live.snapshot().error
            evidence["completed_live_turns"] = live.snapshot().completedTurns
            evidence["prepared_pcm_turn_count"] = preparedPcmTurnCount
            evidence["prepared_pcm_after_capture_boundary"] = preparedPcmTurnCount > 0
            evidence["transport_final"] = live.transportEvidence()
            if (identityOnly) {
                evidence["record_audio_permission_granted"] = context.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == android.content.pm.PackageManager.PERMISSION_GRANTED
                val noLive = runCatching { assertHeadlessIdentity() }
                evidence["no_live_or_recording_started"] = noLive.isSuccess
                evidence["local_voice_session_count"] = runCatching { localVoiceSessionCount() }.getOrNull()
                noLive.exceptionOrNull()?.let { evidence["headless_identity_error"] = it.message }
            } else {
                live.stopLocal(sendRemote = true)
            }
            live.removeListener(headlessObserver)
            File(root, "evidence.json").writeText(gson.toJson(evidence))
            seededOwnerUri?.let { context.contentResolver.delete(it, null, null) }
            store.close()
            if (identityOnly) check(evidence["no_live_or_recording_started"] == true) {
                "Headless identity started Live or recording; retained evidence contains the failure"
            }
        }
        assertTrue(if (keepPublication) publicationScheduled else cancelConfirmed)
    }

    private fun seedOwnerOriginal(config: JsonObject): Uri {
        val bytes = File(root, "source.original").readBytes()
        val digest = java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
        assertEquals("CI must use the unchanged owner original", config.requireString("source_sha256"), digest)
        val resolver = context.contentResolver
        val uri = requireNotNull(resolver.insert(
            MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
            ContentValues().apply {
                put(MediaStore.Images.Media.DISPLAY_NAME, "street-story-owner104-live.jpg")
                put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                put(MediaStore.Images.Media.IS_PENDING, 1)
            },
        ))
        try {
            requireNotNull(resolver.openOutputStream(uri)).use { it.write(bytes) }
            resolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
            return uri
        } catch (failure: Throwable) {
            resolver.delete(uri, null, null)
            throw failure
        }
    }

    /** Evaluator chooses a visible contour; only normal native touch and outbox commit it. */
    private fun selectOwnerContour(api: ApiClient, initial: StoryWire, localId: String, candidateId: String) {
        val data = api.placeMap(initial.id)
        initial.placeMap = data
        ResearchProjectionStore(context).replace(localId, initial)
        val feature = requireNotNull(org.maplibre.geojson.FeatureCollection.fromJson(requireNotNull(data.buildings).toString())
            .features()?.firstOrNull { it.id() == candidateId || (it.hasProperty("candidate_id") && it.getStringProperty("candidate_id") == candidateId) }) {
            "Owner's intended contour is absent from the actual server map"
        }
        fun positions(value: com.google.gson.JsonElement): List<Pair<Double, Double>> {
            if (!value.isJsonArray) return emptyList()
            val array = value.asJsonArray
            return if (array.size() >= 2 && array[0].isJsonPrimitive && array[1].isJsonPrimitive)
                listOf(array[0].asDouble to array[1].asDouble)
            else array.flatMap(::positions)
        }
        val coords = positions(JsonParser.parseString(requireNotNull(feature.geometry()).toJson())
            .asJsonObject.get("coordinates"))
        require(coords.isNotEmpty())
        val west = coords.minOf { it.first }; val east = coords.maxOf { it.first }
        val south = coords.minOf { it.second }; val north = coords.maxOf { it.second }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val device = UiDevice.getInstance(instrumentation)
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            var nativeMap: org.maplibre.android.maps.MapLibreMap? = null
            var view: org.maplibre.android.maps.MapView? = null
            var centered = false
            var target: android.graphics.Point? = null
            var targetLocation: org.maplibre.android.geometry.LatLng? = null
            var previousBounds: Rect? = null
            fun descendants(node: View): Sequence<View> = sequence {
                yield(node)
                if (node is ViewGroup) for (index in 0 until node.childCount) yieldAll(descendants(node.getChildAt(index)))
            }
            waitUntil(60_000, "Actual owner contour did not become touchable") {
                scenario.onActivity { activity ->
                    view = descendants(activity.findViewById(android.R.id.content))
                        .filterIsInstance<org.maplibre.android.maps.MapView>().singleOrNull()
                    view?.getMapAsync { nativeMap = it }
                    val map = nativeMap
                    val visible = view
                    if (map?.style != null && visible != null) {
                        visible.requestRectangleOnScreen(Rect(0, 0, visible.width, visible.height), true)
                        if (!centered) {
                            map.moveCamera(org.maplibre.android.camera.CameraUpdateFactory.newLatLngZoom(
                                org.maplibre.android.geometry.LatLng((south + north) / 2, (west + east) / 2), 18.0))
                            centered = true
                        } else if (visible.isAttachedToWindow && visible.hasWindowFocus() && !visible.isLayoutRequested) {
                            val origin = IntArray(2); visible.getLocationOnScreen(origin)
                            val bounds = Rect(); visible.getGlobalVisibleRect(bounds)
                            // As in the existing native-map test: queryable tiles
                            // can precede removal of the SDK's loading overlay.
                            val stable = visible.foreground == null && previousBounds == bounds
                            previousBounds = Rect(bounds)
                            // Locate an interior rendered point even for a ring/courtyard.
                            // This is read-only hit testing, never a click callback or selection mutation.
                            for (row in 1..9) for (column in 1..9) if (target == null) {
                                val location = org.maplibre.android.geometry.LatLng(
                                    south + (north - south) * row / 10, west + (east - west) * column / 10)
                                val point = map.projection.toScreenLocation(location)
                                val x = origin[0] + point.x.toInt(); val y = origin[1] + point.y.toInt()
                                if (stable && bounds.contains(x, y) && map.queryRenderedFeatures(point, "owner-buildings-fill")
                                    .any { it.id() == candidateId || (it.hasProperty("candidate_id") && it.getStringProperty("candidate_id") == candidateId) }) {
                                    target = android.graphics.Point(x, y)
                                    targetLocation = location
                                }
                            }
                        }
                    }
                }
                target != null
            }
            try {
                EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-map")
                scenario.onActivity {
                    // Window readiness can change the scroll/layout. Recompute
                    // the same geographic hit immediately before the one tap.
                    val map = requireNotNull(nativeMap)
                    val visible = requireNotNull(view)
                    val point = map.projection.toScreenLocation(requireNotNull(targetLocation))
                    val origin = IntArray(2); visible.getLocationOnScreen(origin)
                    val bounds = Rect(); assertTrue(visible.getGlobalVisibleRect(bounds))
                    assertNull("Map loading overlay still intercepts touch", visible.foreground)
                    assertTrue("Current physical tap must hit the expected contour", map.queryRenderedFeatures(point, "owner-buildings-fill")
                        .any { it.id() == candidateId || (it.hasProperty("candidate_id") && it.getStringProperty("candidate_id") == candidateId) })
                    target = android.graphics.Point(origin[0] + point.x.toInt(), origin[1] + point.y.toInt())
                    assertTrue("Physical tap must lie in current visible map", bounds.contains(target!!.x, target!!.y))
                }
                val point = requireNotNull(target)
                assertTrue("Inject exactly one physical contour tap", device.click(point.x, point.y))
                waitUntil(5000, "Physical contour tap did not enable confirmation") {
                    var enabled = false
                    scenario.onActivity { activity ->
                        // UiAutomator omits off-screen controls. Read the real
                        // button state before scrolling it into view to tap it.
                        enabled = descendants(activity.findViewById(android.R.id.content))
                            .single { it.contentDescription?.toString() == "owner-place-confirm" }.isEnabled
                    }
                    enabled
                }
            } finally {
                // Diagnostic UI is separate from SOURCE/provider media. Keep
                // the failed screen, actual button state and native touch logs.
                runCatching {
                    val state = linkedMapOf<String, Any?>("candidate_id" to candidateId,
                        "target_x" to target?.x, "target_y" to target?.y)
                    scenario.onActivity { activity ->
                        val controls = descendants(activity.findViewById(android.R.id.content)).toList()
                        val button = controls.single { it.contentDescription?.toString() == "owner-place-confirm" }
                        val bounds = Rect()
                        state["confirmation_enabled"] = button.isEnabled
                        state["confirmation_visible"] = button.getGlobalVisibleRect(bounds)
                        state["confirmation_bounds"] = bounds.toShortString()
                        state["map_foreground"] = view?.foreground?.javaClass?.simpleName
                        state["map_status"] = (controls.singleOrNull { it.contentDescription?.toString() == "owner-place-status" }
                            as? android.widget.TextView)?.text?.toString()
                    }
                    File(root, "owner-map-tap.json").writeText(gson.toJson(state))
                    device.dumpWindowHierarchy(File(root, "owner-map-tap.xml"))
                    device.takeScreenshot(File(root, "owner-map-tap.png"))
                }
            }
            scenario.onActivity { activity ->
                descendants(activity.findViewById(android.R.id.content))
                    .single { it.contentDescription?.toString() == "owner-place-confirm" }
                    .let { it.requestRectangleOnScreen(Rect(0, 0, it.width, it.height), true) }
            }
            val confirm = requireNotNull(device.wait(androidx.test.uiautomator.Until.findObject(By.desc("owner-place-confirm")), 5000))
            assertTrue("Scrolled physical confirmation must remain enabled", confirm.isEnabled)
            confirm.click()
            pollStory(api, initial.id) { it.visualIdentity?.candidateId == candidateId && it.visualIdentity?.status == "owner_confirmed" }
            waitUntil(5000, "Normal outbox acknowledgement did not reach local projection") {
                ResearchProjectionStore(context).get(localId)?.candidateId == candidateId
            }
            scenario.recreate()
            EmulatorWindowGuard.requireStoryWindow(context, device, "owner-place-map")
            assertEquals("Reopen lost the acknowledged owner selection", candidateId,
                ResearchProjectionStore(context).get(localId)?.candidateId)
        }
    }

    private fun captureStage(
        stage: String,
        story: StoryWire,
        clientStoryId: String,
        store: StoryStore,
        screenshots: MutableList<Map<String, Any?>>,
    ) {
        // Render the actual production response through the normal durable UI projection.
        store.setServerSnapshot(clientStoryId, story.state, story.placeName, story.summary, story.draftText,
            story.processedImageUrl, story.scheduledFor, story.publishedAt, story.error?.message, story.revision)
        store.replaceFacts(clientStoryId, story.facts.map { it.local() })
        ResearchProjectionStore(context).replace(clientStoryId, story)
        context.getSharedPreferences("street_story_topics_v1", Context.MODE_PRIVATE).edit()
            .putString("active_story_id", clientStoryId).commit()
        if (story.state == StoryStage.READY_TO_PUBLISH && !story.processedImageUrl.isNullOrBlank()) {
            val config = AppGraph.config(context)
            val current = store.story(clientStoryId)?.processedImagePath
            if (current == null || !PhotoAssets.available(current)) {
                val bytes = ApiClient(requireNotNull(config.backendUrl), requireNotNull(config.deviceToken)).readAsset(requireNotNull(story.processedImageUrl))
                store.setProcessedImagePath(clientStoryId, PhotoAssets.retainTemporary(bytes))
            }
        }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val device = UiDevice.getInstance(instrumentation)
        val detailDescription = when {
            stage.contains("photo-before") || stage.contains("object-identified") -> "identity-progress"
            stage.contains("publication-concept") -> "concept-island-expanded"
            stage.contains("publication-text") -> "publication-preview-chat"
            stage.contains("generated-visual") -> "publication-image"
            else -> "facts-island-expanded"
        }
        val live = AppGraph.live(context)
        val liveWasActive = live.isActiveFor(clientStoryId)
        dismissExternalLauncherAnr(device)
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            instrumentation.waitForIdleSync()
            Thread.sleep(1_000)
            (device.findObject(By.text("ПОЗЖЕ")) ?: device.findObject(By.text("Позже")))?.click()
            instrumentation.waitForIdleSync()
            // UiAutomator's click returns before the dialog dismissal frame.
            Thread.sleep(500)
            assertTrue("Stage UI missing: $stage", device.currentPackageName == context.packageName)
            scenario.onActivity { activity ->
                fun find(view: View): View? {
                    if (view.contentDescription?.toString() == detailDescription) return view
                    if (view is ViewGroup) for (index in 0 until view.childCount) {
                        find(view.getChildAt(index))?.let { return it }
                    }
                    return null
                }
                find(activity.findViewById(android.R.id.content))?.let { facts ->
                    facts.requestRectangleOnScreen(Rect(0, 0, facts.width, minOf(facts.height, 600)), true)
                }
            }
            instrumentation.waitForIdleSync()
            Thread.sleep(500)
            assertTrue("Detail UI missing: $stage", device.currentPackageName == context.packageName)
            if (stage == "03-facts-after-research") {
                scenario.onActivity { it.onBackPressed() }
                instrumentation.waitForIdleSync()
                assertTrue("Navigating to topics stopped Live", live.isActiveFor(clientStoryId))
                scenario.recreate()
                instrumentation.waitForIdleSync()
                dismissExternalLauncherAnr(device)
                val title = requireNotNull(story.placeName).trim().take(80)
                waitUntil(10_000, "Topic missing after navigation") {
                    dismissExternalLauncherAnr(device)
                    device.findObject(By.text(title)) != null
                }
                val topic = requireNotNull(device.findObject(By.text(title)))
                topic.click()
                instrumentation.waitForIdleSync()
                assertTrue("Returning to the photo stopped Live", live.isActiveFor(clientStoryId))
            }
        }
        if (liveWasActive) assertTrue("Activity navigation stopped Live at $stage", live.isActiveFor(clientStoryId))
        screenshots.add(mapOf("stage" to stage, "story_revision" to story.revision,
            "fact_count" to story.facts.size, "selected_count" to story.facts.count { it.selected },
            "identity_candidate_id" to story.visualIdentity?.candidateId,
            "identity_generation" to story.identityGeneration,
            "identity_images_reviewed_count" to story.identityProgress?.imagesReviewedCount,
            "identity_visual_comparison_verified" to story.identityProgress?.visualComparisonVerified,
            "media_artifacts_written" to false, "image_url" to story.processedImageUrl))
    }

    private fun dismissExternalLauncherAnr(device: UiDevice) {
        // The emulator launcher can show its own ANR above the healthy product.
        // Never dismiss a Street Story ANR or any other application failure.
        if (device.findObject(By.text("Pixel Launcher isn't responding")) == null) return
        requireNotNull(device.findObject(By.text("Close app"))) {
            "External launcher ANR has no close action"
        }.click()
        device.waitForIdle()
    }

    private fun speak(live: LiveSessionController, pcm: File) {
        // Provider turn completion can precede AudioTrack playback draining.
        // A real owner waits for Mira; injecting the next fixture immediately
        // loses its opening words to the normal echo/research input gate.
        var readySince = 0L
        waitUntil(120_000, "Microphone input did not resume before prepared speech") {
            if (live.shouldSuppressMicrophoneInput()) {
                readySince = 0L
                false
            } else {
                if (readySince == 0L) readySince = System.currentTimeMillis()
                System.currentTimeMillis() - readySince >= 750L
            }
        }
        awaitingTurnAfter = live.snapshot().completedTurns
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
        preparedPcmTurnCount++
    }

    private fun awaitAnswer(live: LiveSessionController, label: String) {
        // Wait for provider turn completion, not its first transcript fragment.
        waitUntil(120_000, "Live answer timed out: $label") {
            checkStageWatchdog()
            val state = live.snapshot()
            state.error?.let { error("Live failed during $label: $it") }
            state.active && state.completedTurns > awaitingTurnAfter
        }
        awaitingTurnAfter = live.snapshot().completedTurns
    }

    private fun ownerText(live: LiveSessionController, text: String, label: String) {
        awaitingTurnAfter = live.snapshot().completedTurns
        live.sendText(text)
        awaitAnswer(live, label)
    }

    private fun pollWithOwnerClarification(
        api: ApiClient, storyId: String, live: LiveSessionController,
        evidence: MutableMap<String, Any?>, label: String, clarification: String,
        allowedNeedsReviewCodes: Set<String> = setOf("visual_stale"),
        predicate: (StoryWire) -> Boolean,
    ): StoryWire {
        try {
            return pollStory(api, storyId, 60_000, allowedNeedsReviewCodes, predicate)
        } catch (failure: IllegalStateException) {
            if (!failure.message.orEmpty().startsWith("Timed out waiting for story")) throw failure
        }
        if (label in setOf("fact research", "more facts")) {
            val observed = readStory(api, storyId)
            check(observed.researchOutcome?.outcome != "outcome_unknown") {
                "Research outcome is unknown; retain the original operation instead of another request"
            }
            if (observed.researchPending["facts"] == true) {
                evidence["$label observed_existing_job"] = true
                return pollStory(api, storyId, allowedNeedsReviewCodes = allowedNeedsReviewCodes, predicate = predicate)
            }
        }
        // Real owner clarification through the same native WSS conversation.
        // Backend/tool errors remain failures; this does not fabricate a result.
        evidence["$label text clarification"] = true
        ownerText(live, clarification, "$label clarification")
        return pollStory(api, storyId, allowedNeedsReviewCodes = allowedNeedsReviewCodes, predicate = predicate)
    }

    private fun waitUntil(timeoutMs: Long, message: String, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            checkStageWatchdog()
            if (predicate()) return
            Thread.sleep(250)
        }
        error(message)
    }

    private fun pollStory(
        api: ApiClient,
        storyId: String,
        timeoutMs: Long = RESEARCH_TIMEOUT_MS,
        allowedNeedsReviewCodes: Set<String> = setOf("visual_identity_uncertain"),
        predicate: (StoryWire) -> Boolean,
    ): StoryWire {
        val deadline = System.currentTimeMillis() + timeoutMs
        var last: StoryWire? = null
        while (System.currentTimeMillis() < deadline) {
            checkStageWatchdog()
            last = readStory(api, storyId)
            if (predicate(last)) return last
            val code = last.error?.code.orEmpty()
            if (last.state == StoryStage.NEEDS_REVIEW && code.isNotBlank() && code !in allowedNeedsReviewCodes) {
                error("Story needs review: $code ${last.error?.message.orEmpty()}")
            }
            Thread.sleep(2_000)
        }
        error("Timed out waiting for story; last=${last?.state}")
    }

    private fun readStory(api: ApiClient, storyId: String): StoryWire {
        for (attempt in 0..3) {
            try {
                val story = api.getStory(storyId)
                if (headlessIdentityActive) {
                    identityStoryReads += 1
                    story.identityProgress?.let(::sampleIdentityProgress)
                    assertHeadlessIdentity()
                }
                return story
            } catch (failure: ApiException) {
                if (failure.status !in setOf(502, 503, 504) || attempt == 3) throw failure
                transientStoryReadRetries += 1
                android.util.Log.w("StreetStoryGolden", "story_read_retry status=${failure.status} attempt=${attempt + 1}")
                Thread.sleep(2_000)
            }
        }
        error("Unreachable story read retry state")
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
        private const val OWNER_PROMPT_SHA256 = "d3cf096cec2569198f74fb1b6e2b49612248dd541f45df7ea637afccafc3bd7a"
        private const val PCM_CHUNK_SAMPLES = EfficientVad.FRAME_SAMPLES
        private const val PCM_CHUNK_SLEEP_MS = EfficientVad.FRAME_MS
        private const val RESEARCH_TIMEOUT_MS = 90_000L
        private const val VISUAL_TIMEOUT_MS = 8L * 60 * 1000
        private const val SOCIAL_TIMEOUT_MS = 6L * 60 * 1000
    }
}
