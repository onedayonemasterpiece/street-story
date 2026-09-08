package com.onedayonemasterpiece.streetstory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.OffsetDateTime

class IdentityPolicyTest {
    @Test fun createAndChunkKeysAreStableAndBounded(){val id="story-20260908-120000-12345678";assertEquals(newRequestKey("create",id),newRequestKey("create",id));assertTrue(newRequestKey("chunk",id.repeat(8)).length<=128)}
    @Test fun storyIdsDoNotCollide(){val now=OffsetDateTime.parse("2026-09-08T12:00:00+02:00");assertNotEquals(newClientStoryId(now),newClientStoryId(now))}
}
