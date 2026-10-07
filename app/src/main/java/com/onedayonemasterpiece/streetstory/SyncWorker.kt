package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.Intent
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

internal fun effectiveSnapshotDraft(state: String, localDraft: String?, remoteDraft: String?): String? =
    if (state in setOf(StoryStage.SCHEDULED, StoryStage.PUBLISHED) && localDraft != null) localDraft else remoteDraft

internal fun shouldPollStory(wire: StoryWire): Boolean =
    listOf("identity", "facts").any { wire.researchPending[it] == true && wire.researchControls[it]?.stopped != true } ||
        wire.state in setOf(StoryStage.QUEUED, StoryStage.VISUAL_PROCESSING, StoryStage.SCHEDULING) ||
        (wire.state == StoryStage.IDENTIFYING && wire.researchControls["identity"]?.stopped != true) ||
        (wire.state == StoryStage.RESEARCHING && wire.researchControls["facts"]?.stopped != true)

object SyncScheduler {
    private const val UNIQUE_WORK = "street-story-sync"
    fun enqueue(context: Context, delaySeconds: Long = 0L) {
        val builder = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.LINEAR, 15, TimeUnit.SECONDS)
        if (delaySeconds > 0) builder.setInitialDelay(delaySeconds, TimeUnit.SECONDS)
        WorkManager.getInstance(context).enqueueUniqueWork(UNIQUE_WORK, ExistingWorkPolicy.APPEND_OR_REPLACE, builder.build())
    }
}

class SyncWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    private val gson = Gson()

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val config = AppGraph.config(applicationContext)
        val base = config.backendUrl
        val token = config.deviceToken
        if (base.isNullOrBlank() || token.isNullOrBlank()) return@withContext Result.success()
        val api = ApiClient(base, token) { PhotoAssets.open(applicationContext, it) }
        val store = AppGraph.store(applicationContext)
        val feed = FeedProjectionStore(applicationContext)
        val research = ResearchProjectionStore(applicationContext)
        var pollAgain = false
        val capabilities = runCatching { api.capabilities() }.getOrNull()
        try {
            for (initial in store.stories()) {
                try {
                    if (syncStory(store, feed, research, api, initial, capabilities)) pollAgain = true
                } catch (exc: SourceUnavailableException) {
                    // Initial local-only intake can also lose a temporary grant.
                    store.setStage(initial.clientStoryId, initial.stage, SOURCE_UNAVAILABLE_MESSAGE)
                } catch (exc: ApiException) {
                    store.setStage(initial.clientStoryId, if (exc.retryable) initial.stage else StoryStage.NEEDS_REVIEW, exc.message)
                    if (exc.retryable) pollAgain = true
                } catch (exc: ApiProtocolException) {
                    store.setStage(initial.clientStoryId, StoryStage.NEEDS_REVIEW, exc.message)
                } catch (exc: IOException) {
                    store.setStage(initial.clientStoryId, initial.stage, "Сеть недоступна · локальные данные сохранены")
                    pollAgain = true
                } catch (exc: Exception) {
                    store.setStage(initial.clientStoryId, StoryStage.NEEDS_REVIEW, "Нужна проверка: ${exc.message}")
                } finally {
                    broadcast()
                }
            }
        } finally {
            feed.close()
        }
        if (pollAgain) SyncScheduler.enqueue(applicationContext, POLL_SECONDS)
        Result.success()
    }

    internal fun syncStory(
        store: StoryStore,
        feed: FeedProjectionStore,
        research: ResearchProjectionStore,
        api: ApiClient,
        initial: StorySnapshot,
        capabilities: CapabilitiesWire?,
    ): Boolean {
        var story = store.story(initial.clientStoryId) ?: return false
        var remote = if (story.serverStoryId.isNullOrBlank()) api.createStory(story) else api.getStory(requireNotNull(story.serverStoryId))
        validateStoryIdentity(story, remote)
        if (story.serverStoryId.isNullOrBlank()) store.setServerIdentity(story.clientStoryId, remote.id)
        applyRemote(store, feed, research, story.clientStoryId, remote)
        var sourceUnavailable = false
        val needsSource = remote.state in setOf(StoryStage.PHOTO_READY, StoryStage.IDENTIFYING, StoryStage.VISUAL_PROCESSING) ||
            remote.researchPending["identity"] == true ||
            store.pendingOperations(story.clientStoryId).any { it.kind == "visual" }
        if (remote.sourceAvailable == false && needsSource) {
            // The backend keeps only RAM bytes. Rehydrate the same immutable upload
            // from its persisted gallery grant, preserving jobs and editorial state.
            try {
                remote = api.createStory(story)
                validateStoryIdentity(story, remote)
            } catch (exc: SourceUnavailableException) {
                sourceUnavailable = true
                android.util.Log.i("StreetStorySync", "source_unavailable story_id=${story.clientStoryId} state=${remote.state}")
            }
        }
        val identityBackfillEligible = remote.visualIdentity == null && remote.researchControls["identity"]?.stopped != true &&
            remote.state !in setOf(
                StoryStage.IDENTIFYING,
                StoryStage.SCHEDULED,
                StoryStage.PUBLISHED,
                StoryStage.SCHEDULING,
            )
        if (identityBackfillEligible && !sourceUnavailable) {
            remote = api.ensureIdentity(remote.id)
            validateStoryIdentity(story, remote)
        }
        if (story.serverStoryId.isNullOrBlank()) store.setServerIdentity(story.clientStoryId, remote.id)
        applyRemote(store, feed, research, story.clientStoryId, remote)
        story = requireNotNull(store.story(story.clientStoryId))
        val serverId = requireNotNull(story.serverStoryId)
        PhotoImportTelemetry.pending(applicationContext, story.clientStoryId)?.let { payload ->
            runCatching { api.photoDiagnostic(serverId, payload) }.onSuccess {
                PhotoImportTelemetry.acknowledge(applicationContext, story.clientStoryId, payload)
            }
        }

        for (session in store.finishedVoiceSessions().filter { it.storyId == story.clientStoryId }) {
            syncVoice(store, api, serverId, session)
        }

        for (operation in store.pendingOperations(story.clientStoryId)) {
            // Only an unsent visual depends on SOURCE. Preserve its request ID
            // and continue selection/text/voice sync while access is restored.
            if (sourceUnavailable && operation.kind == "visual") continue
            try {
                remote = api.mutate(serverId, operation.kind, operation.payloadJson, operation.requestKey)
                validateStoryIdentity(story, remote)
                applyRemote(store, feed, research, story.clientStoryId, remote)
                store.markOperationDone(operation.id)
            } catch (exc: ApiException) {
                if (exc.retryable) {
                    store.markOperationRetry(operation.id, exc.message)
                    return true
                }
                store.markOperationDone(operation.id)
                store.setStage(story.clientStoryId, StoryStage.NEEDS_REVIEW, exc.message)
                return false
            }
        }

        remote = api.getStory(serverId)
        validateStoryIdentity(story, remote)
        val previousUrl = store.story(story.clientStoryId)?.processedImageUrl
        applyRemote(store, feed, research, story.clientStoryId, remote)
        if (remote.destinations.isEmpty() && capabilities != null && capabilities.destinations.isNotEmpty()) {
            store.replaceDestinations(story.clientStoryId, capabilities.destinations.map { it.local() })
        }
        val current = requireNotNull(store.story(story.clientStoryId))
        val assetUrl = current.processedImageUrl
        if (!assetUrl.isNullOrBlank() && (current.processedImagePath.isNullOrBlank() || previousUrl != assetUrl || !PhotoAssets.available(current.processedImagePath))) {
            current.processedImagePath?.let(PhotoAssets::releaseTemporary)
            store.setProcessedImagePath(story.clientStoryId, PhotoAssets.retainTemporary(api.readAsset(assetUrl)))
        }
        // The visual queue can remain active in needs_review after an uncertain
        // first result. Its authoritative job projection, not a microphone, keeps
        // readback polling alive. Explicitly paused research stops polling.
        if (sourceUnavailable) {
            store.setStage(story.clientStoryId, current.stage, SOURCE_UNAVAILABLE_MESSAGE)
            // A revoked grant requires owner/provider access restoration. Do not
            // hot-loop a visual; reopening the app or reattaching enqueues sync.
            return (remote.researchPending["facts"] == true && remote.researchControls["facts"]?.stopped != true) ||
                remote.state in setOf(StoryStage.QUEUED, StoryStage.VISUAL_PROCESSING, StoryStage.SCHEDULING)
        }
        return shouldPollStory(remote)
    }

    private fun syncVoice(store: StoryStore, api: ApiClient, serverStoryId: String, session: VoiceSessionSnapshot) {
        val localChunks = store.chunks(session.sessionId)
        check(localChunks.size == session.chunkCount) { "local voice chunk registry changed" }
        var receipt = api.openVoiceSession(serverStoryId, session)
        reconcileChunks(store, session, localChunks, receipt)
        store.markVoiceServerInitialized(session.sessionId)
        if (receipt.recordingFinished) {
            requireCompleteManifest(session, localChunks, receipt)
            store.markVoiceComplete(session.sessionId)
            return
        }
        for (chunk in store.pendingChunks(session.sessionId)) {
            receipt = api.uploadChunk(serverStoryId, session, chunk)
            reconcileChunks(store, session, localChunks, receipt)
        }
        val refreshed = requireNotNull(store.voiceSession(session.sessionId))
        if (store.pendingChunks(session.sessionId).isNotEmpty()) return
        receipt = api.completeVoice(serverStoryId, refreshed, localChunks)
        reconcileChunks(store, refreshed, localChunks, receipt)
        if (!receipt.recordingFinished) throw ApiProtocolException("Backend did not durably finish the complete voice manifest")
        requireCompleteManifest(refreshed, localChunks, receipt)
        store.markVoiceComplete(refreshed.sessionId)
    }

    private fun reconcileChunks(store: StoryStore, session: VoiceSessionSnapshot, local: List<ChunkRecord>, receipt: VoiceReceipt) {
        if (receipt.sessionId != session.sessionId) throw ApiProtocolException("Voice receipt belongs to another session")
        val byIndex = local.associateBy { it.chunkIndex }
        for (remote in receipt.received) {
            val chunk = byIndex[remote.index] ?: throw ApiProtocolException("Backend reports an unknown voice chunk")
            if (!chunk.sha256.equals(remote.sha256, ignoreCase = true)) throw ApiProtocolException("Voice chunk hash conflict at ${remote.index}")
            store.markChunkUploaded(session.sessionId, remote.index)
        }
    }

    private fun requireCompleteManifest(session: VoiceSessionSnapshot, local: List<ChunkRecord>, receipt: VoiceReceipt) {
        val received = receipt.received.associate { it.index to it.sha256.lowercase() }
        if (received.size != local.size || local.any { received[it.chunkIndex] != it.sha256.lowercase() }) {
            throw ApiProtocolException("Backend completed voice with a different chunk manifest for ${session.sessionId}")
        }
    }

    private fun validateStoryIdentity(local: StorySnapshot, remote: StoryWire) {
        if (remote.id.isBlank() || remote.clientStoryId != local.clientStoryId) throw ApiProtocolException("Story identity mismatch")
        if (!local.serverStoryId.isNullOrBlank() && local.serverStoryId != remote.id) throw ApiProtocolException("Backend story ID changed")
    }

    private fun applyRemote(
        store: StoryStore,
        feed: FeedProjectionStore,
        research: ResearchProjectionStore,
        storyId: String,
        wire: StoryWire,
    ) {
        val state = normalizeState(wire.state)
        val localDraft = store.story(storyId)?.draftText
        val draft = effectiveSnapshotDraft(state, localDraft, wire.draftText)
        store.setServerSnapshot(
            storyId,
            state,
            wire.placeName,
            wire.summary,
            draft,
            wire.processedImageUrl,
            wire.scheduledFor,
            wire.publishedAt,
            wire.error?.message,
            wire.revision,
        )
        store.replaceFacts(
            storyId,
            wire.facts.map {
                FactSnapshot(
                    it.factId,
                    it.text,
                    it.confidence,
                    it.eligibleForSelection,
                    it.selected && it.eligibleForSelection,
                    gson.toJson(it.sources),
                )
            },
        )
        feed.replaceVoiceMessages(storyId, wire.voiceMessages)
        feed.replaceLiveMessages(storyId, wire.liveMessages)
        research.replace(storyId, wire)
        if (wire.destinations.isNotEmpty()) store.replaceDestinations(storyId, wire.destinations.map { it.local() })
    }

    private fun DestinationWire.local() = DestinationSnapshot(alias, label, provider, status, selected)

    private fun normalizeState(value: String): String = when (value) {
        StoryStage.PHOTO_READY, "created", "voice_pending" -> StoryStage.PHOTO_READY
        StoryStage.IDENTIFYING -> StoryStage.IDENTIFYING
        StoryStage.IDENTITY_READY -> StoryStage.IDENTITY_READY
        StoryStage.QUEUED, "uploaded" -> StoryStage.QUEUED
        StoryStage.VOICE_READY -> StoryStage.VOICE_READY
        StoryStage.RESEARCHING, "processing" -> StoryStage.RESEARCHING
        StoryStage.REVIEW, "facts_ready" -> StoryStage.REVIEW
        StoryStage.VISUAL_PROCESSING -> StoryStage.VISUAL_PROCESSING
        StoryStage.VISUAL_BLOCKED -> StoryStage.VISUAL_BLOCKED
        StoryStage.READY_TO_PUBLISH, "ready" -> StoryStage.READY_TO_PUBLISH
        StoryStage.SCHEDULING, "publish_pending" -> StoryStage.SCHEDULING
        StoryStage.SCHEDULED -> StoryStage.SCHEDULED
        StoryStage.PUBLISHED -> StoryStage.PUBLISHED
        StoryStage.NEEDS_REVIEW, "failed" -> StoryStage.NEEDS_REVIEW
        else -> StoryStage.NEEDS_REVIEW
    }

    private fun broadcast() {
        applicationContext.sendBroadcast(Intent(ACTION_STATE_CHANGED).setPackage(applicationContext.packageName))
    }

    companion object {
        const val ACTION_STATE_CHANGED = "com.onedayonemasterpiece.streetstory.SYNC_CHANGED"
        // Active identity/research is user-visible; keep checklist projection fresh enough to show stages.
        private const val POLL_SECONDS = 3L
    }
}
