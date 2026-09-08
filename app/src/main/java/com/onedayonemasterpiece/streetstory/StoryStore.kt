package com.onedayonemasterpiece.streetstory

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.io.File
import java.time.OffsetDateTime

class StoryStore(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {
    init { setWriteAheadLoggingEnabled(true) }
    override fun onConfigure(db: SQLiteDatabase) { super.onConfigure(db); db.setForeignKeyConstraintsEnabled(true) }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE stories(client_story_id TEXT PRIMARY KEY,server_story_id TEXT UNIQUE,created_at INTEGER NOT NULL,photo_path TEXT NOT NULL,photo_sha256 TEXT NOT NULL,photo_mime_type TEXT NOT NULL,latitude REAL,longitude REAL,stage TEXT NOT NULL,place_name TEXT,summary TEXT,draft_text TEXT,processed_image_path TEXT,processed_image_url TEXT,scheduled_for TEXT,published_at TEXT,last_error TEXT,backend_revision INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL)""")
        db.execSQL("""CREATE TABLE voice_sessions(session_id TEXT PRIMARY KEY,story_id TEXT NOT NULL,kind TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,timezone TEXT NOT NULL,device_label TEXT NOT NULL,duration_ms INTEGER NOT NULL DEFAULT 0,wall_elapsed_ms INTEGER NOT NULL DEFAULT 0,manual_pause_ms INTEGER NOT NULL DEFAULT 0,auto_silence_skipped_ms INTEGER NOT NULL DEFAULT 0,chunk_count INTEGER NOT NULL DEFAULT 0,capture_state TEXT NOT NULL,capture_activity TEXT NOT NULL,capture_policy TEXT NOT NULL,vad_engine TEXT,remote_state TEXT NOT NULL DEFAULT 'local_only',server_initialized INTEGER NOT NULL DEFAULT 0,complete_sent INTEGER NOT NULL DEFAULT 0,last_error TEXT,manual_pause_started_epoch_ms INTEGER,created_at INTEGER NOT NULL,FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE chunks(session_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,wall_start_ms INTEGER NOT NULL,wall_end_ms INTEGER NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,mime_type TEXT NOT NULL,uploaded INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,chunk_index),FOREIGN KEY(session_id) REFERENCES voice_sessions(session_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE facts(story_id TEXT NOT NULL,fact_id TEXT NOT NULL,text TEXT NOT NULL,confidence REAL NOT NULL,evidence_supported INTEGER NOT NULL,selected INTEGER NOT NULL,sources_json TEXT NOT NULL,PRIMARY KEY(story_id,fact_id),FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE destinations(story_id TEXT NOT NULL,alias TEXT NOT NULL,label TEXT NOT NULL,provider TEXT NOT NULL,status TEXT NOT NULL,selected INTEGER NOT NULL,PRIMARY KEY(story_id,alias),FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,story_id TEXT NOT NULL,kind TEXT NOT NULL,request_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,state TEXT NOT NULL,last_error TEXT,created_at INTEGER NOT NULL,FOREIGN KEY(story_id) REFERENCES stories(client_story_id) ON DELETE CASCADE)""")
        db.execSQL("CREATE INDEX idx_voice_sync ON voice_sessions(remote_state,capture_state,created_at)")
        db.execSQL("CREATE INDEX idx_outbox_sync ON outbox(state,created_at)")
        db.execSQL("CREATE INDEX idx_story_feed ON stories(created_at DESC)")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) { error("No migration required before v1 release") }

    @Synchronized fun createStory(photo: ImportedPhoto): StorySnapshot {
        val now = System.currentTimeMillis()
        writableDatabase.insertOrThrow("stories", null, ContentValues().apply {
            put("client_story_id", photo.clientStoryId); put("created_at", now); put("photo_path", photo.path); put("photo_sha256", photo.sha256); put("photo_mime_type", photo.mimeType)
            if (photo.latitude == null) putNull("latitude") else put("latitude", photo.latitude); if (photo.longitude == null) putNull("longitude") else put("longitude", photo.longitude)
            put("stage", StoryStage.PHOTO_READY); put("updated_at", now)
        })
        return requireNotNull(story(photo.clientStoryId))
    }

    @Synchronized fun story(id: String): StorySnapshot? = readableDatabase.query("stories", STORY_COLUMNS, "client_story_id=?", arrayOf(id), null, null, null, "1").use { if (it.moveToFirst()) it.story() else null }
    @Synchronized fun stories(): List<StorySnapshot> = readableDatabase.query("stories", STORY_COLUMNS, null, null, null, null, "created_at DESC").use { c -> buildList { while (c.moveToNext()) add(c.story()) } }

    @Synchronized fun createVoiceSession(storyId: String, kind: String, deviceLabel: String): VoiceSessionSnapshot {
        require(story(storyId) != null); require(kind == RecordingKind.INITIAL || kind == RecordingKind.REFINEMENT)
        check(activeVoiceSession() == null) { "another voice session is active" }
        val id = newVoiceSessionId(); val now = System.currentTimeMillis()
        writableDatabase.insertOrThrow("voice_sessions", null, ContentValues().apply {
            put("session_id", id); put("story_id", storyId); put("kind", kind); put("started_at", OffsetDateTime.now().toString()); put("timezone", currentTimezone()); put("device_label", deviceLabel)
            put("capture_state", CaptureState.RECORDING); put("capture_activity", CaptureActivity.AUTO_SILENCE); put("capture_policy", CapturePolicy.VOICE_ACTIVITY_AUTO_PAUSE_V1); put("vad_engine", EfficientVad.ENGINE_NAME)
            put("remote_state", VoiceRemoteState.LOCAL_ONLY); put("created_at", now)
        })
        if (kind == RecordingKind.INITIAL) setStage(storyId, StoryStage.RECORDING)
        return requireNotNull(voiceSession(id))
    }

    @Synchronized fun voiceSession(id: String): VoiceSessionSnapshot? = readableDatabase.query("voice_sessions", VOICE_COLUMNS, "session_id=?", arrayOf(id), null, null, null, "1").use { if (it.moveToFirst()) it.voice() else null }
    @Synchronized fun activeVoiceSession(): VoiceSessionSnapshot? = readableDatabase.query("voice_sessions", VOICE_COLUMNS, "capture_state IN (?,?)", arrayOf(CaptureState.RECORDING, CaptureState.PAUSED), null, null, "created_at DESC", "1").use { if (it.moveToFirst()) it.voice() else null }
    @Synchronized fun latestVoiceSession(storyId: String): VoiceSessionSnapshot? = readableDatabase.query("voice_sessions", VOICE_COLUMNS, "story_id=? AND capture_state!=?", arrayOf(storyId, CaptureState.DISCARDED), null, null, "created_at DESC", "1").use { if (it.moveToFirst()) it.voice() else null }
    @Synchronized fun finishedVoiceSessions(): List<VoiceSessionSnapshot> = readableDatabase.query("voice_sessions", VOICE_COLUMNS, "capture_state=? AND remote_state!=?", arrayOf(CaptureState.FINISHED, VoiceRemoteState.COMPLETE), null, null, "created_at").use { c -> buildList { while (c.moveToNext()) add(c.voice()) } }

    @Synchronized fun setCaptureState(id: String, state: String, activity: String? = null) { writableDatabase.update("voice_sessions", ContentValues().apply { put("capture_state", state); if (activity != null) put("capture_activity", activity) }, "session_id=?", arrayOf(id)) }
    @Synchronized fun beginManualPause(id: String) { writableDatabase.execSQL("UPDATE voice_sessions SET capture_state=?,capture_activity=?,manual_pause_started_epoch_ms=COALESCE(manual_pause_started_epoch_ms,?) WHERE session_id=?", arrayOf<Any?>(CaptureState.PAUSED, CaptureActivity.MANUAL_PAUSE, System.currentTimeMillis(), id)) }
    @Synchronized fun endManualPause(id: String) {
        val started = readableDatabase.rawQuery("SELECT manual_pause_started_epoch_ms FROM voice_sessions WHERE session_id=?", arrayOf(id)).use { if (it.moveToFirst() && !it.isNull(0)) it.getLong(0) else null }
        val additional = started?.let { (System.currentTimeMillis() - it).coerceAtLeast(0L) } ?: 0L
        writableDatabase.execSQL("UPDATE voice_sessions SET manual_pause_ms=manual_pause_ms+?,manual_pause_started_epoch_ms=NULL,capture_state=?,capture_activity=? WHERE session_id=?", arrayOf<Any?>(additional, CaptureState.RECORDING, CaptureActivity.AUTO_SILENCE, id))
    }
    @Synchronized fun updateCaptureProgress(id: String, durationMs: Long, wallElapsedMs: Long, manualPauseMs: Long, autoSkippedMs: Long, activity: String) { writableDatabase.update("voice_sessions", ContentValues().apply { put("duration_ms", durationMs.coerceAtLeast(0)); put("wall_elapsed_ms", wallElapsedMs.coerceAtLeast(0)); put("manual_pause_ms", manualPauseMs.coerceAtLeast(0)); put("auto_silence_skipped_ms", autoSkippedMs.coerceAtLeast(0)); put("capture_activity", activity) }, "session_id=?", arrayOf(id)) }
    @Synchronized fun setLocalVoiceError(id: String, message: String) { writableDatabase.update("voice_sessions", ContentValues().apply { put("last_error", message) }, "session_id=?", arrayOf(id)) }

    @Synchronized fun finishVoiceSession(id: String, endedAt: String, wallElapsedMs: Long, autoSilenceSkippedMs: Long) {
        val session = requireNotNull(voiceSession(id))
        writableDatabase.update("voice_sessions", ContentValues().apply { put("ended_at", endedAt); put("wall_elapsed_ms", wallElapsedMs); put("auto_silence_skipped_ms", autoSilenceSkippedMs); put("capture_state", CaptureState.FINISHED); put("capture_activity", CaptureActivity.IDLE); put("remote_state", VoiceRemoteState.LOCAL_ONLY); putNull("manual_pause_started_epoch_ms") }, "session_id=?", arrayOf(id))
        if (session.kind == RecordingKind.INITIAL) setStage(session.storyId, StoryStage.QUEUED)
    }

    @Synchronized fun discardVoiceSession(id: String) {
        chunks(id).forEach { File(it.path).delete() }
        val session = voiceSession(id)
        writableDatabase.update("voice_sessions", ContentValues().apply { put("capture_state", CaptureState.DISCARDED); put("capture_activity", CaptureActivity.IDLE) }, "session_id=?", arrayOf(id))
        if (session?.kind == RecordingKind.INITIAL) setStage(session.storyId, StoryStage.PHOTO_READY)
    }

    @Synchronized fun nextChunkIndex(id: String): Int = readableDatabase.rawQuery("SELECT COALESCE(MAX(chunk_index)+1,0) FROM chunks WHERE session_id=?", arrayOf(id)).use { it.moveToFirst(); it.getInt(0) }
    @Synchronized fun addChunk(sessionId: String, chunkIndex: Int, startMs: Long, endMs: Long, wallStartMs: Long, wallEndMs: Long, path: String, sha256: String, mimeType: String) {
        val db = writableDatabase; db.beginTransaction(); try {
            db.insertOrThrow("chunks", null, ContentValues().apply { put("session_id", sessionId); put("chunk_index", chunkIndex); put("start_ms", startMs); put("end_ms", endMs); put("wall_start_ms", wallStartMs); put("wall_end_ms", wallEndMs); put("path", path); put("sha256", sha256); put("mime_type", mimeType) })
            db.execSQL("UPDATE voice_sessions SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE session_id=?),duration_ms=MAX(duration_ms,?) WHERE session_id=?", arrayOf<Any?>(sessionId, endMs, sessionId))
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }
    @Synchronized fun chunks(id: String): List<ChunkRecord> = readableDatabase.query("chunks", CHUNK_COLUMNS, "session_id=?", arrayOf(id), null, null, "chunk_index").use { c -> buildList { while (c.moveToNext()) add(c.chunk()) } }
    @Synchronized fun pendingChunks(id: String): List<ChunkRecord> = chunks(id).filter { !it.uploaded }
    @Synchronized fun markChunkUploaded(id: String, index: Int) { writableDatabase.update("chunks", ContentValues().apply { put("uploaded", 1) }, "session_id=? AND chunk_index=?", arrayOf(id, index.toString())) }
    @Synchronized fun markVoiceServerInitialized(id: String) { writableDatabase.update("voice_sessions", ContentValues().apply { put("server_initialized", 1); put("remote_state", VoiceRemoteState.RECEIVING) }, "session_id=?", arrayOf(id)) }
    @Synchronized fun markVoiceComplete(id: String) { writableDatabase.update("voice_sessions", ContentValues().apply { put("complete_sent", 1); put("remote_state", VoiceRemoteState.COMPLETE); putNull("last_error") }, "session_id=?", arrayOf(id)) }
    @Synchronized fun setVoiceRemoteError(id: String, state: String, error: String) { writableDatabase.update("voice_sessions", ContentValues().apply { put("remote_state", state); put("last_error", error) }, "session_id=?", arrayOf(id)) }

    @Synchronized fun markInterruptedRecordingsPaused() { writableDatabase.execSQL("UPDATE voice_sessions SET capture_state=?,capture_activity=?,manual_pause_started_epoch_ms=COALESCE(manual_pause_started_epoch_ms,?) WHERE capture_state=?", arrayOf<Any?>(CaptureState.PAUSED, CaptureActivity.MANUAL_PAUSE, System.currentTimeMillis(), CaptureState.RECORDING)) }

    @Synchronized fun setServerIdentity(clientId: String, serverId: String) { writableDatabase.update("stories", ContentValues().apply { put("server_story_id", serverId); put("updated_at", System.currentTimeMillis()) }, "client_story_id=?", arrayOf(clientId)) }
    @Synchronized fun setStage(id: String, stage: String, error: String? = null) { writableDatabase.update("stories", ContentValues().apply { put("stage", stage); if (error == null) putNull("last_error") else put("last_error", error); put("updated_at", System.currentTimeMillis()) }, "client_story_id=?", arrayOf(id)) }
    @Synchronized fun setServerSnapshot(id: String, stage: String, place: String?, summary: String?, draftText: String?, imageUrl: String?, scheduledFor: String?, publishedAt: String?, error: String?, revision: Int) { writableDatabase.update("stories", ContentValues().apply { put("stage", stage); if (place == null) putNull("place_name") else put("place_name", place); if (summary == null) putNull("summary") else put("summary", summary); if (draftText == null) putNull("draft_text") else put("draft_text", draftText); if (imageUrl == null) putNull("processed_image_url") else put("processed_image_url", imageUrl); if (scheduledFor == null) putNull("scheduled_for") else put("scheduled_for", scheduledFor); if (publishedAt == null) putNull("published_at") else put("published_at", publishedAt); if (error == null) putNull("last_error") else put("last_error", error); put("backend_revision", revision); put("updated_at", System.currentTimeMillis()) }, "client_story_id=?", arrayOf(id)) }
    @Synchronized fun setProcessedImagePath(id: String, path: String) { writableDatabase.update("stories", ContentValues().apply { put("processed_image_path", path); put("updated_at", System.currentTimeMillis()) }, "client_story_id=?", arrayOf(id)) }
    @Synchronized fun setDraftText(id: String, text: String) { writableDatabase.update("stories", ContentValues().apply { put("draft_text", text); put("updated_at", System.currentTimeMillis()) }, "client_story_id=?", arrayOf(id)) }

    @Synchronized fun replaceFacts(storyId: String, incoming: List<FactSnapshot>) {
        val db = writableDatabase; db.beginTransaction(); try {
            val existing = facts(storyId).associateBy { it.factId }
            db.delete("facts", "story_id=?", arrayOf(storyId))
            for (fact in incoming) {
                val preserved = existing[fact.factId]?.selected
                val selected = if (!fact.evidenceSupported) false else preserved ?: fact.selected
                db.insertOrThrow("facts", null, ContentValues().apply { put("story_id", storyId); put("fact_id", fact.factId); put("text", fact.text); put("confidence", fact.confidence); put("evidence_supported", if (fact.evidenceSupported) 1 else 0); put("selected", if (selected) 1 else 0); put("sources_json", fact.sourcesJson) })
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }
    @Synchronized fun facts(storyId: String): List<FactSnapshot> = readableDatabase.query("facts", arrayOf("fact_id","text","confidence","evidence_supported","selected","sources_json"), "story_id=?", arrayOf(storyId), null, null, "rowid").use { c -> buildList { while (c.moveToNext()) add(FactSnapshot(c.getString(0), c.getString(1), c.getDouble(2), c.getInt(3)!=0, c.getInt(4)!=0, c.getString(5))) } }
    @Synchronized fun setFactSelected(storyId: String, factId: String, selected: Boolean) { val supported = facts(storyId).firstOrNull { it.factId == factId }?.evidenceSupported == true; writableDatabase.update("facts", ContentValues().apply { put("selected", if (selected && supported) 1 else 0) }, "story_id=? AND fact_id=?", arrayOf(storyId, factId)) }

    @Synchronized fun replaceDestinations(storyId: String, incoming: List<DestinationSnapshot>) {
        val db=writableDatabase; db.beginTransaction(); try { val existing=destinations(storyId).associateBy{it.alias}; db.delete("destinations","story_id=?",arrayOf(storyId)); for(d in incoming){ val selected=existing[d.alias]?.selected ?: d.selected; db.insertOrThrow("destinations",null,ContentValues().apply{put("story_id",storyId);put("alias",d.alias);put("label",d.label);put("provider",d.provider);put("status",d.status);put("selected",if(selected)1 else 0)})}; db.setTransactionSuccessful() } finally { db.endTransaction() }
    }
    @Synchronized fun destinations(storyId: String): List<DestinationSnapshot> = readableDatabase.query("destinations", arrayOf("alias","label","provider","status","selected"), "story_id=?", arrayOf(storyId), null, null, "label").use { c -> buildList { while(c.moveToNext()) add(DestinationSnapshot(c.getString(0),c.getString(1),c.getString(2),c.getString(3),c.getInt(4)!=0)) } }
    @Synchronized fun setDestinationSelected(storyId:String,alias:String,selected:Boolean){writableDatabase.update("destinations",ContentValues().apply{put("selected",if(selected)1 else 0)},"story_id=? AND alias=?",arrayOf(storyId,alias))}

    @Synchronized fun enqueueOperation(storyId:String,kind:String,requestKey:String,payloadJson:String): PendingOperation {
        val existing=readableDatabase.query("outbox",OUTBOX_COLUMNS,"request_key=?",arrayOf(requestKey),null,null,null,"1").use{if(it.moveToFirst())it.operation() else null}; if(existing!=null)return existing
        val id=newOperationId(); writableDatabase.insertOrThrow("outbox",null,ContentValues().apply{put("id",id);put("story_id",storyId);put("kind",kind);put("request_key",requestKey);put("payload_json",payloadJson);put("state","pending");put("created_at",System.currentTimeMillis())}); return requireNotNull(operation(id))
    }
    @Synchronized fun operation(id:String):PendingOperation?=readableDatabase.query("outbox",OUTBOX_COLUMNS,"id=?",arrayOf(id),null,null,null,"1").use{if(it.moveToFirst())it.operation() else null}
    @Synchronized fun pendingOperations(storyId:String):List<PendingOperation> = readableDatabase.query("outbox",OUTBOX_COLUMNS,"story_id=? AND state IN ('pending','retry')",arrayOf(storyId),null,null,"created_at").use{c->buildList{while(c.moveToNext())add(c.operation())}}
    @Synchronized fun markOperationDone(id:String){writableDatabase.update("outbox",ContentValues().apply{put("state","done");putNull("last_error")},"id=?",arrayOf(id))}
    @Synchronized fun markOperationRetry(id:String,error:String){writableDatabase.update("outbox",ContentValues().apply{put("state","retry");put("last_error",error)},"id=?",arrayOf(id))}

    private fun Cursor.story()=StorySnapshot(getString(0),stringOrNull(1),getLong(2),getString(3),getString(4),getString(5),doubleOrNull(6),doubleOrNull(7),getString(8),stringOrNull(9),stringOrNull(10),stringOrNull(11),stringOrNull(12),stringOrNull(13),stringOrNull(14),stringOrNull(15),stringOrNull(16),getInt(17))
    private fun Cursor.voice()=VoiceSessionSnapshot(getString(0),getString(1),getString(2),getString(3),stringOrNull(4),getString(5),getString(6),getLong(7),getLong(8),getLong(9),getLong(10),getInt(11),getString(12),getString(13),getString(14),stringOrNull(15),getString(16),getInt(17)!=0,getInt(18)!=0,stringOrNull(19),getLong(20))
    private fun Cursor.chunk()=ChunkRecord(getString(0),getInt(1),getLong(2),getLong(3),getLong(4),getLong(5),getString(6),getString(7),getString(8),getInt(9)!=0)
    private fun Cursor.operation()=PendingOperation(getString(0),getString(1),getString(2),getString(3),getString(4),getString(5),stringOrNull(6))
    private fun Cursor.stringOrNull(i:Int)=if(isNull(i))null else getString(i)
    private fun Cursor.doubleOrNull(i:Int)=if(isNull(i))null else getDouble(i)

    companion object {
        private const val DB_NAME="street-story.db"; private const val DB_VERSION=1
        private val STORY_COLUMNS=arrayOf("client_story_id","server_story_id","created_at","photo_path","photo_sha256","photo_mime_type","latitude","longitude","stage","place_name","summary","draft_text","processed_image_path","processed_image_url","scheduled_for","published_at","last_error","backend_revision")
        private val VOICE_COLUMNS=arrayOf("session_id","story_id","kind","started_at","ended_at","timezone","device_label","duration_ms","wall_elapsed_ms","manual_pause_ms","auto_silence_skipped_ms","chunk_count","capture_state","capture_activity","capture_policy","vad_engine","remote_state","server_initialized","complete_sent","last_error","created_at")
        private val CHUNK_COLUMNS=arrayOf("session_id","chunk_index","start_ms","end_ms","wall_start_ms","wall_end_ms","path","sha256","mime_type","uploaded")
        private val OUTBOX_COLUMNS=arrayOf("id","story_id","kind","request_key","payload_json","state","last_error")
    }
}
