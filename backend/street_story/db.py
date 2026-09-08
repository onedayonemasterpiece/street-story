from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS stories(
  id TEXT PRIMARY KEY,
  client_story_id TEXT NOT NULL UNIQUE,
  photo_sha256 TEXT NOT NULL,
  photo_mime_type TEXT NOT NULL,
  photo_path TEXT NOT NULL,
  latitude REAL,
  longitude REAL,
  voice_protocol TEXT NOT NULL,
  state TEXT NOT NULL,
  place_name TEXT,
  summary TEXT,
  draft_text TEXT,
  processed_image_path TEXT,
  processed_image_url TEXT,
  vibepublish_asset_ref TEXT,
  scheduled_for TEXT,
  published_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  error_message TEXT,
  research_json TEXT NOT NULL DEFAULT '{}',
  visual_context_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_sessions(
  session_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  open_digest TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  recording_finished INTEGER NOT NULL DEFAULT 0,
  manifest_json TEXT,
  transcript TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_chunks(
  session_id TEXT NOT NULL REFERENCES voice_sessions(session_id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  wall_start_ms INTEGER NOT NULL,
  wall_end_ms INTEGER NOT NULL,
  mime_type TEXT NOT NULL,
  transcript TEXT,
  PRIMARY KEY(session_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS facts(
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL,
  text TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_supported INTEGER NOT NULL,
  selected INTEGER NOT NULL,
  sources_json TEXT NOT NULL,
  PRIMARY KEY(story_id, fact_id)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  semantic_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at REAL NOT NULL,
  lease_until REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, available_at, lease_until, created_at);
CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_intents(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  request_key TEXT NOT NULL UNIQUE,
  vibepublish_request_key TEXT NOT NULL UNIQUE,
  request_json TEXT NOT NULL,
  vibepublish_operation_id TEXT,
  state TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)

    def connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = self.connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def now() -> float:
        return time.time()

    def cache_get(self, key: str) -> Any | None:
        with self.connection() as db:
            row = db.execute("SELECT value_json,expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row or row["expires_at"] <= self.now():
            return None
        return json.loads(row["value_json"])

    def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None:
        now = self.now()
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.tx() as db:
            db.execute(
                "INSERT INTO cache(key,value_json,expires_at,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at,created_at=excluded.created_at",
                (key, raw, now + ttl_seconds, now),
            )
