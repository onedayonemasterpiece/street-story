package com.onedayonemasterpiece.streetstory

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    @Before fun reset(){context.deleteDatabase("street-story.db");File(context.filesDir,"stories").deleteRecursively();File(context.filesDir,"audio").deleteRecursively()}
    @After fun close(){ }

    @Test fun photoDraftChunksChoicesAndFeedSurviveReopen(){
        val png=Base64.getDecoder().decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlJkAAAAASUVORK5CYII=")
        val imported=PhotoImporter.importStream(context,ByteArrayInputStream(png),"image/png","story-test-001")
        assertTrue(File(imported.path).isFile);assertTrue(File(imported.path).absolutePath.startsWith(context.filesDir.absolutePath));assertEquals(64,imported.sha256.length)
        StoryStore(context).use{store->store.createStory(imported)}
        StoryStore(context).use{store->
            assertEquals("story-test-001",store.stories().single().clientStoryId)
            val voice=store.createVoiceSession("story-test-001",RecordingKind.INITIAL,"emulator")
            val audio=File(context.filesDir,"audio/test.m4a").apply{parentFile?.mkdirs();writeBytes(byteArrayOf(1,2,3,4))}
            val sha="a".repeat(64)
            store.addChunk(voice.sessionId,0,0,6000,0,6500,audio.absolutePath,sha,AudioProfile.MIME_M4A)
            store.finishVoiceSession(voice.sessionId,"2026-09-08T12:00:10+02:00",7000,1000)
            store.replaceFacts("story-test-001",listOf(
                FactSnapshot("supported","Проверенный факт",0.9,true,true,"[]"),
                FactSnapshot("unsupported","Без источника",0.2,false,true,"[]"),
            ))
            assertFalse(store.facts("story-test-001").first{it.factId=="unsupported"}.selected)
            store.setFactSelected("story-test-001","supported",false)
            store.replaceFacts("story-test-001",listOf(FactSnapshot("supported","Проверенный факт",0.95,true,true,"[]")))
            assertFalse(store.facts("story-test-001").single().selected)
            val a=store.enqueueOperation("story-test-001","visual","ss-visual-test","{}")
            val b=store.enqueueOperation("story-test-001","visual","ss-visual-test","{}")
            assertEquals(a.id,b.id)
            store.setStage("story-test-001",StoryStage.REVIEW)
        }
        StoryStore(context).use{store->
            val story=store.story("story-test-001")!!;assertEquals(StoryStage.REVIEW,story.stage)
            val voice=store.latestVoiceSession("story-test-001")!!;assertEquals(CaptureState.FINISHED,voice.captureState)
            assertEquals(1,store.chunks(voice.sessionId).size);assertTrue(File(store.chunks(voice.sessionId).single().path).isFile)
            assertEquals(1,store.pendingOperations("story-test-001").size)
        }
    }
}
