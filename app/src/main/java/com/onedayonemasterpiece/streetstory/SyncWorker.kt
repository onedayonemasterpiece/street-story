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
        val api = ApiClient(base, token)
        val store = AppGraph.store(applicationContext)
        var pollAgain = false
        val capabilities = runCatching { api.capabilities() }.getOrNull()
        for (initial in store.stories()) {
            try {
                if (syncStory(store, api, initial, capabilities)) pollAgain = true
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
        if (pollAgain) SyncScheduler.enqueue(applicationContext, POLL_SECONDS)
        Result.success()
    }

    private fun syncStory(store: StoryStore, api: ApiClient, initial: StorySnapshot, capabilities: CapabilitiesWire?): Boolean {
        var story = store.story(initial.clientStoryId) ?: return false
        var remote = if (story.serverStoryId.isNullOrBlank()) api.createStory(story) else api.getStory(requireNotNull(story.serverStoryId))
        validateStoryIdentity(story, remote)
        if (story.serverStoryId.isNullOrBlank()) store.setServerIdentity(story.clientStoryId, remote.id)
        applyRemote(store, story.clientStoryId, remote)
        story = requireNotNull(store.story(story.clientStoryId))
        val serverId = requireNotNull(story.serverStoryId)

        for (session in store.finishedVoiceSessions().filter { it.storyId == story.clientStoryId }) {
            syncVoice(store, api, serverId, session)
        }

        for (operation in store.pendingOperations(story.clientStoryId)) {
            try {
                remote = api.mutate(serverId, operation.kind, operation.payloadJson, operation.requestKey)
                validateStoryIdentity(story, remote)
                applyRemote(store, story.clientStoryId, remote)
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
        applyRemote(store, story.clientStoryId, remote)
        if (capabilities != null && capabilities.destinations.isNotEmpty()) {
            store.replaceDestinations(story.clientStoryId, capabilities.destinations.map { it.local() })
        }
        val current = requireNotNull(store.story(story.clientStoryId))
        val assetUrl = current.processedImageUrl
        if (!assetUrl.isNullOrBlank() && (current.processedImagePath.isNullOrBlank() || previousUrl != assetUrl || !File(current.processedImagePath).isFile)) {
            val target = File(applicationContext.filesDir, "stories/${story.clientStoryId}/processed.img")
            api.downloadAsset(assetUrl, target)
            store.setProcessedImagePath(story.clientStoryId, target.absolutePath)
        }
        return current.stage in setOf(StoryStage.QUEUED, StoryStage.RESEARCHING, StoryStage.VISUAL_PROCESSING, StoryStage.SCHEDULING)
    }

    private fun syncVoice(store: StoryStore, api: ApiClient, serverStoryId: String, session: VoiceSessionSnapshot) {
        val localChunks = store.chunks(session.sessionId)
        check(localChunks.size == session.chunkCount) { "local voice chunk registry changed" }
        var receipt = api.openVoiceSession(serverStoryId, session)
        reconcileChunks(store, session, localChunks, receipt)
        store.markVoiceServerInitialized(session.sessionId)
        if (receipt.recordingFinished) {
            requireCompleteManifest(session, localChunks, receipt)
            queueRefinementIfNeeded(store, session)
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
        queueRefinementIfNeeded(store, refreshed)
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

    private fun queueRefinementIfNeeded(store: StoryStore, session: VoiceSessionSnapshot) {
        if (session.kind != RecordingKind.REFINEMENT) return
        val key = newRequestKey("refinement", session.sessionId)
        store.enqueueOperation(session.storyId, "refinements", key, gson.toJson(mapOf("voice_session_id" to session.sessionId)))
    }

    private fun validateStoryIdentity(local: StorySnapshot, remote: StoryWire) {
        if (remote.id.isBlank() || remote.clientStoryId != local.clientStoryId) throw ApiProtocolException("Story identity mismatch")
        if (!local.serverStoryId.isNullOrBlank() && local.serverStoryId != remote.id) throw ApiProtocolException("Backend story ID changed")
    }

    private fun applyRemote(store: StoryStore, storyId: String, wire: StoryWire) {
        val state = normalizeState(wire.state)
        store.setServerSnapshot(storyId, state, wire.placeName, wire.summary, wire.draftText, wire.processedImageUrl,
            wire.scheduledFor, wire.publishedAt, wire.error?.message, wire.revision)
        store.replaceFacts(storyId, wire.facts.map { FactSnapshot(it.factId, it.text, it.confidence, it.evidenceSupported,
            it.selected && it.evidenceSupported, gson.toJson(it.sources)) })
        if (wire.destinations.isNotEmpty()) store.replaceDestinations(storyId, wire.destinations.map { it.local() })
    }

    private fun DestinationWire.local() = DestinationSnapshot(alias, label, provider, status, selected)

    private fun normalizeState(value: String): String = when (value) {
        StoryStage.PHOTO_READY, "created", "voice_pending" -> StoryStage.PHOTO_READY
        StoryStage.QUEUED, "uploaded" -> StoryStage.QUEUED
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
        private const val POLL_SECONDS = 20L
    }
}
