package com.onedayonemasterpiece.streetstory

import java.io.ByteArrayInputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PhotoUploadIdentityTest {
    @Test fun `same raw input gets independent opaque upload identities`() {
        val first = newPhotoUploadId()
        val second = newPhotoUploadId()
        assertTrue(first.matches(Regex("[0-9a-f]{64}")))
        assertTrue(second.matches(Regex("[0-9a-f]{64}")))
        assertNotEquals(first, second)
    }

    @Test fun `raw reader preserves complete binary source without transformations`() {
        val bytes = ByteArray(130_117) { (it % 251).toByte() }
        assertArrayEquals(bytes, readPhotoBytes(ByteArrayInputStream(bytes)))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `empty source is rejected before upload`() {
        readPhotoBytes(ByteArrayInputStream(byteArrayOf()))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `oversized source is rejected without an image file`() {
        readPhotoBytes(ByteArrayInputStream(ByteArray(16 * 1024 * 1024 + 1)))
    }
}
