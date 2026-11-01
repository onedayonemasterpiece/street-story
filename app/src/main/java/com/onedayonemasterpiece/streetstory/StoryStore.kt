package com.onedayonemasterpiece.streetstory

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import java.io.File
import java.time.OffsetDateTime

class StoryStore(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {
    private val draftOverrides = context.getSharedPreferences("street_story_draft_overrides", Context.MODE_PRIVATE)
    private val filesRoot = context.applicationContext.filesDir.canonicalFile

    init { setWriteAheadLoggingEnabled(true) }

    override fun onConfigure(db: SQLiteDatabase) {
        super.onConfigure(db)
        db.setForeignKeyConstraintsEnabled(true)
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE stories(client_story_id TEXT PRIMARY KEY,server_story_id TEXT UNIQUE,created_at INTEGER NOT NULL,photo_path TEXT NOT NULL,photo_sha256 TEXT NOT NULL,photo_mime_type TEXT NOT NULL,latitude REAL,longitude REAL,stage TEXT NOT NULL,place_name TEXT,summary TEXT,draft_text TEXT,processed_image_path TEXT,processed_image_url TEXT,processed_image_bytes_url TEXT,scheduled_for TEXT,published_at TEXT,last_error TEXT,backend_revision INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL)""")
        db.execSQL("""CREATE TABLE voice_sessions(session_id TEXT PRIMARY KEY,story_id TEXT NOT NULL,kind TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,timezone TEXT NOT NULL,device_label TEXT NOT NULL,duration_ms INTEGER NOT NULL DEFAULT 0,wall_elapsed_ms INTEGER NOT NULL DEFAULT 0,manual_pause_ms INTEGER NOT NULL DEFAULT 0,auto_silence_skipped_ms INTEGER NOT NULL DEFAULT 0,chunk_count INTEGER NOT NULL DEFAULT 0,capture_state TEXT NOT NULL,capture_activity TEXT NOT NULL,capture_policy TEXT NOT NULL,vad_engine TEXT,remote_state TEXT NOT NULL DEFAULT 'local_only',server_initialized INTEGER NOT NULL DEFAULT 0,complete_sent INTEGER NOT NULL DEFAULT 0,last_error TEXT,manual_pause_started_epoch_ms INTEGER,created_at INTEGER NOT NULL,FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE chunks(session_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,wall_start_ms INTEGER NOT NULL,wall_end_ms INTEGER NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,mime_type TEXT NOT NULL,uploaded INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,chunk_index),FOREIGN KEY(session_id) REFERENCES voice_sessions(session_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE facts(story_id TEXT NOT NULL,fact_id TEXT NOT NULL,text TEXT NOT NULL,confidence REAL NOT NULL,evidence_supported INTEGER NOT NULL,selected INTEGER NOT NULL,sources_json TEXT NOT NULL,review_status TEXT NOT NULL DEFAULT 'unreviewed',PRIMARY KEY(story_id,fact_id),FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE destinations(story_id TEXT NOT NULL,alias TEXT NOT NULL,label TEXT NOT NULL,provider TEXT NOT NULL,status TEXT NOT NULL,selected INTEGER NOT NULL,PRIMARY KEY(story_id,alias),FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,story_id TEXT NOT NULL,kind TEXT NOT NULL,request_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,state TEXT NOT NULL,last_error TEXT,created_at INTEGER NOT NULL,FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("CREATE INDEX idx_voice_sync ON voice_sessions(remote_state,capture_state,created_at)")
        db.execSQL("CREATE INDEX idx_outbox_sync ON outbox(state,created_at)")
        db.execSQL("CREATE INDEX idx_story_feed ON stories(created_at DESC)")
        db.execSQL("ALTER TABLE facts ADD COLUMN eligibility_reason TEXT")
        db.execSQL("ALTER TABLE facts ADD COLUMN strongest INTEGER NOT NULL DEFAULT 0")
        createPhonePhotoTables(db)
    }

    private fun createPhonePhotoTables(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE phone_photos(media_id INTEGER PRIMARY KEY,uri TEXT NOT NULL,modified INTEGER NOT NULL,size INTEGER NOT NULL,captured_at INTEGER NOT NULL,mime_type TEXT NOT NULL,latitude REAL,longitude REAL,gps_state TEXT NOT NULL)")
        db.execSQL("CREATE INDEX idx_phone_photos_gps ON phone_photos(gps_state,latitude,longitude)")
        db.execSQL("CREATE TABLE story_photo_choices(story_id TEXT NOT NULL,photo_id TEXT NOT NULL,uri TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(story_id,photo_id),FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE facts ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unreviewed'")
            db.execSQL("UPDATE facts SET review_status='eligible' WHERE evidence_supported=1")
            db.execSQL("ALTER TABLE stories ADD COLUMN processed_image_bytes_url TEXT")
        }
        if (oldVersion < 3) db.execSQL("ALTER TABLE facts ADD COLUMN eligibility_reason TEXT")
        if (oldVersion < 4) db.execSQL("ALTER TABLE facts ADD COLUMN strongest INTEGER NOT NULL DEFAULT 0")
        if (oldVersion < 5) {
            createPhonePhotoTables(db)
            db.execSQL("INSERT INTO story_photo_choices(story_id,photo_id,uri,position) SELECT client_story_id,'source',photo_path,0 FROM stories")
        }
    }

    @Synchronized
    fun createStory(photo: ImportedPhoto): StorySnapshot {
        val now = System.currentTimeMillis()
        writableDatabase.insertOrThrow("stories", null, ContentValues().apply {
            put("client_story_id", photo.clientStoryId)
            put("created_at", now)
            put("photo_path", photo.path)
            put("photo_sha256", photo.sha256)
            put("photo_mime_type", photo.mimeType)
            if (photo.latitude == null) putNull("latitude") else put("latitude", photo.latitude)
            if (photo.longitude == null) putNull("longitude") else put("longitude", photo.longitude)
            put("stage", StoryStage.PHOTO_READY)
            put("updated_at", now)
        })
        saveStoryPhotos(photo.clientStoryId, listOf(LocalStoryPhoto("source", photo.path, 0)))
        return requireNotNull(story(photo.clientStoryId))
    }

    @Synchronized
    fun deleteStory(id: String) {
        val story = story(id) ?: return
        val chunkPaths = readableDatabase.rawQuery(
            "SELECT c.path FROM chunks c JOIN voice_sessions v ON v.session_id=c.session_id WHERE v.story_id=?",
            arrayOf(id),
        ).use { cursor ->
            buildList {
                while (cursor.moveToNext()) add(cursor.getString(0))
            }
        }
        writableDatabase.delete("stories", "client_story_id=?", arrayOf(id))
        draftOverrides.edit().remove(id).commit()

        val ownedFiles = buildList {
            add(story.photoPath)
            story.processedImagePath?.let(::add)
            addAll(chunkPaths)
        }
        ownedFiles.forEach { raw ->
            PhotoAssets.releaseTemporary(raw)
            runCatching {
                val file = File(raw).canonicalFile
                if (file.path.startsWith(filesRoot.path + File.separator) && file.isFile) file.delete()
            }
        }
        runCatching {
            val storyDir = File(filesRoot, "stories/$id").canonicalFile
            if (storyDir.path.startsWith(filesRoot.path + File.separator) && storyDir.isDirectory) {
                storyDir.deleteRecursively()
            }
        }
    }

    @Synchronized
    fun story(id: String): StorySnapshot? = readableDatabase.query(
        "stories", STORY_COLUMNS, "client_story_id=?", arrayOf(id), null, null, null, "1",
    ).use { if (it.moveToFirst()) it.story().withDraftOverride() else null }

    @Synchronized
    fun stories(): List<StorySnapshot> = readableDatabase.query(
        "stories", STORY_COLUMNS, null, null, null, null, "created_at DESC",
    ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.story().withDraftOverride()) } }

    @Synchronized
    fun createVoiceSession(storyId: String, kind: String, deviceLabel: String): VoiceSessionSnapshot {
        require(story(storyId) != null)
        require(kind == RecordingKind.INITIAL || kind == RecordingKind.REFINEMENT || kind == RecordingKind.LIVE_ARCHIVE)
        check(activeVoiceSession() == null) { "another voice session is active" }
        val id = newVoiceSessionId()
        val now = System.currentTimeMillis()
        writableDatabase.insertOrThrow("voice_sessions", null, ContentValues().apply {
            put("session_id", id)
            put("story_id", storyId)
            put("kind", kind)
            put("started_at", OffsetDateTime.now().toString())
            put("timezone", currentTimezone())
            put("device_label", deviceLabel)
            put("capture_state", CaptureState.RECORDING)
            put("capture_activity", CaptureActivity.AUTO_SILENCE)
            put("capture_policy", CapturePolicy.VOICE_ACTIVITY_AUTO_PAUSE_V1)
            put("vad_engine", EfficientVad.ENGINE_NAME)
            put("remote_state", VoiceRemoteState.LOCAL_ONLY)
            put("created_at", now)
        })
        if (kind == RecordingKind.INITIAL) setStage(storyId, StoryStage.RECORDING)
        return requireNotNull(voiceSession(id))
    }

    @Synchronized
    fun voiceSession(id: String): VoiceSessionSnapshot? = readableDatabase.query(
        "voice_sessions", VOICE_COLUMNS, "session_id=?", arrayOf(id), null, null, null, "1",
    ).use { if (it.moveToFirst()) it.voice() else null }

    @Synchronized
    fun activeVoiceSession(): VoiceSessionSnapshot? = readableDatabase.query(
        "voice_sessions", VOICE_COLUMNS, "capture_state IN (?,?)",
        arrayOf(CaptureState.RECORDING, CaptureState.PAUSED), null, null, "created_at DESC", "1",
    ).use { if (it.moveToFirst()) it.voice() else null }

    @Synchronized
    fun latestVoiceSession(storyId: String): VoiceSessionSnapshot? = readableDatabase.query(
        "voice_sessions", VOICE_COLUMNS, "story_id=? AND capture_state!=?",
        arrayOf(storyId, CaptureState.DISCARDED), null, null, "created_at DESC", "1",
    ).use { if (it.moveToFirst()) it.voice() else null }

    @Synchronized
    fun finishedVoiceSessions(): List<VoiceSessionSnapshot> = readableDatabase.query(
        "voice_sessions", VOICE_COLUMNS, "capture_state=? AND remote_state!=? AND kind!=?",
        arrayOf(CaptureState.FINISHED, VoiceRemoteState.COMPLETE, RecordingKind.LIVE_ARCHIVE), null, null, "created_at",
    ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.voice()) } }

    @Synchronized
    fun setCaptureState(id: String, state: String, activity: String? = null) {
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("capture_state", state)
            if (activity != null) put("capture_activity", activity)
        }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun beginManualPause(id: String) {
        writableDatabase.execSQL(
            "UPDATE voice_sessions SET capture_state=?,capture_activity=?,manual_pause_started_epoch_ms=COALESCE(manual_pause_started_epoch_ms,?) WHERE session_id=?",
            arrayOf<Any?>(CaptureState.PAUSED, CaptureActivity.MANUAL_PAUSE, System.currentTimeMillis(), id),
        )
    }

    @Synchronized
    fun endManualPause(id: String) {
        val started = readableDatabase.rawQuery(
            "SELECT manual_pause_started_epoch_ms FROM voice_sessions WHERE session_id=?", arrayOf(id),
        ).use { if (it.moveToFirst() && !it.isNull(0)) it.getLong(0) else null }
        val additional = started?.let { (System.currentTimeMillis() - it).coerceAtLeast(0L) } ?: 0L
        writableDatabase.execSQL(
            "UPDATE voice_sessions SET manual_pause_ms=manual_pause_ms+?,manual_pause_started_epoch_ms=NULL,capture_state=?,capture_activity=? WHERE session_id=?",
            arrayOf<Any?>(additional, CaptureState.RECORDING, CaptureActivity.AUTO_SILENCE, id),
        )
    }

    @Synchronized
    fun updateCaptureProgress(
        id: String,
        durationMs: Long,
        wallElapsedMs: Long,
        manualPauseMs: Long,
        autoSkippedMs: Long,
        activity: String,
    ) {
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("duration_ms", durationMs.coerceAtLeast(0))
            put("wall_elapsed_ms", wallElapsedMs.coerceAtLeast(0))
            put("manual_pause_ms", manualPauseMs.coerceAtLeast(0))
            put("auto_silence_skipped_ms", autoSkippedMs.coerceAtLeast(0))
            put("capture_activity", activity)
        }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun setLocalVoiceError(id: String, message: String) {
        writableDatabase.update("voice_sessions", ContentValues().apply { put("last_error", message) }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun finishVoiceSession(id: String, endedAt: String, wallElapsedMs: Long, autoSilenceSkippedMs: Long) {
        val session = requireNotNull(voiceSession(id))
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("ended_at", endedAt)
            put("wall_elapsed_ms", wallElapsedMs)
            put("auto_silence_skipped_ms", autoSilenceSkippedMs)
            put("capture_state", CaptureState.FINISHED)
            put("capture_activity", CaptureActivity.IDLE)
            put("remote_state", VoiceRemoteState.LOCAL_ONLY)
            putNull("manual_pause_started_epoch_ms")
        }, "session_id=?", arrayOf(id))
        if (session.kind == RecordingKind.INITIAL) setStage(session.storyId, StoryStage.QUEUED)
    }

    @Synchronized
    fun discardVoiceSession(id: String) {
        chunks(id).forEach { File(it.path).delete() }
        val session = voiceSession(id)
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("capture_state", CaptureState.DISCARDED)
            put("capture_activity", CaptureActivity.IDLE)
        }, "session_id=?", arrayOf(id))
        if (session?.kind == RecordingKind.INITIAL) setStage(session.storyId, StoryStage.PHOTO_READY)
    }

    @Synchronized
    fun nextChunkIndex(id: String): Int = readableDatabase.rawQuery(
        "SELECT COALESCE(MAX(chunk_index)+1,0) FROM chunks WHERE session_id=?", arrayOf(id),
    ).use { it.moveToFirst(); it.getInt(0) }

    @Synchronized
    fun persistedDuration(id: String): Long = readableDatabase.rawQuery(
        "SELECT COALESCE(MAX(end_ms),0) FROM chunks WHERE session_id=?", arrayOf(id),
    ).use { it.moveToFirst(); it.getLong(0) }

    @Synchronized
    fun addChunk(
        sessionId: String,
        chunkIndex: Int,
        startMs: Long,
        endMs: Long,
        wallStartMs: Long,
        wallEndMs: Long,
        path: String,
        sha256: String,
        mimeType: String,
    ) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.insertOrThrow("chunks", null, ContentValues().apply {
                put("session_id", sessionId)
                put("chunk_index", chunkIndex)
                put("start_ms", startMs)
                put("end_ms", endMs)
                put("wall_start_ms", wallStartMs)
                put("wall_end_ms", wallEndMs)
                put("path", path)
                put("sha256", sha256)
                put("mime_type", mimeType)
            })
            db.execSQL(
                "UPDATE voice_sessions SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE session_id=?),duration_ms=MAX(duration_ms,?) WHERE session_id=?",
                arrayOf<Any?>(sessionId, endMs, sessionId),
            )
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun chunks(id: String): List<ChunkRecord> = readableDatabase.query(
        "chunks", CHUNK_COLUMNS, "session_id=?", arrayOf(id), null, null, "chunk_index",
    ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.chunk()) } }

    @Synchronized
    fun pendingChunks(id: String): List<ChunkRecord> = chunks(id).filter { !it.uploaded }

    @Synchronized
    fun markChunkUploaded(id: String, index: Int) {
        writableDatabase.update("chunks", ContentValues().apply { put("uploaded", 1) }, "session_id=? AND chunk_index=?", arrayOf(id, index.toString()))
    }

    @Synchronized
    fun markVoiceServerInitialized(id: String) {
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("server_initialized", 1)
            put("remote_state", VoiceRemoteState.RECEIVING)
        }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun markVoiceComplete(id: String) {
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("complete_sent", 1)
            put("remote_state", VoiceRemoteState.COMPLETE)
            putNull("last_error")
        }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun setVoiceRemoteError(id: String, state: String, error: String) {
        writableDatabase.update("voice_sessions", ContentValues().apply {
            put("remote_state", state)
            put("last_error", error)
        }, "session_id=?", arrayOf(id))
    }

    @Synchronized
    fun markInterruptedRecordingsPaused() {
        writableDatabase.execSQL(
            """
            UPDATE voice_sessions
            SET capture_state=?,
                capture_activity=?,
                manual_pause_started_epoch_ms=COALESCE(manual_pause_started_epoch_ms,?),
                duration_ms=COALESCE((SELECT MAX(end_ms) FROM chunks WHERE chunks.session_id=voice_sessions.session_id),0)
            WHERE capture_state=?
            """.trimIndent(),
            arrayOf<Any?>(CaptureState.PAUSED, CaptureActivity.MANUAL_PAUSE, System.currentTimeMillis(), CaptureState.RECORDING),
        )
    }

    @Synchronized
    fun setServerIdentity(clientId: String, serverId: String) {
        writableDatabase.update("stories", ContentValues().apply {
            put("server_story_id", serverId)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(clientId))
    }

    @Synchronized
    fun setPhotoSourcePath(id: String, path: String) {
        require(path.startsWith("content:"))
        writableDatabase.update("stories", ContentValues().apply {
            put("photo_path", path)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(id))
    }

    @Synchronized
    fun setStage(id: String, stage: String, error: String? = null) {
        writableDatabase.update("stories", ContentValues().apply {
            put("stage", stage)
            if (error == null) putNull("last_error") else put("last_error", error)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(id))
    }

    @Synchronized
    fun setServerSnapshot(
        id: String,
        stage: String,
        place: String?,
        summary: String?,
        draftText: String?,
        imageUrl: String?,
        scheduledFor: String?,
        publishedAt: String?,
        error: String?,
        revision: Int,
    ) {
        writableDatabase.update("stories", ContentValues().apply {
            put("stage", stage)
            if (place == null) putNull("place_name") else put("place_name", place)
            if (summary == null) putNull("summary") else put("summary", summary)
            if (draftText == null) putNull("draft_text") else put("draft_text", draftText)
            if (imageUrl == null) putNull("processed_image_url") else put("processed_image_url", imageUrl)
            if (scheduledFor == null) putNull("scheduled_for") else put("scheduled_for", scheduledFor)
            if (publishedAt == null) putNull("published_at") else put("published_at", publishedAt)
            if (error == null) putNull("last_error") else put("last_error", error)
            put("backend_revision", revision)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(id))
        if (stage == StoryStage.SCHEDULED || stage == StoryStage.PUBLISHED) {
            draftOverrides.edit().remove(id).commit()
        }
    }

    @Synchronized
    fun setProcessedImagePath(id: String, path: String, expectedUrl: String? = null) {
        if (expectedUrl != null && story(id)?.processedImageUrl != expectedUrl) {
            PhotoAssets.releaseTemporary(path)
            return
        }
        story(id)?.processedImagePath?.takeIf { it != path }?.let(PhotoAssets::releaseTemporary)
        writableDatabase.update("stories", ContentValues().apply {
            put("processed_image_path", path)
            put("processed_image_bytes_url", expectedUrl ?: story(id)?.processedImageUrl)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(id))
    }

    @Synchronized
    fun clearPlaceMaterial(id: String) {
        check(draftOverrides.edit().remove(id).commit())
        story(id)?.processedImagePath?.let(PhotoAssets::releaseTemporary)
        writableDatabase.update("stories", ContentValues().apply {
            putNull("draft_text"); putNull("summary")
            putNull("processed_image_path"); putNull("processed_image_url"); putNull("processed_image_bytes_url")
        }, "client_story_id=?", arrayOf(id))
    }

    @Synchronized
    fun setDraftText(id: String, text: String) {
        check(draftOverrides.edit().putString(id, text).commit()) { "local draft override was not persisted" }
        writableDatabase.update("stories", ContentValues().apply {
            put("draft_text", text)
            put("updated_at", System.currentTimeMillis())
        }, "client_story_id=?", arrayOf(id))
    }

    @Synchronized
    fun replaceFacts(storyId: String, incoming: List<FactSnapshot>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val existing = facts(storyId).associateBy { it.factId }
            // Pending checkbox edits belong to the offline owner. Once synced,
            // the backend also carries owner changes made through Mira/Live.
            val preserveLocalSelection = pendingOperations(storyId).any { it.kind == "facts" }
            db.delete("facts", "story_id=?", arrayOf(storyId))
            for (fact in incoming) {
                val preserved = if (preserveLocalSelection) existing[fact.factId]?.selected else null
                val selected = if (!fact.evidenceSupported) false else preserved ?: fact.selected
                db.insertOrThrow("facts", null, ContentValues().apply {
                    put("story_id", storyId)
                    put("fact_id", fact.factId)
                    put("text", fact.text)
                    put("confidence", fact.confidence)
                    put("evidence_supported", if (fact.evidenceSupported) 1 else 0)
                    put("selected", if (selected) 1 else 0)
                    put("sources_json", fact.sourcesJson)
                    put("review_status", fact.reviewStatus)
                    put("eligibility_reason", fact.eligibilityReason)
                    put("strongest", if (fact.strongest) 1 else 0)
                })
            }
            val selectedCount = facts(storyId).count { it.selected }
            if (existing.size != incoming.size || existing.values.count { it.selected } != selectedCount) {
                Log.i("StreetStoryFacts", "event=facts_projection story_id=$storyId source_sha=${BuildConfig.SOURCE_SHA} " +
                    "fact_count=${incoming.size} selected_count=$selectedCount preserve_pending_owner_selection=$preserveLocalSelection")
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun facts(storyId: String): List<FactSnapshot> = readableDatabase.query(
        "facts", arrayOf("fact_id", "text", "confidence", "evidence_supported", "selected", "sources_json", "review_status", "eligibility_reason", "strongest"),
        "story_id=?", arrayOf(storyId), null, null, "rowid",
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) {
                add(FactSnapshot(cursor.getString(0), cursor.getString(1), cursor.getDouble(2), cursor.getInt(3) != 0, cursor.getInt(4) != 0, cursor.getString(5), cursor.getString(6), cursor.getString(7), cursor.getInt(8) != 0))
            }
        }
    }

    @Synchronized
    fun setFactSelected(storyId: String, factId: String, selected: Boolean) {
        val supported = facts(storyId).firstOrNull { it.factId == factId }?.evidenceSupported == true
        writableDatabase.update("facts", ContentValues().apply {
            put("selected", if (selected && supported) 1 else 0)
        }, "story_id=? AND fact_id=?", arrayOf(storyId, factId))
    }

    @Synchronized
    fun replaceDestinations(storyId: String, incoming: List<DestinationSnapshot>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val existing = destinations(storyId).associateBy { it.alias }
            db.delete("destinations", "story_id=?", arrayOf(storyId))
            for (destination in incoming) {
                val selected = existing[destination.alias]?.selected ?: destination.selected
                db.insertOrThrow("destinations", null, ContentValues().apply {
                    put("story_id", storyId)
                    put("alias", destination.alias)
                    put("label", destination.label)
                    put("provider", destination.provider)
                    put("status", destination.status)
                    put("selected", if (selected) 1 else 0)
                })
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun destinations(storyId: String): List<DestinationSnapshot> = readableDatabase.query(
        "destinations", arrayOf("alias", "label", "provider", "status", "selected"),
        "story_id=?", arrayOf(storyId), null, null, "label",
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) {
                add(DestinationSnapshot(cursor.getString(0), cursor.getString(1), cursor.getString(2), cursor.getString(3), cursor.getInt(4) != 0))
            }
        }
    }

    @Synchronized
    fun setDestinationSelected(storyId: String, alias: String, selected: Boolean) {
        writableDatabase.update("destinations", ContentValues().apply {
            put("selected", if (selected) 1 else 0)
        }, "story_id=? AND alias=?", arrayOf(storyId, alias))
    }

    @Synchronized
    fun enqueueOperation(storyId: String, kind: String, requestKey: String, payloadJson: String): PendingOperation {
        val existing = readableDatabase.query(
            "outbox", OUTBOX_COLUMNS, "request_key=?", arrayOf(requestKey), null, null, null, "1",
        ).use { if (it.moveToFirst()) it.operation() else null }
        if (existing != null) return existing
        val id = newOperationId()
        writableDatabase.insertOrThrow("outbox", null, ContentValues().apply {
            put("id", id)
            put("story_id", storyId)
            put("kind", kind)
            put("request_key", requestKey)
            put("payload_json", payloadJson)
            put("state", "pending")
            put("created_at", System.currentTimeMillis())
        })
        return requireNotNull(operation(id))
    }

    @Synchronized
    fun operation(id: String): PendingOperation? = readableDatabase.query(
        "outbox", OUTBOX_COLUMNS, "id=?", arrayOf(id), null, null, null, "1",
    ).use { if (it.moveToFirst()) it.operation() else null }

    @Synchronized
    fun pendingOperations(storyId: String): List<PendingOperation> = readableDatabase.query(
        "outbox", OUTBOX_COLUMNS, "story_id=? AND state IN ('pending','retry')", arrayOf(storyId), null, null, "created_at",
    ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.operation()) } }

    @Synchronized
    fun markOperationDone(id: String) {
        writableDatabase.update("outbox", ContentValues().apply {
            put("state", "done")
            putNull("last_error")
        }, "id=?", arrayOf(id))
    }

    @Synchronized
    fun markOperationRetry(id: String, error: String) {
        writableDatabase.update("outbox", ContentValues().apply {
            put("state", "retry")
            put("last_error", error)
        }, "id=?", arrayOf(id))
    }

    /** MediaStore reconciliation: scan every id, timestamp and size, EXIF only when changed. */
    @Synchronized
    fun phonePhotoVersions(): Map<Long, PhonePhotoVersion> = readableDatabase.query(
        "phone_photos", arrayOf("media_id", "modified", "size", "gps_state"), null, null, null, null, null,
    ).use { cursor ->
        buildMap {
            while (cursor.moveToNext()) {
                put(cursor.getLong(0), PhonePhotoVersion(cursor.getLong(1), cursor.getLong(2), cursor.getString(3)))
            }
        }
    }

    @Synchronized
    fun upsertPhonePhoto(
        id: Long, modified: Long, size: Long, capturedAt: Long, mime: String,
        coordinates: DoubleArray?, gpsState: String,
    ) {
        writableDatabase.insertWithOnConflict("phone_photos", null, ContentValues().apply {
            put("media_id", id)
            put("uri", android.content.ContentUris.withAppendedId(android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id).toString())
            put("modified", modified)
            put("size", size)
            put("captured_at", capturedAt)
            put("mime_type", mime)
            if (coordinates != null) {
                put("latitude", coordinates[0]); put("longitude", coordinates[1])
            } else { putNull("latitude"); putNull("longitude") }
            put("gps_state", gpsState)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    @Synchronized
    fun removeMissingPhonePhotos(missing: Set<Long>) {
        if (missing.isEmpty()) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            missing.forEach { id -> db.delete("phone_photos", "media_id=?", arrayOf(id.toString())) }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    @Synchronized
    fun phonePhotoGpsCount(): Int = readableDatabase.rawQuery(
        "SELECT COUNT(*) FROM phone_photos WHERE gps_state='gps'", null,
    ).use { if (it.moveToFirst()) it.getInt(0) else 0 }

    /**
     * Bounding rectangles in SQLite before exact Location.distanceBetween.
     * Longitude bounds handle the date line and poles. Never LIMIT before distance.
     */
    @Synchronized
    fun photosInBoundingBox(center: PhotoPoint, radiusMeters: Double): List<PhonePhoto> {
        val b = PhonePhotoIndex.bounds(center, radiusMeters)
        val selection = StringBuilder("gps_state='gps' AND latitude BETWEEN ? AND ?")
        val args = mutableListOf(b[0].toString(), b[1].toString())
        when {
            b[3] - b[2] >= 360.0 -> Unit
            b[2] < -180.0 -> {
                selection.append(" AND (longitude >= ? OR longitude <= ?)")
                args += (b[2] + 360.0).toString(); args += b[3].toString()
            }
            b[3] > 180.0 -> {
                selection.append(" AND (longitude >= ? OR longitude <= ?)")
                args += b[2].toString(); args += (b[3] - 360.0).toString()
            }
            else -> {
                selection.append(" AND longitude BETWEEN ? AND ?")
                args += b[2].toString(); args += b[3].toString()
            }
        }
        return readableDatabase.query(
            "phone_photos", arrayOf("media_id", "uri", "captured_at", "latitude", "longitude"),
            selection.toString(), args.toTypedArray(), null, null, null,
        ).use { cursor ->
            buildList {
                while (cursor.moveToNext()) add(PhonePhoto(
                    cursor.getLong(0), cursor.getString(1), cursor.getLong(2),
                    cursor.getDouble(3), cursor.getDouble(4),
                ))
            }
        }
    }

    @Synchronized
    fun selectedStoryPhotos(storyId: String): List<LocalStoryPhoto> = readableDatabase.query(
        "story_photo_choices", arrayOf("photo_id", "uri", "position"), "story_id=?",
        arrayOf(storyId), null, null, "position",
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) add(LocalStoryPhoto(cursor.getString(0), cursor.getString(1), cursor.getInt(2)))
        }
    }

    /** Every owner toggle persists the selection, including order. */
    @Synchronized
    fun saveStoryPhotos(storyId: String, photos: List<LocalStoryPhoto>) {
        require(photos.size <= 8 && photos.map { it.photoId }.distinct().size == photos.size)
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete("story_photo_choices", "story_id=?", arrayOf(storyId))
            photos.forEachIndexed { index, photo ->
                db.insertOrThrow("story_photo_choices", null, ContentValues().apply {
                    put("story_id", storyId); put("photo_id", photo.photoId)
                    put("uri", photo.uri); put("position", index)
                })
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    /** Queue uploads before order, retaining a stable UUID and request key. */
    @Synchronized
    fun enqueueStoryPhotoOrder(storyId: String, photos: List<LocalStoryPhoto>) {
        require(photos.size in 1..8)
        val gson = com.google.gson.Gson()
        pendingOperations(storyId).filter { it.kind == "photo-order" }.forEach { markOperationDone(it.id) }
        photos.filter { it.photoId != "source" }.forEach { photo ->
            enqueueOperation(
                storyId, "photo-upload", newRequestKey("photo", storyId + "-" + photo.photoId),
                gson.toJson(mapOf("photo_id" to photo.photoId, "uri" to photo.uri)),
            )
        }
        enqueueOperation(
            storyId, "photo-order", newRequestKey("order", storyId + "-" + java.util.UUID.randomUUID()),
            gson.toJson(mapOf("photo_ids" to photos.map { it.photoId })),
        )
    }

    private fun StorySnapshot.withDraftOverride(): StorySnapshot =
        draftOverrides.getString(clientStoryId, null)?.let { copy(draftText = it) } ?: this

    private fun Cursor.story() = StorySnapshot(
        getString(0), stringOrNull(1), getLong(2), getString(3), getString(4), getString(5),
        doubleOrNull(6), doubleOrNull(7), getString(8), stringOrNull(9), stringOrNull(10), stringOrNull(11),
        stringOrNull(12), stringOrNull(13), stringOrNull(14), stringOrNull(15), stringOrNull(16), getInt(17), stringOrNull(18),
    )

    private fun Cursor.voice() = VoiceSessionSnapshot(
        getString(0), getString(1), getString(2), getString(3), stringOrNull(4), getString(5), getString(6),
        getLong(7), getLong(8), getLong(9), getLong(10), getInt(11), getString(12), getString(13), getString(14),
        stringOrNull(15), getString(16), getInt(17) != 0, getInt(18) != 0, stringOrNull(19), getLong(20),
    )

    private fun Cursor.chunk() = ChunkRecord(
        getString(0), getInt(1), getLong(2), getLong(3), getLong(4), getLong(5), getString(6), getString(7), getString(8), getInt(9) != 0,
    )

    private fun Cursor.operation() = PendingOperation(
        getString(0), getString(1), getString(2), getString(3), getString(4), getString(5), stringOrNull(6),
    )

    private fun Cursor.stringOrNull(index: Int) = if (isNull(index)) null else getString(index)
    private fun Cursor.doubleOrNull(index: Int) = if (isNull(index)) null else getDouble(index)

    companion object {
        private const val DB_NAME = "street-story.db"
        private const val DB_VERSION = 5
        private val STORY_COLUMNS = arrayOf(
            "client_story_id", "server_story_id", "created_at", "photo_path", "photo_sha256", "photo_mime_type",
            "latitude", "longitude", "stage", "place_name", "summary", "draft_text", "processed_image_path",
            "processed_image_url", "scheduled_for", "published_at", "last_error", "backend_revision", "processed_image_bytes_url",
        )
        private val VOICE_COLUMNS = arrayOf(
            "session_id", "story_id", "kind", "started_at", "ended_at", "timezone", "device_label", "duration_ms",
            "wall_elapsed_ms", "manual_pause_ms", "auto_silence_skipped_ms", "chunk_count", "capture_state",
            "capture_activity", "capture_policy", "vad_engine", "remote_state", "server_initialized", "complete_sent",
            "last_error", "created_at",
        )
        private val CHUNK_COLUMNS = arrayOf(
            "session_id", "chunk_index", "start_ms", "end_ms", "wall_start_ms", "wall_end_ms", "path", "sha256",
            "mime_type", "uploaded",
        )
        private val OUTBOX_COLUMNS = arrayOf("id", "story_id", "kind", "request_key", "payload_json", "state", "last_error")
    }
}
