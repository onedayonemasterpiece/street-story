package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Test

class SyncDraftPolicyTest {
    @Test
    fun manualDraftWinsWhenNativePublicationIsScheduledOrPublished() {
        assertEquals("manual", effectiveSnapshotDraft(StoryStage.SCHEDULED, "manual", "server"))
        assertEquals("manual", effectiveSnapshotDraft(StoryStage.PUBLISHED, "manual", "server"))
    }

    @Test
    fun remoteDraftRemainsAuthoritativeBeforeScheduling() {
        assertEquals("server", effectiveSnapshotDraft(StoryStage.REVIEW, "manual", "server"))
        assertEquals("server", effectiveSnapshotDraft(StoryStage.READY_TO_PUBLISH, "manual", "server"))
    }
}
