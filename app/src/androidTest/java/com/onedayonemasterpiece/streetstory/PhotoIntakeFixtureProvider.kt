package com.onedayonemasterpiece.streetstory

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor

/** Synthetic test-only gallery: temporary reads through a pipe, no persisted image. */
class PhotoIntakeFixtureProvider : ContentProvider() {
    override fun onCreate() = true
    override fun getType(uri: Uri) = "image/jpeg"
    override fun query(uri: Uri, projection: Array<out String>?, selection: String?, selectionArgs: Array<out String>?, sortOrder: String?): Cursor? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = throw UnsupportedOperationException()
    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?) = throw UnsupportedOperationException()
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?) = throw UnsupportedOperationException()
    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        require(mode == "r")
        val pipe = ParcelFileDescriptor.createPipe()
        Thread {
            ParcelFileDescriptor.AutoCloseOutputStream(pipe[1]).use { it.write(PhotoGpsFixture.bytes()) }
        }.start()
        return pipe[0]
    }
}
