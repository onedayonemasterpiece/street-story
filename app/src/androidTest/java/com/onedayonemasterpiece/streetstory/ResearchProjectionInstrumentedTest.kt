package com.onedayonemasterpiece.streetstory

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ResearchProjectionInstrumentedTest {
    @Test
    fun persistsIdentitySourcesAndResearchVoiceRevision() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val store = ResearchProjectionStore(context)
        val storyId = "research-projection-test"
        store.clear(storyId)

        val candidate = PlaceCandidateWire().apply {
            candidateId = "wiki:1"
            name = "Дом Советов"
            type = "wikipedia"
            url = "https://ru.wikipedia.org/wiki/Test"
        }
        val identity = VisualIdentityWire().apply {
            status = "match"
            candidateId = "wiki:1"
            candidateName = "Дом Советов"
            confidence = 0.94
            observations = arrayListOf("Силуэт совпадает")
            candidates = arrayListOf(candidate)
        }
        val source = SourceWire().apply {
            type = "wikipedia"
            title = "Дом Советов"
            url = "https://ru.wikipedia.org/wiki/Test"
        }
        val wire = StoryWire().apply {
            visualIdentity = identity
            sources = arrayListOf(source)
            sourceCount = 1
            researchRevision = "revision-1"
            researchVoiceIds = arrayListOf("voice-1", "voice-2", "voice-3")
        }

        store.replace(storyId, wire)
        val saved = store.get(storyId)
        assertNotNull(saved)
        assertEquals("match", saved?.identityStatus)
        assertEquals("wiki:1", saved?.candidateId)
        assertEquals(listOf("voice-1", "voice-2", "voice-3"), saved?.researchVoiceIds)
        assertEquals(1, saved?.sourceCount)
        assertEquals("Дом Советов", saved?.sources?.single()?.title)

        store.clear(storyId)
    }
}
