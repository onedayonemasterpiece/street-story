from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Store
from .providers import (
    GeminiClient,
    GroundedResearch,
    OSMClient,
    PermanentProviderError,
    RetryableProviderError,
    VibePublishClient,
    WikipediaClient,
    project_destinations,
)


class ConflictError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NotFoundError(RuntimeError):
    pass


class InvalidStateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def stable_fact_id(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return "fact_" + hashlib.sha256(normalized.encode()).hexdigest()[:20]


def _durable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    with part.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@dataclass
class ProviderBundle:
    osm: Any
    wikipedia: Any
    gemini: Any
    vibepublish: Any


class StreetStoryService:
    def __init__(self, settings: Settings, providers: ProviderBundle | None = None):
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "street-story.sqlite3")
        self.providers = providers or ProviderBundle(
            OSMClient(self.store, settings.osm_user_agent),
            WikipediaClient(self.store),
            GeminiClient(settings, self.store),
            VibePublishClient(settings),
        )

    def recover_jobs(self) -> int:
        now = self.store.now()
        with self.store.tx() as db:
            changed = db.execute(
                "UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=COALESCE(last_error,'worker_restart_recovery'),updated_at=? "
                "WHERE state='running' AND lease_until<=?",
                (now, now, now),
            ).rowcount
        return changed

    def _idem(self, db, key: str, action: str, request_digest: str, resource_type: str, resource_id: str) -> str | None:
        if not key or len(key) > 128:
            raise ConflictError("idempotency_key_invalid", "A bounded Idempotency-Key is required")
        row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row:
            if row["action"] != action or row["request_digest"] != request_digest:
                raise ConflictError("idempotency_conflict", "Idempotency-Key is bound to different content")
            return row["resource_id"]
        db.execute(
            "INSERT INTO idempotency(key,action,request_digest,resource_type,resource_id,created_at) VALUES(?,?,?,?,?,?)",
            (key, action, request_digest, resource_type, resource_id, self.store.now()),
        )
        return None

    def _story_row(self, db, story_id: str):
        row = db.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
        if not row:
            raise NotFoundError("story not found")
        return row

    def story(self, story_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            return self._story_repr(db, row)

    def stories(self) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            return [self._story_repr(db, row) for row in db.execute("SELECT * FROM stories ORDER BY created_at DESC")]

    def _story_repr(self, db, row) -> dict[str, Any]:
        facts = [{
            "fact_id": fact["fact_id"], "text": fact["text"], "confidence": fact["confidence"],
            "evidence_supported": bool(fact["evidence_supported"]), "selected": bool(fact["selected"]),
            "sources": json.loads(fact["sources_json"]),
        } for fact in db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (row["id"],))]
        error = None
        if row["error_code"] or row["error_message"]:
            error = {"code": row["error_code"] or "backend_error", "message": row["error_message"] or "Backend error"}
        processing = None
        if row["state"] == "researching":
            pending = db.execute("SELECT created_at,available_at FROM jobs WHERE story_id=? AND kind IN ('research','refinement') AND state IN ('ready','running','retry') ORDER BY created_at LIMIT 1", (row["id"],)).fetchone()
            delayed = pending and self.store.now()-pending["created_at"] >= self.settings.processing_delayed_after_seconds
            processing = {"status": "processing_delayed" if delayed else "researching", "message": "Обработка займёт немного больше времени" if delayed else "Исследуем", "automatic_retry": True}
            error = None
        return {
            "processing": processing,
            "id": row["id"], "client_story_id": row["client_story_id"], "state": row["state"],
            "place_name": row["place_name"], "summary": row["summary"], "draft_text": row["draft_text"],
            "processed_image_url": row["processed_image_url"], "scheduled_for": row["scheduled_for"],
            "published_at": row["published_at"], "revision": row["revision"], "error": error,
            "facts": facts, "destinations": [],
        }

    def create_story(
        self,
        *,
        key: str,
        client_story_id: str,
        photo_sha256: str,
        photo_mime_type: str,
        photo_bytes: bytes,
        voice_protocol: str,
        lat: float | None,
        lon: float | None,
    ) -> dict[str, Any]:
        actual = hashlib.sha256(photo_bytes).hexdigest()
        if actual.lower() != photo_sha256.lower():
            raise ConflictError("photo_digest_mismatch", "Uploaded photo does not match photo_sha256")
        if voice_protocol != "voice-chunks-v2":
            raise ConflictError("voice_protocol_unsupported", "voice-chunks-v2 is required")
        identity = {
            "client_story_id": client_story_id, "photo_sha256": photo_sha256.lower(), "photo_mime_type": photo_mime_type,
            "voice_protocol": voice_protocol, "lat": lat, "lon": lon,
        }
        req_digest = digest(identity)
        story_id = "story_" + hashlib.sha256(client_story_id.encode()).hexdigest()[:24]
        with self.store.tx() as db:
            replay = self._idem(db, key, "create_story", req_digest, "story", story_id)
            existing_client = db.execute("SELECT * FROM stories WHERE client_story_id=?", (client_story_id,)).fetchone()
            if existing_client:
                if existing_client["photo_sha256"].lower() != photo_sha256.lower():
                    raise ConflictError("client_story_photo_conflict", "client_story_id is bound to another photo digest")
                if digest({
                    "client_story_id": existing_client["client_story_id"], "photo_sha256": existing_client["photo_sha256"],
                    "photo_mime_type": existing_client["photo_mime_type"], "voice_protocol": existing_client["voice_protocol"],
                    "lat": existing_client["latitude"], "lon": existing_client["longitude"],
                }) != req_digest:
                    raise ConflictError("client_story_payload_conflict", "client_story_id is bound to different metadata")
                return self._story_repr(db, existing_client)
            if replay:
                row = self._story_row(db, replay)
                return self._story_repr(db, row)
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(photo_mime_type, ".img")
            photo_path = self.settings.data_dir / "stories" / story_id / ("source" + suffix)
            _durable_write(photo_path, photo_bytes)
            now = self.store.now()
            db.execute(
                "INSERT INTO stories(id,client_story_id,photo_sha256,photo_mime_type,photo_path,latitude,longitude,voice_protocol,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (story_id, client_story_id, photo_sha256.lower(), photo_mime_type, str(photo_path), lat, lon, voice_protocol, "photo_ready", now, now),
            )
            return self._story_repr(db, self._story_row(db, story_id))

    def open_voice(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = str(body.get("session_id", ""))
        if not session_id:
            raise ConflictError("session_id_required", "session_id is required")
        kind = str(body.get("kind", "initial"))
        if kind not in {"initial", "refinement"}:
            raise ConflictError("voice_kind_invalid", "voice kind must be initial or refinement")
        req_digest = digest({"story_id": story_id, **body})
        now = self.store.now()
        with self.store.tx() as db:
            self._story_row(db, story_id)
            replay = self._idem(db, key, "open_voice", req_digest, "voice_session", session_id)
            row = db.execute("SELECT * FROM voice_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row:
                if row["story_id"] != story_id or row["open_digest"] != req_digest:
                    raise ConflictError("voice_session_conflict", "session_id is bound to different content")
            elif replay:
                raise ConflictError("voice_session_missing", "Idempotency record points to missing voice session")
            else:
                db.execute(
                    "INSERT INTO voice_sessions(session_id,story_id,kind,open_digest,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (session_id, story_id, kind, req_digest, canonical(body), now, now),
                )
            return self._voice_receipt(db, session_id)

    def _voice_receipt(self, db, session_id: str) -> dict[str, Any]:
        session = db.execute("SELECT * FROM voice_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not session:
            raise NotFoundError("voice session not found")
        received = [{"index": row["chunk_index"], "sha256": row["sha256"]} for row in db.execute(
            "SELECT chunk_index,sha256 FROM voice_chunks WHERE session_id=? ORDER BY chunk_index", (session_id,)
        )]
        return {"session_id": session_id, "recording_finished": bool(session["recording_finished"]), "received": received}

    def put_chunk(
        self,
        story_id: str,
        session_id: str,
        index: int,
        key: str,
        content_sha256: str,
        data: bytes,
        timing: dict[str, int],
        mime_type: str,
    ) -> dict[str, Any]:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != content_sha256.lower():
            raise ConflictError("chunk_digest_mismatch", "X-Content-SHA256 does not match the uploaded chunk")
        req_digest = digest({"story_id": story_id, "session_id": session_id, "index": index, "sha256": actual, **timing, "mime_type": mime_type})
        with self.store.tx() as db:
            session = db.execute("SELECT * FROM voice_sessions WHERE session_id=? AND story_id=?", (session_id, story_id)).fetchone()
            if not session:
                raise NotFoundError("voice session not found")
            self._idem(db, key, "put_chunk", req_digest, "voice_chunk", f"{session_id}:{index}")
            existing = db.execute("SELECT * FROM voice_chunks WHERE session_id=? AND chunk_index=?", (session_id, index)).fetchone()
            if existing:
                if existing["sha256"].lower() != actual:
                    raise ConflictError("voice_chunk_conflict", "Chunk index is bound to a different digest")
                return self._voice_receipt(db, session_id)
            if session["recording_finished"]:
                raise ConflictError("voice_already_complete", "Cannot add chunks after completion")
            path = self.settings.data_dir / "voice" / session_id / f"{index:06d}-{actual[:16]}.m4a"
            _durable_write(path, data)
            db.execute(
                "INSERT INTO voice_chunks(session_id,chunk_index,sha256,path,start_ms,end_ms,wall_start_ms,wall_end_ms,mime_type) VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, index, actual, str(path), timing["start_ms"], timing["end_ms"], timing["wall_start_ms"], timing["wall_end_ms"], mime_type),
            )
            return self._voice_receipt(db, session_id)

    def complete_voice(self, story_id: str, session_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        expected = body.get("chunks") or []
        with self.store.tx() as db:
            session = db.execute("SELECT * FROM voice_sessions WHERE session_id=? AND story_id=?", (session_id, story_id)).fetchone()
            if not session:
                raise NotFoundError("voice session not found")
            self._idem(db, key, "complete_voice", req_digest, "voice_session", session_id)
            actual = [{"index": row["chunk_index"], "sha256": row["sha256"]} for row in db.execute(
                "SELECT chunk_index,sha256 FROM voice_chunks WHERE session_id=? ORDER BY chunk_index", (session_id,)
            )]
            expected_identity = [{"index": int(item["index"]), "sha256": str(item["sha256"]).lower()} for item in expected]
            exact_indices = [item["index"] for item in expected_identity] == list(range(len(expected_identity)))
            if not exact_indices or actual != expected_identity or int(body.get("chunk_count", -1)) != len(actual):
                raise ConflictError("voice_manifest_mismatch", "Exact ordered chunk manifest is required; reconcile and retry")
            if session["recording_finished"]:
                if json.loads(session["manifest_json"] or "[]") != actual:
                    raise ConflictError("voice_manifest_conflict", "Completed voice manifest is immutable")
                return self._voice_receipt(db, session_id)
            now = self.store.now()
            db.execute(
                "UPDATE voice_sessions SET recording_finished=1,manifest_json=?,metadata_json=?,updated_at=? WHERE session_id=?",
                (canonical(actual), canonical({**json.loads(session["metadata_json"]), "complete": body}), now, session_id),
            )
            if session["kind"] == "initial":
                self._enqueue_job(db, story_id, "research", f"research:{session_id}", {"voice_session_id": session_id})
                db.execute("UPDATE stories SET state='researching',revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?", (now, story_id))
            return self._voice_receipt(db, session_id)

    def mutate_facts(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        selected_ids = set(str(x) for x in body.get("selected_fact_ids", []))
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            self._story_row(db, story_id)
            if self._idem(db, key, "facts", req_digest, "story", story_id):
                return self._story_repr(db, self._story_row(db, story_id))
            facts = list(db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,)))
            valid = {row["fact_id"] for row in facts}
            unknown = selected_ids - valid
            if unknown:
                raise ConflictError("fact_id_unknown", f"Unknown fact ids: {sorted(unknown)}")
            for row in facts:
                selected = row["fact_id"] in selected_ids and bool(row["evidence_supported"])
                db.execute("UPDATE facts SET selected=? WHERE story_id=? AND fact_id=?", (int(selected), story_id, row["fact_id"]))
            return self._story_repr(db, self._story_row(db, story_id))

    def mutate_refinement(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = str(body.get("voice_session_id", ""))
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            self._story_row(db, story_id)
            if self._idem(db, key, "refinement", req_digest, "story", story_id):
                return self._story_repr(db, self._story_row(db, story_id))
            session = db.execute("SELECT * FROM voice_sessions WHERE session_id=? AND story_id=?", (session_id, story_id)).fetchone()
            if not session or session["kind"] != "refinement" or not session["recording_finished"]:
                raise InvalidStateError("refinement_voice_not_complete", "A completed refinement voice session is required")
            if "selected_fact_ids" in body:
                selected_ids = {str(value) for value in body.get("selected_fact_ids", [])}
                facts = list(db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,)))
                valid = {row["fact_id"] for row in facts}
                unknown = selected_ids - valid
                if unknown:
                    raise ConflictError("fact_id_unknown", f"Unknown fact ids: {sorted(unknown)}")
                for row in facts:
                    selected = row["fact_id"] in selected_ids and bool(row["evidence_supported"])
                    db.execute("UPDATE facts SET selected=? WHERE story_id=? AND fact_id=?", (int(selected), story_id, row["fact_id"]))
            self._enqueue_job(db, story_id, "refinement", f"refinement:{session_id}", {"voice_session_id": session_id})
            now = self.store.now()
            db.execute("UPDATE stories SET state='researching',revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?", (now, story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    def mutate_visual(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if self._idem(db, key, "visual", req_digest, "story", story_id):
                return self._story_repr(db, story)
            selected = [str(x) for x in body.get("selected_fact_ids", [])]
            facts = {r["fact_id"]: r for r in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))}
            for fact_id in selected:
                if fact_id not in facts or not facts[fact_id]["evidence_supported"]:
                    raise ConflictError("visual_fact_unsupported", f"Fact {fact_id} is not externally supported")
            context = {
                "prompt_version": "street-story-image-v1",
                "selected_facts": [{"fact_id": fid, "text": facts[fid]["text"], "sources": json.loads(facts[fid]["sources_json"])} for fid in selected],
                "place_name": story["place_name"],
                "user_voice_intent": json.loads(story["research_json"] or "{}").get("transcript", ""),
            }
            now = self.store.now()
            db.execute(
                "UPDATE stories SET state='visual_processing',visual_context_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(context), now, story_id),
            )
            self._enqueue_job(db, story_id, "visual", f"visual:{key}", {"selected_fact_ids": selected})
            return self._story_repr(db, self._story_row(db, story_id))

    def mutate_publish(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        delay = int(body.get("delay_minutes", 60))
        if delay < 1 or delay > 24 * 60:
            raise ConflictError("publish_delay_invalid", "delay_minutes must be between 1 and 1440")
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            existing = db.execute("SELECT * FROM publish_intents WHERE request_key=?", (key,)).fetchone()
            replay = self._idem(db, key, "publish", req_digest, "publish_intent", existing["id"] if existing else "pending")
            if existing:
                return self._story_repr(db, story)
            if replay:
                raise ConflictError("publish_intent_missing", "Idempotency record points to a missing publish intent")
            if story["state"] not in {"ready_to_publish", "scheduling", "scheduled"} or not story["vibepublish_asset_ref"]:
                raise InvalidStateError("visual_not_ready", "A successful verified visual is required before photo publication")
            destinations = [str(x) for x in body.get("destinations", [])]
            if not destinations:
                raise ConflictError("publish_destinations_required", "At least one destination is required")
            scheduled = datetime.now(timezone.utc) + timedelta(minutes=delay)
            scheduled_iso = scheduled.isoformat().replace("+00:00", "Z")
            intent_id = "pubintent_" + uuid.uuid4().hex[:24]
            vp_key = "ss-vp-publish-" + hashlib.sha256(f"{story_id}:{key}".encode()).hexdigest()[:48]
            request = {
                "destinations": destinations,
                "text": body.get("text_override") if body.get("text_override") is not None else story["draft_text"],
                "asset_ref": story["vibepublish_asset_ref"],
                "scheduled_for": scheduled_iso,
            }
            now = self.store.now()
            db.execute("UPDATE idempotency SET resource_id=? WHERE key=?", (intent_id, key))
            db.execute(
                "INSERT INTO publish_intents(id,story_id,request_key,vibepublish_request_key,request_json,state,scheduled_for,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (intent_id, story_id, key, vp_key, canonical(request), "pending", scheduled_iso, now, now),
            )
            self._enqueue_job(db, story_id, "publish", f"publish:{intent_id}", {"intent_id": intent_id})
            db.execute("UPDATE stories SET state='scheduling',scheduled_for=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?", (scheduled_iso, now, story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    async def capabilities(self) -> dict[str, Any]:
        try:
            bootstrap = await self.providers.vibepublish.bootstrap()
        except (RetryableProviderError, PermanentProviderError):
            bootstrap = {"destinations": []}
        result = {"destinations": project_destinations(bootstrap)}
        pool = getattr(self.providers.gemini, "pool", None)
        if pool is not None:
            result["gemini"] = {operation: pool.snapshot(operation) for operation in ("transcription", "grounded_research")}
        return result

    def _enqueue_job(self, db, story_id: str, kind: str, semantic_key: str, payload: dict[str, Any]) -> str:
        existing = db.execute("SELECT id FROM jobs WHERE semantic_key=?", (semantic_key,)).fetchone()
        if existing:
            return existing["id"]
        job_id = "job_" + uuid.uuid4().hex[:24]
        now = self.store.now()
        db.execute(
            "INSERT INTO jobs(id,story_id,kind,semantic_key,payload_json,state,available_at,created_at,updated_at) VALUES(?,?,?,?,?,'ready',?,?,?)",
            (job_id, story_id, kind, semantic_key, canonical(payload), now, now, now),
        )
        return job_id

    def _claim(self):
        now = self.store.now()
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?", (now + 90, now, row["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    async def run_once(self) -> bool:
        job = self._claim()
        if not job:
            quota = getattr(self.providers.gemini, 'quota', None)
            if quota is not None:
                try:
                    await quota.recover()
                except RetryableProviderError:
                    pass  # No provider work; pending accounting remains durable.
            return False
        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                with self.store.tx() as db:
                    db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND attempts=?", (self.store.now()+90, job['id'], job['attempts']))

        lease_task = asyncio.create_task(heartbeat())
        try:
            if job["kind"] in {"research", "refinement"}:
                await self._run_research(job)
            elif job["kind"] == "visual":
                await self._run_visual(job)
            elif job["kind"] == "publish":
                await self._run_publish(job)
            else:
                raise PermanentProviderError(f"Unknown job kind {job['kind']}")
            with self.store.tx() as db:
                db.execute("UPDATE jobs SET state='done',lease_until=0,last_error=NULL,updated_at=? WHERE id=?", (self.store.now(), job["id"]))
        except RetryableProviderError as exc:
            backoff = min(300, 2 ** min(job["attempts"], 8))
            available_at = max(self.store.now()+1, exc.retry_at) if exc.retry_at is not None else self.store.now()+backoff
            with self.store.tx() as db:
                db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=?", (available_at, self.settings.redact(str(exc)), self.store.now(), job["id"]))
            return True
        except (PermanentProviderError, ConflictError, InvalidStateError) as exc:
            with self.store.tx() as db:
                db.execute("UPDATE jobs SET state='failed',lease_until=0,last_error=?,updated_at=? WHERE id=?", (self.settings.redact(str(exc)), self.store.now(), job["id"]))
                if job["kind"] != "visual":
                    db.execute("UPDATE stories SET state='needs_review',error_code=?,error_message=?,revision=revision+1,updated_at=? WHERE id=?", ("provider_permanent_error", "Не удалось выполнить обработку. Требуется проверка настроек сервиса.", self.store.now(), job["story_id"]))
            return True
        except Exception as exc:
            with self.store.tx() as db:
                db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=?", (self.store.now()+5, f"worker_failure:{type(exc).__name__}", self.store.now(), job["id"]))
            return True
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
        return True

    async def _transcribe_session(self, session_id: str) -> str:
        with self.store.connection() as db:
            session = db.execute("SELECT * FROM voice_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not session or not session["recording_finished"]:
                raise PermanentProviderError("Voice session is not complete")
            chunks = [dict(row) for row in db.execute("SELECT * FROM voice_chunks WHERE session_id=? ORDER BY chunk_index", (session_id,))]
            if session["transcript"] is not None:
                return session["transcript"]
        transcripts: list[str] = []
        for chunk in chunks:
            transcript = chunk["transcript"]
            if transcript is None:
                transcript = await self.providers.gemini.transcribe(Path(chunk["path"]), chunk["mime_type"])
                with self.store.tx() as db:
                    current = db.execute("SELECT transcript FROM voice_chunks WHERE session_id=? AND chunk_index=?", (session_id, chunk["chunk_index"])).fetchone()
                    if current["transcript"] is None:
                        db.execute("UPDATE voice_chunks SET transcript=? WHERE session_id=? AND chunk_index=?", (transcript, session_id, chunk["chunk_index"]))
                    else:
                        transcript = current["transcript"]
            transcripts.append(transcript)
        aggregate = "\n".join(t for t in transcripts if t).strip()
        with self.store.tx() as db:
            db.execute("UPDATE voice_sessions SET transcript=?,updated_at=? WHERE session_id=?", (aggregate, self.store.now(), session_id))
        return aggregate

    async def _run_research(self, job: dict[str, Any]) -> None:
        payload = json.loads(job["payload_json"])
        story_id = job["story_id"]
        session_id = payload["voice_session_id"]
        refinement_text = await self._transcribe_session(session_id)
        with self.store.connection() as db:
            story = dict(self._story_row(db, story_id))
            prior = json.loads(story["research_json"] or "{}")
            previous_facts = [{"fact_id": row["fact_id"], "text": row["text"], "selected": bool(row["selected"]), "sources": json.loads(row["sources_json"])} for row in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))]
            if job["kind"] == "refinement":
                transcript = "\n\n".join(x for x in [prior.get("transcript", ""), "Уточнение пользователя: " + refinement_text] if x)
            else:
                transcript = refinement_text
        lat, lon = story["latitude"], story["longitude"]
        osm = {"reverse": {}, "nearby": []}
        wikipedia: list[dict[str, Any]] = []
        if lat is not None and lon is not None:
            osm = self.store.checkpoint_get(job['id'], 'osm')
            if osm is None:
                osm = await self.providers.osm.lookup(float(lat), float(lon))
                self.store.checkpoint_put(job['id'], 'osm', osm)
            wikipedia = self.store.checkpoint_get(job['id'], 'wikipedia')
            if wikipedia is None:
                wikipedia = await self.providers.wikipedia.nearby(float(lat), float(lon))
                self.store.checkpoint_put(job['id'], 'wikipedia', wikipedia)
        saved = self.store.checkpoint_get(job['id'], 'grounded_research')
        if saved is None:
            grounded = await self.providers.gemini.research(
                Path(story["photo_path"]), story["photo_mime_type"], transcript, osm, wikipedia, previous_facts
            )
            self.store.checkpoint_put(job['id'], 'grounded_research', {'payload': grounded.payload, 'grounding_sources': grounded.grounding_sources})
        else:
            grounded = GroundedResearch(**saved)
        source_objects: dict[str, dict[str, str]] = {}
        for source in OSMClient.sources(osm):
            source_objects[source["url"].rstrip("/")] = source
        for page in wikipedia:
            url = str(page.get("url", ""))
            if url.startswith("https://"):
                source_objects[url.rstrip("/")] = {"type": "wikipedia", "title": str(page.get("title", url)), "url": url}
        for source in grounded.grounding_sources:
            source_objects[source["url"].rstrip("/")] = source
        incoming = grounded.payload.get("facts", [])[:20]
        normalized: list[dict[str, Any]] = []
        for item in incoming:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            sources = []
            for url in item.get("source_urls", []) or []:
                candidate = source_objects.get(str(url).rstrip("/"))
                if candidate and candidate not in sources:
                    sources.append(candidate)
            normalized.append({
                "fact_id": stable_fact_id(text), "text": text,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
                "evidence_supported": bool(sources), "sources": sources,
            })
        with self.store.tx() as db:
            old_selected = {r["fact_id"]: bool(r["selected"]) for r in db.execute("SELECT fact_id,selected FROM facts WHERE story_id=?", (story_id,))}
            db.execute("DELETE FROM facts WHERE story_id=?", (story_id,))
            for fact in normalized:
                selected = fact["evidence_supported"] and old_selected.get(fact["fact_id"], True)
                db.execute(
                    "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,?,?,?,?,?)",
                    (story_id, fact["fact_id"], fact["text"], fact["confidence"], int(fact["evidence_supported"]), int(selected), canonical(fact["sources"])),
                )
            reverse = osm.get("reverse", {})
            place_name = str(grounded.payload.get("place_name") or (reverse.get("namedetails") or {}).get("name") or reverse.get("display_name") or "").strip() or None
            research = {
                "transcript": transcript,
                "osm": osm,
                "wikipedia": wikipedia,
                "grounding_sources": grounded.grounding_sources,
            }
            now = self.store.now()
            db.execute(
                "UPDATE stories SET state='review',place_name=?,summary=?,draft_text=?,research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (place_name, str(grounded.payload.get("summary", "")).strip() or None, str(grounded.payload.get("draft_text", "")).strip() or None, canonical(research), now, story_id),
            )

    async def _run_visual(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        try:
            await self.providers.vibepublish.bootstrap()
        except (RetryableProviderError, PermanentProviderError) as exc:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='visual_blocked',error_code='vibepublish_runtime_unavailable',error_message=?,revision=revision+1,updated_at=? WHERE id=?",
                    (str(exc), self.store.now(), story_id),
                )
            return
        if not self.providers.vibepublish.source_image_ingress_supported():
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='visual_blocked',error_code='vibepublish_media_ingress_not_enabled',"
                    "error_message='VibePublish PR #1 has no authenticated HTTP source-image ingress; retry this same story after runtime/contract support is deployed',"
                    "revision=revision+1,updated_at=? WHERE id=?",
                    (self.store.now(), story_id),
                )
            return
        raise PermanentProviderError("VibePublish source-image ingress contract changed; backend integration must be updated from fresh contract")

    async def _run_publish(self, job: dict[str, Any]) -> None:
        intent_id = json.loads(job["payload_json"])["intent_id"]
        with self.store.connection() as db:
            intent = dict(db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone())
        request = json.loads(intent["request_json"])
        if not intent["vibepublish_operation_id"]:
            bootstrap = await self.providers.vibepublish.bootstrap()
            projected = project_destinations(bootstrap)
            real_aliases = {d["alias"] for d in projected if d["status"] in {"supported", "needs_review"}}
            if not set(request["destinations"]).issubset(real_aliases):
                raise PermanentProviderError("Requested destination is not in current VibePublish bootstrap projection")
            payload = {
                "to": request["destinations"],
                "content": {"text": request.get("text") or "", "format": "plain"},
                "media": [{"source": {"kind": "asset", "id": request["asset_ref"]}, "role": "image"}],
                "surface": "post",
                "delivery": {"kind": "at", "at": request["scheduled_for"]},
                "mode": "execute",
                "routing_revision": bootstrap["routing_revision"],
            }
            receipt = await self.providers.vibepublish.publish(payload, intent["vibepublish_request_key"])
            operation_id = receipt.get("operation_id")
            if not operation_id:
                raise RetryableProviderError("VibePublish accepted no recoverable operation_id")
            with self.store.tx() as db:
                db.execute("UPDATE publish_intents SET vibepublish_operation_id=?,state=?,updated_at=? WHERE id=?", (operation_id, receipt.get("state", "accepted"), self.store.now(), intent_id))
            state = receipt.get("state")
        else:
            receipt = await self.providers.vibepublish.status(intent["vibepublish_operation_id"])
            receipts = receipt.get("receipts") or []
            current = receipts[0] if receipts else receipt
            state = current.get("state")
        if state in {"scheduled", "verified"}:
            with self.store.tx() as db:
                db.execute("UPDATE publish_intents SET state=?,last_error=NULL,updated_at=? WHERE id=?", (state, self.store.now(), intent_id))
                db.execute("UPDATE stories SET state='scheduled',scheduled_for=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?", (request["scheduled_for"], self.store.now(), job["story_id"]))
        elif state in {"failed", "blocked", "outcome_unknown", "cancelled"}:
            raise PermanentProviderError(f"VibePublish operation ended in {state}")
        else:
            raise RetryableProviderError(f"VibePublish operation is {state or 'pending'}")

    def asset(self, story_id: str) -> tuple[bytes, str, str]:
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            path = row["processed_image_path"]
            if not path:
                raise NotFoundError("processed asset not found")
            data = Path(path).read_bytes()
            return data, "image/png", hashlib.sha256(data).hexdigest()
