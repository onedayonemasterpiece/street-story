package com.onedayonemasterpiece.streetstory

import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.util.UUID

/** Legacy wire name photo_sha256 carries an opaque upload identity, never a pixel digest. */
internal fun newPhotoUploadId(): String =
    UUID.randomUUID().toString().replace("-", "") + UUID.randomUUID().toString().replace("-", "")

internal fun readPhotoBytes(input: InputStream): ByteArray {
    val output = ByteArrayOutputStream()
    val buffer = ByteArray(64 * 1024)
    while (true) {
        val count = input.read(buffer)
        if (count < 0) break
        require(output.size() + count <= 16 * 1024 * 1024) { "Фото больше 16 МБ" }
        output.write(buffer, 0, count)
    }
    require(output.size() > 0) { "Пустой файл фото" }
    return output.toByteArray()
}
