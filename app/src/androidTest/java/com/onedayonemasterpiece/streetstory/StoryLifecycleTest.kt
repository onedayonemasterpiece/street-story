package com.onedayonemasterpiece.streetstory

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.File
import java.util.Base64

@RunWith(AndroidJUnit4::class)
class StoryLifecycleTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Before
    fun resetFiles() {
        File(context.filesDir, "stories").deleteRecursively()
        File(context.filesDir, "audio").deleteRecursively()
    }

    @Test
    fun deleteStoryRemovesOwnedFilesAndDatabaseRows() {
        val storyId = "story-delete-${System.nanoTime()}"
        val png = Base64.getDecoder().decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlJkAAAAASUVORK5CYII=")
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(png), "image/png", storyId)
        val photo = File(imported.path)
        val audio: File

        StoryStore(context).use { store ->
            store.createStory(imported)
            val voice = store.createVoiceSession(storyId, RecordingKind.REFINEMENT, "emulator")
            audio = File(context.filesDir, "audio/delete-${voice.sessionId}.m4a").apply {
                parentFile?.mkdirs()
                writeBytes(byteArrayOf(1, 2, 3))
            }
            store.addChunk(
                voice.sessionId,
                0,
                0,
                1000,
                0,
                1100,
                audio.absolutePath,
                "c".repeat(64),
                AudioProfile.MIME_M4A,
            )
            store.discardVoiceSession(voice.sessionId)
            // discard removes its chunk file; use an owned processed file as a second
            // durable file to prove story-directory cleanup as well.
            val processed = File(context.filesDir, "stories/$storyId/processed.img").apply {
                parentFile?.mkdirs()
                writeBytes(byteArrayOf(4, 5, 6))
            }
            store.setProcessedImagePath(storyId, processed.absolutePath)
            store.deleteStory(storyId)
            assertNull(store.story(storyId))
            assertTrue(store.facts(storyId).isEmpty())
        }

        assertFalse(photo.exists())
        assertFalse(File(context.filesDir, "stories/$storyId").exists())
    }

    @Test
    fun photoDraftChunksChoicesAndCrashRecoverySurviveReopen() {
        val storyId = "story-test-${System.nanoTime()}"
        val png = Base64.getDecoder().decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlJkAAAAASUVORK5CYII=")
        val imported = PhotoImporter.importStream(context, ByteArrayInputStream(png), "image/png", storyId)
        assertTrue(File(imported.path).isFile)
        assertTrue(File(imported.path).absolutePath.startsWith(context.filesDir.absolutePath))
        assertEquals(64, imported.sha256.length)

        StoryStore(context).use { store -> store.createStory(imported) }
        StoryStore(context).use { store ->
            assertEquals(storyId, store.story(storyId)!!.clientStoryId)
            val voice = store.createVoiceSession(storyId, RecordingKind.INITIAL, "emulator")
            val audio = File(context.filesDir, "audio/test-${voice.sessionId}.m4a").apply {
                parentFile?.mkdirs()
                writeBytes(byteArrayOf(1, 2, 3, 4))
            }
            val sha = "a".repeat(64)
            store.addChunk(voice.sessionId, 0, 0, 6000, 0, 6500, audio.absolutePath, sha, AudioProfile.MIME_M4A)
            store.finishVoiceSession(voice.sessionId, "2026-09-08T12:00:10+02:00", 7000, 1000)

            store.replaceFacts(storyId, listOf(
                FactSnapshot("supported", "Проверенный факт", 0.9, true, true, "[]"),
                FactSnapshot("unsupported", "Без источника", 0.2, false, true, "[]"),
            ))
            assertFalse(store.facts(storyId).first { it.factId == "unsupported" }.selected)
            store.setFactSelected(storyId, "supported", false)
            store.replaceFacts(storyId, listOf(FactSnapshot("supported", "Проверенный факт", 0.95, true, true, "[]")))
            assertFalse(store.facts(storyId).single().selected)

            store.setDraftText(storyId, "Моя локальная правка")
            store.setServerSnapshot(
                storyId, StoryStage.READY_TO_PUBLISH, "Место", "Резюме", "Серверный черновик",
                null, null, null, null, 3,
            )
            assertEquals("Моя локальная правка", store.story(storyId)!!.draftText)

            val a = store.enqueueOperation(storyId, "visual", "ss-visual-$storyId", "{}")
            val b = store.enqueueOperation(storyId, "visual", "ss-visual-$storyId", "{}")
            assertEquals(a.id, b.id)
            store.setStage(storyId, StoryStage.REVIEW)

            val interrupted = store.createVoiceSession(storyId, RecordingKind.REFINEMENT, "emulator")
            val crashAudio = File(context.filesDir, "audio/crash-${interrupted.sessionId}.m4a").apply {
                writeBytes(byteArrayOf(5, 6, 7))
            }
            store.addChunk(interrupted.sessionId, 0, 0, 6000, 0, 6200, crashAudio.absolutePath, "b".repeat(64), AudioProfile.MIME_M4A)
            store.updateCaptureProgress(interrupted.sessionId, 120_000, 125_000, 0, 5_000, CaptureActivity.VOICE)
            store.markInterruptedRecordingsPaused()
            val recovered = store.voiceSession(interrupted.sessionId)!!
            assertEquals(CaptureState.PAUSED, recovered.captureState)
            assertEquals(6000, recovered.durationMs)
            assertEquals(6000, store.persistedDuration(interrupted.sessionId))
            store.discardVoiceSession(interrupted.sessionId)
        }

        StoryStore(context).use { store ->
            val story = store.story(storyId)!!
            assertEquals(StoryStage.REVIEW, story.stage)
            assertEquals("Моя локальная правка", story.draftText)
            assertEquals(CaptureState.FINISHED, store.latestVoiceSession(storyId)!!.captureState)
            assertNull(store.activeVoiceSession())
            assertEquals(1, store.pendingOperations(storyId).size)
            assertTrue(store.stories().any { it.clientStoryId == storyId })
        }
    }
}
