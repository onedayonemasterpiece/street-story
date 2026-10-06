package com.onedayonemasterpiece.streetstory

import android.content.ContentValues
import android.content.Context
import android.graphics.Rect
import android.view.View
import android.view.ViewGroup
import android.provider.MediaStore
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
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
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
        val baseUrl = config.requireString("backend_url")
        val safeAlias = config.requireString("safe_alias")
        val token = File(root, "token.txt").readText().trim()
        require(baseUrl.startsWith("https://") && token.isNotBlank())
        val keepPublication = InstrumentationRegistry.getArguments().getString("keepPublication") == "true"
        val identityOnly = InstrumentationRegistry.getArguments().getString("identityOnly") == "true"
        val moreOnly = InstrumentationRegistry.getArguments().getString("moreOnly") == "true"
        val resumeStoryId = InstrumentationRegistry.getArguments().getString("resumeStoryId").orEmpty().trim()
        require(resumeStoryId.isEmpty() || (Regex("story_[a-zA-Z0-9]{8,64}").matches(resumeStoryId) && !identityOnly))
        require(!identityOnly || !keepPublication)
        require(!moreOnly || (!identityOnly && !keepPublication))
        require(!keepPublication || safeAlias == "street_story_e2e_20260928_tg")
        require(isExplicitTestAlias(safeAlias))

        beginStage("photo", 120_000)
        val photoFile = File(root, "photo.jpg")
        val pcmFiles = (1..6).map { File(root, "voice-$it.pcm") }
        require(photoFile.isFile && (identityOnly || pcmFiles.all(File::isFile)))

        val appConfig = AppGraph.config(context)
        appConfig.backendUrl = baseUrl
        appConfig.deviceToken = token

        val photoSha = sha256(photoFile.readBytes())
        val imported = PhotoImporter.import(context, insertIntoMediaStore(photoFile))
        assertEquals(photoSha, imported.sha256)
        assertNotNull(imported.latitude)
        assertNotNull(imported.longitude)
        assertTrue(kotlin.math.abs(requireNotNull(imported.latitude) - config.get("latitude").asDouble) < 0.0025)
        assertTrue(kotlin.math.abs(requireNotNull(imported.longitude) - config.get("longitude").asDouble) < 0.0025)

        val store = AppGraph.store(context)
        val api = ApiClient(baseUrl, token)
        val resumed = if (resumeStoryId.isBlank()) null else api.getStory(resumeStoryId)
        if (resumed != null) {
            assertEquals("Resume photo differs from saved story", photoSha, resumed.photoSha256)
            require(resumed.clientStoryId.isNotBlank())
            val raw = rawStory(baseUrl, token, resumeStoryId)
            val publication = raw.get("publication")
            require(publication == null || publication.isJsonNull ||
                (publication.isJsonObject && publication.asJsonObject.entrySet().isEmpty())) {
                "Resume must reconcile an existing publication separately; never schedule it twice"
            }
            require(resumed.state != StoryStage.VISUAL_PROCESSING) { "Read back the existing generation before continuing" }
        }
        val local = store.createStory(if (resumed == null) imported else imported.copy(clientStoryId = resumed.clientStoryId))
        val created = resumed ?: api.createStory(local)
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
            "destination_alias" to safeAlias,
            "physical_mic" to false,
            "prepared_owner_photo" to true,
            "photo_coordinates_source" to "embedded_exif",
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
        evidence["stage_screenshots"] = screenshots
        evidence["stage_timings"] = stageTimings
        evidence["identity_progress_samples"] = identityProgressSamples
        fun capture(stage: String, story: StoryWire) {
            captureStage(stage, story, local.clientStoryId, store, screenshots)
        }

        try {
            beginStage("identity", 5L * 60 * 1000)
            assertHeadlessIdentity()
            capture("01-photo-before-research", api.getStory(storyId))
            var story = pollStory(api, storyId, stageDeadline - System.currentTimeMillis(),
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
            assertTrue("Match has no retained decoded reference images", references.isNotEmpty())
            val modelImageHashes = references.map { reference ->
                val hash = reference.requireString("model_image_sha256")
                check(hash.matches(Regex("[0-9a-f]{64}"))) { "Invalid visual proof image digest" }
                check(reference.requireString("source_url").startsWith("https://")) { "Reference source provenance missing" }
                hash
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
                "identity_model_image_count" to modelImageHashes.size,
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
            val savedFactsReady = story.sourceCount > 0 && story.facts.count { it.eligibleForSelection } >= 2
            evidence["initial_fact_inventory_reused"] = savedFactsReady
            evidence["initial_eligible_fact_count"] = story.facts.count { it.eligibleForSelection }
            if (!savedFactsReady) {
                speak(live, pcmFiles[2])
                awaitAnswer(live, "research request")
            }
            story = pollWithOwnerClarification(
                api, storyId, live, evidence, "fact research",
                "Продолжи поиск фактов о подтверждённом объекте на фотографии. Прочитай текущее состояние темы, используй именно его подтверждённый POI ID и сохрани проверенные факты с источниками. Не выбирай факты и не готовь текст публикации.",
                allowedNeedsReviewCodes = setOf("visual_stale"),
            ) {
                it.sourceCount > 0 && it.facts.count { fact -> fact.eligibleForSelection } >= 2
            }
            assertTrue(story.sourceCount > 0)
            assertTrue(story.sources.all { !it.title.isNullOrBlank() && it.url.startsWith("https://") })
            require(story.facts.count { it.evidenceSupported } >= 2)
            capture("03-facts-after-research", story)

            beginStage("selection", 4L * 60 * 1000)
            val savedSelectionReady = resumed != null && story.facts.count { it.selected && it.eligibleForSelection } == 2
            evidence["selection_history_reused"] = savedSelectionReady
            if (!savedSelectionReady) {
                speak(live, pcmFiles[3])
                awaitAnswer(live, "fact selection")
                story = pollWithOwnerClarification(api, storyId, live, evidence, "fact selection",
                    "Выбери для поста ровно два самых надёжных подтверждённых факта из текущего списка и сохрани этот выбор. Сейчас заверши именно выбор фактов; платформу публикации я укажу позже.") {
                    it.facts.count { fact -> fact.selected && fact.evidenceSupported } == 2
                }
            }
            capture("04-facts-selected", story)
            val selectedFactIds = story.facts.filter { it.selected && it.evidenceSupported }.map { it.factId }

            beginStage("concept", 3L * 60 * 1000)
            val savedConceptReady = resumed != null && !story.publicationConcept.isNullOrBlank()
            evidence["concept_history_reused"] = savedConceptReady
            if (!savedConceptReady) {
                ownerText(live, "Концепция поста: исторический вход в город, история которого видна в кирпичных башнях на фотографии. Сохрани эту концепцию; выбор двух фактов оставь без изменений.", "publication concept")
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
                    "Уточняю правку: сделай текущий текст короче и живее, без канцелярита. Используй только два уже выбранных факта; выбор и изображение не меняй.") {
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
            val savedVisualReady = resumed != null && savedDraftReady && story.state == StoryStage.READY_TO_PUBLISH && !story.processedImageUrl.isNullOrBlank()
            evidence["visual_history_reused"] = savedVisualReady
            if (!savedVisualReady) {
                speak(live, pcmFiles[5])
                awaitAnswer(live, "visual-only edit")
            }
            val visualStarted = api.getStory(storyId)
            if (visualStarted.state != StoryStage.VISUAL_PROCESSING && visualStarted.processedImageUrl.isNullOrBlank()) {
                evidence["visual_text_clarification"] = true
                ownerText(live, "Создай инфографичную картинку по исходной фотографии, сохранённой концепции и двум выбранным фактам. Сделай её чуть теплее. Текущий текст поста оставь без изменений.", "visual clarification")
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
            live.sendText(
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

            live.sendText("Подтверждаю именно показанную карточку публикации.")
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
                    "image_sha256" to imageSha,
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
            store.close()
            if (identityOnly) check(evidence["no_live_or_recording_started"] == true) {
                "Headless identity started Live or recording; retained evidence contains the failure"
            }
        }
        assertTrue(if (keepPublication) publicationScheduled else cancelConfirmed)
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
            val image = File(root, "screenshot-visual.img")
            val config = AppGraph.config(context)
            ApiClient(requireNotNull(config.backendUrl), requireNotNull(config.deviceToken)).downloadAsset(requireNotNull(story.processedImageUrl), image)
            store.setProcessedImagePath(clientStoryId, image.absolutePath)
        }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val device = UiDevice.getInstance(instrumentation)
        val directory = File(root, "screenshots").apply { mkdirs() }
        val (detailDescription, detailSuffix) = when {
            stage.contains("photo-before") || stage.contains("object-identified") -> "identity-progress" to "identity"
            stage.contains("publication-concept") -> "concept-island-expanded" to "concept"
            stage.contains("publication-text") -> "publication-preview-chat" to "text"
            stage.contains("generated-visual") -> "publication-image" to "image"
            else -> "facts-island-expanded" to "facts"
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
            assertTrue("Stage screenshot failed: $stage", device.takeScreenshot(File(directory, "$stage.png")))
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
            assertTrue("Detail screenshot failed: $stage", device.takeScreenshot(File(directory, "$stage-$detailSuffix.png")))
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
            "files" to listOf("$stage.png", "$stage-$detailSuffix.png")))
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
        private const val OWNER_PROMPT_SHA256 = "92496e7fd70419af40312865f486907fecea9ab84fdb35edc0fbef427faec424"
        private const val PCM_CHUNK_SAMPLES = 4096
        private const val PCM_CHUNK_SLEEP_MS = 260L
        private const val RESEARCH_TIMEOUT_MS = 90_000L
        private const val VISUAL_TIMEOUT_MS = 8L * 60 * 1000
        private const val SOCIAL_TIMEOUT_MS = 6L * 60 * 1000
    }
}
