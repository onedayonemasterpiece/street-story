package com.onedayonemasterpiece.streetstory

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

data class VoiceMessageSnapshot(
    val storyId: String,
    val sessionId: String,
    val kind: String,
    val rawTranscript: String?,
    val displayText: String?,
    val startedAt: String?,
    val endedAt: String?,
)

data class LiveMessageSnapshot(
    val storyId: String,
    val messageId: String,
    val sessionId: String,
    val role: String,
    val text: String,
    val final: Boolean,
)

object FeedModel {
    const val STORY_LIMIT = 10
    fun <T> latest(items: List<T>): List<T> = items.take(STORY_LIMIT)
}

class FeedProjectionStore(context: Context) : SQLiteOpenHelper(context.applicationContext, DB_NAME, null, DB_VERSION) {
    init { setWriteAheadLoggingEnabled(true) }

    override fun onConfigure(db: SQLiteDatabase) {
        super.onConfigure(db)
        db.setForeignKeyConstraintsEnabled(false)
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE voice_messages(
                story_id TEXT NOT NULL,
                session_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                raw_transcript TEXT,
                display_text TEXT,
                started_at TEXT,
                ended_at TEXT,
                updated_at INTEGER NOT NULL
            )""".trimIndent(),
        )
        db.execSQL("CREATE INDEX idx_feed_voice_story ON voice_messages(story_id,started_at,session_id)")
        createLiveMessagesTable(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) createLiveMessagesTable(db)
    }

    private fun createLiveMessagesTable(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE IF NOT EXISTS live_messages(
                story_id TEXT NOT NULL,
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                final INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )""".trimIndent(),
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_feed_live_story ON live_messages(story_id,position,message_id)")
    }

    @Synchronized
    fun replaceVoiceMessages(storyId: String, incoming: List<VoiceMessageWire>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val ids = incoming.map { it.sessionId }.filter { it.isNotBlank() }.toSet()
            if (ids.isEmpty()) {
                db.delete("voice_messages", "story_id=?", arrayOf(storyId))
            } else {
                val placeholders = ids.joinToString(",") { "?" }
                db.delete(
                    "voice_messages",
                    "story_id=? AND session_id NOT IN ($placeholders)",
                    arrayOf(storyId, *ids.toTypedArray()),
                )
            }
            val now = System.currentTimeMillis()
            incoming.filter { it.sessionId.isNotBlank() }.forEach { voice ->
                db.insertWithOnConflict(
                    "voice_messages",
                    null,
                    ContentValues().apply {
                        put("story_id", storyId)
                        put("session_id", voice.sessionId)
                        put("kind", voice.kind.ifBlank { RecordingKind.INITIAL })
                        if (voice.rawTranscript == null) putNull("raw_transcript") else put("raw_transcript", voice.rawTranscript)
                        if (voice.displayText == null) putNull("display_text") else put("display_text", voice.displayText)
                        if (voice.startedAt == null) putNull("started_at") else put("started_at", voice.startedAt)
                        if (voice.endedAt == null) putNull("ended_at") else put("ended_at", voice.endedAt)
                        put("updated_at", now)
                    },
                    SQLiteDatabase.CONFLICT_REPLACE,
                )
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun replaceLiveMessages(storyId: String, incoming: List<LiveMessageWire>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete("live_messages", "story_id=?", arrayOf(storyId))
            val now = System.currentTimeMillis()
            incoming.filter { it.messageId.isNotBlank() && it.role in setOf(LiveRole.USER, LiveRole.ASSISTANT) && it.text.isNotBlank() }
                .takeLast(100)
                .forEachIndexed { index, item ->
                    db.insertWithOnConflict(
                        "live_messages", null,
                        ContentValues().apply {
                            put("story_id", storyId); put("message_id", item.messageId)
                            put("session_id", item.sessionId); put("role", item.role)
                            put("text", item.text.take(8000)); put("final", if (item.final) 1 else 0)
                            put("position", index); put("updated_at", now)
                        },
                        SQLiteDatabase.CONFLICT_REPLACE,
                    )
                }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    @Synchronized
    fun replaceLocalLiveMessages(storyId: String, incoming: List<LiveChatMessage>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete("live_messages", "story_id=?", arrayOf(storyId))
            val now = System.currentTimeMillis()
            incoming.filter { it.role in setOf(LiveRole.USER, LiveRole.ASSISTANT) && it.text.isNotBlank() }
                .takeLast(100)
                .forEachIndexed { index, item ->
                    db.insertWithOnConflict(
                        "live_messages", null,
                        ContentValues().apply {
                            put("story_id", storyId); put("message_id", "local:" + index)
                            put("session_id", "local"); put("role", item.role); put("text", item.text.take(8000))
                            put("final", 1); put("position", index); put("updated_at", now)
                        },
                        SQLiteDatabase.CONFLICT_REPLACE,
                    )
                }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    @Synchronized
    fun liveMessages(storyId: String): List<LiveMessageSnapshot> = readableDatabase.query(
        "live_messages",
        arrayOf("story_id","message_id","session_id","role","text","final"),
        "story_id=?", arrayOf(storyId), null, null, "position,message_id",
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) {
                add(LiveMessageSnapshot(
                    cursor.getString(0), cursor.getString(1), cursor.getString(2),
                    cursor.getString(3), cursor.getString(4), cursor.getInt(5) != 0,
                ))
            }
        }
    }

    @Synchronized
    fun clear(storyId: String) {
        writableDatabase.delete("voice_messages", "story_id=?", arrayOf(storyId))
        writableDatabase.delete("live_messages", "story_id=?", arrayOf(storyId))
    }

    @Synchronized
    fun voiceMessages(storyId: String): List<VoiceMessageSnapshot> = readableDatabase.query(
        "voice_messages",
        arrayOf("story_id", "session_id", "kind", "raw_transcript", "display_text", "started_at", "ended_at"),
        "story_id=?",
        arrayOf(storyId),
        null,
        null,
        "COALESCE(started_at,''),session_id",
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) {
                add(
                    VoiceMessageSnapshot(
                        cursor.getString(0), cursor.getString(1), cursor.getString(2),
                        if (cursor.isNull(3)) null else cursor.getString(3),
                        if (cursor.isNull(4)) null else cursor.getString(4),
                        if (cursor.isNull(5)) null else cursor.getString(5),
                        if (cursor.isNull(6)) null else cursor.getString(6),
                    ),
                )
            }
        }
    }

    companion object {
        private const val DB_NAME = "street-story-feed.db"
        private const val DB_VERSION = 2
    }
}
