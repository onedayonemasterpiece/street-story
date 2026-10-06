from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings, reveal
from .db import Store
from .errors import research_retry_at
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


MAX_JOB_ATTEMPTS = 8
RETRY_EXHAUSTED_ERROR = "provider_retry_exhausted"
RETRY_EXHAUSTED_MESSAGE = "Automatic processing stopped after repeated provider failures. Review and retry manually."
RESEARCH_JOB_KINDS = {'identity', 'identity_visual', 'research', 'refinement'}


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
    research: Any = None


class StreetStoryService:
    def __init__(self, settings: Settings, providers: ProviderBundle | None = None):
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "street-story.sqlite3")
        from .temporary_photos import TemporaryPhotos
        self._temporary_photos = TemporaryPhotos()
        self.providers = providers or ProviderBundle(
            OSMClient(self.store, settings.osm_user_agent),
            WikipediaClient(self.store),
            GeminiClient(settings, self.store),
            VibePublishClient(settings),
        )

        if self.providers.research is None and (settings.research_endpoint or settings.native_vision_reserve or reveal(settings.research_gigachat_key)):
            from .research_adapter import ProductResearchAdapter
            self.providers.research = ProductResearchAdapter(self)

    def recover_jobs(self) -> int:
        now = self.store.now()
        with self.store.tx() as db:
            exhausted = [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE state IN ('ready','retry','running') AND attempts>=?",
                (MAX_JOB_ATTEMPTS,),
            )]
            changed = 0
            for job in exhausted:
                if job['kind'] in {'research', 'refinement'}:
                    row = db.execute("SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage=?",
                                     (job['id'], 'worker_non_wait_failures')).fetchone()
                    if not row or int(json.loads(row[0]).get('count', 0)) < MAX_JOB_ATTEMPTS:
                        continue
                elif job["kind"] in RESEARCH_JOB_KINDS and not str(job.get('last_error') or '').startswith('worker_failure:'):
                    continue
                self._fail_retry_exhausted(db, job, "retry_budget_exhausted")
                changed += 1
            changed += db.execute(
                "UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=COALESCE(last_error,'worker_restart_recovery'),updated_at=? "
                "WHERE state='running' AND lease_until<=?",
                (now, now, now),
            ).rowcount
            # A crash can leave a joined request after the preceding job became
            # terminal. Never start it alongside a recovered active attempt.
            for story in db.execute("SELECT id FROM stories WHERE research_json LIKE ?",
                                    ('%"pending_fact_request"%',)):
                changed += int(self._resume_joined_fact_request(db, story['id']))
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
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            self._hydrate_poi_memory(db, row)
            row = self._story_row(db, story_id)
            return self._story_repr(db, row)

    def _source_photo_bytes(self, story_id: str) -> bytes:
        data = self._temporary_photos.get(story_id)
        if data is None:
            raise ConflictError('source_unavailable', 'Временное фото недоступно. Загрузите его повторно из галереи.')
        return data

    def _source_photo_for_job(self, story_id: str) -> bytes:
        try:
            return self._source_photo_bytes(story_id)
        except ConflictError as exc:
            raise RetryableProviderError('source_unavailable', retry_at=self.store.now()+300) from exc

    def _restore_source_photo(self, db, story_id: str, data: bytes):
        self._temporary_photos.put(story_id, data)
        # A normal reupload wakes only tasks waiting for ephemeral photo bytes.
        # Unknown external operations keep their existing reconciliation state.
        db.execute("UPDATE jobs SET available_at=?,updated_at=? WHERE story_id=? AND state='retry' "
                   "AND last_error IN ('source_unavailable','identity_source_unavailable')",
                   (self.store.now(), self.store.now(), story_id))

    def _hydrate_poi_memory(self, db, row) -> int:
        from .poi_memory import ensure_poi_identity, hydrate_story_facts, memory_keys
        research = json.loads(row['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        if identity.get('status') not in {'match', 'owner_confirmed'} or row['state'] in {'scheduling', 'scheduled', 'published'}:
            return 0
        keys = memory_keys(db, identity)
        chosen = next((item for item in identity.get('candidates') or [] if item.get('candidate_id') == identity.get('candidate_id')), {})
        bound = db.execute("SELECT 1 FROM poi_aliases WHERE namespace='street_story_candidate' AND value=?", (identity.get('candidate_id'),)).fetchone()
        verified = identity.get('status') == 'match' and identity.get('visual_reference_verified') is True
        aliases = set(chosen.get('alias_candidate_ids') or [])
        if verified:
            from .identity_subject_binding import subject_aliases
            aliases.update(subject_aliases(identity.get('candidates') or []).get(identity.get('candidate_id'), set()))
        if not bound or (verified and (not research.get('poi_id') or any(alias not in keys for alias in aliases))):
            poi_id = ensure_poi_identity(db, identity, latitude=row['latitude'], longitude=row['longitude'], now=self.store.now())
            if poi_id and research.get('poi_id') != poi_id:
                research['poi_id'] = poi_id
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), row['id']))
        added = hydrate_story_facts(db, identity, row['id'])
        if added:
            db.execute('UPDATE stories SET revision=revision+1,updated_at=? WHERE id=?', (self.store.now(), row['id']))
        return added

    def stories(self) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            return [self._story_repr(db, row) for row in db.execute("SELECT * FROM stories ORDER BY created_at DESC")]

    def delete_story(self, story_id: str) -> dict[str, Any]:
        durable_paths: list[Path] = []
        story_dir: Path | None = None
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            if row["state"] in {"scheduling", "scheduled"}:
                raise ConflictError(
                    "scheduled_story_delete_blocked",
                    "Сначала отмените запланированную публикацию, затем удалите тему.",
                )
            for value in (row["photo_path"], row["processed_image_path"]):
                if value:
                    durable_paths.append(Path(str(value)))
            durable_paths.extend(
                Path(str(item["path"]))
                for item in db.execute(
                    "SELECT vc.path FROM voice_chunks vc JOIN voice_sessions vs ON vs.session_id=vc.session_id "
                    "WHERE vs.story_id=?",
                    (story_id,),
                )
                if item["path"]
            )
            story_dir = self.settings.data_dir / "stories" / story_id
            db.execute("DELETE FROM stories WHERE id=?", (story_id,))
        root = self.settings.data_dir.resolve()
        self._temporary_photos.discard(story_id)
        for path in durable_paths:
            try:
                resolved = path.resolve()
                if root in resolved.parents and resolved.is_file():
                    resolved.unlink()
            except OSError:
                pass
        if story_dir is not None:
            try:
                resolved_dir = story_dir.resolve()
                if root in resolved_dir.parents and resolved_dir.is_dir():
                    shutil.rmtree(resolved_dir)
            except OSError:
                pass
        return {"ok": True, "story_id": story_id}

    def _story_repr(self, db, row) -> dict[str, Any]:
        from .fact_ledger import assertion_state, backfill_legacy_fact_ledger
        from .research_control import KINDS, PURPOSES, research_stopped
        research = json.loads(row['research_json'] or '{}')
        generation = int(research.get('identity_generation') or 0)
        controls = research.get('research_controls') or {}
        pending_research = {purpose: False for purpose in PURPOSES}
        for job in db.execute("SELECT kind,payload_json FROM jobs WHERE story_id=? AND state IN ('ready','retry','running')", (row['id'],)):
            payload = json.loads(job['payload_json'] or '{}')
            if (payload.get('photo_sha256', row['photo_sha256']) != row['photo_sha256']
                    or payload.get('identity_generation', generation) != generation):
                continue
            for purpose in PURPOSES:
                if job['kind'] in KINDS[purpose] and not research_stopped(
                        research, purpose, photo_sha256=row['photo_sha256'], identity_generation=generation):
                    pending_research[purpose] = True
        backfill_legacy_fact_ledger(db, self.store.now())
        assertion_rows = assertion_state(db, row["id"])
        facts = []
        for fact in db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (row["id"],)):
            assertion = assertion_rows.get(str(fact["fact_id"]), {})
            facts.append({
                "fact_id": fact["fact_id"], "text": fact["text"], "confidence": fact["confidence"],
                "evidence_supported": bool(fact["evidence_supported"]), "selected": bool(fact["selected"]),
                "owner_selected": bool(assertion.get("owner_selected", fact["selected"])),
                "review_status": str(assertion.get("review_status") or "unreviewed"),
                "eligibility": str(assertion.get("eligibility") or "unreviewed"),
                "revision_digest": str(assertion.get("revision_digest") or ""),
                "supporting_evidence_keys": sorted({digest(list(evidence)) for evidence in db.execute(
                    "SELECT e.source_url,e.source_version_id,e.chunk_id,e.span_start,e.span_end,e.span_sha256 "
                    "FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id "
                    "WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted'", (row['id'], fact['fact_id']))}),
                "sources": json.loads(fact["sources_json"]),
            })
        error = None
        if row["error_code"] or row["error_message"]:
            error = {"code": row["error_code"] or "backend_error", "message": row["error_message"] or "Backend error"}
        processing = None
        if row["state"] == "identifying":
            pending = db.execute(
                "SELECT created_at,available_at FROM jobs WHERE story_id=? AND kind='identity' "
                "AND state IN ('ready','running','retry') ORDER BY created_at LIMIT 1",
                (row["id"],),
            ).fetchone()
            delayed = pending and self.store.now()-pending["created_at"] >= self.settings.processing_delayed_after_seconds
            processing = {
                "status": "processing_delayed" if delayed else "identifying",
                "message": "Определение объекта займёт немного больше времени" if delayed else "Определяем объект",
                "automatic_retry": True,
            }
            error = None
        elif row["state"] == "researching":
            pending = db.execute("SELECT created_at,available_at FROM jobs WHERE story_id=? AND kind IN ('research','refinement') AND state IN ('ready','running','retry') ORDER BY created_at LIMIT 1", (row["id"],)).fetchone()
            delayed = pending and self.store.now()-pending["created_at"] >= self.settings.processing_delayed_after_seconds
            processing = {"status": "processing_delayed" if delayed else "researching", "message": "Обработка займёт немного больше времени" if delayed else "Исследуем", "automatic_retry": True}
            error = None
        processing_purpose = {'identifying': 'identity', 'researching': 'facts'}.get(row['state'])
        if processing_purpose and research_stopped(research, processing_purpose,
                photo_sha256=row['photo_sha256'], identity_generation=generation):
            processing = {'status': 'research_paused', 'message': 'Исследование приостановлено', 'automatic_retry': False}
        return {
            "processing": processing,
            "id": row["id"], "client_story_id": row["client_story_id"], "state": row["state"],
            "photo_sha256": row['photo_sha256'], "identity_generation": generation,
            "source_available": self._temporary_photos.get(row['id']) is not None,
            "research_control_revision": int(research.get('research_control_revision') or 0),
            "research_controls": {purpose: {
                'stopped': research_stopped(research, purpose, photo_sha256=row['photo_sha256'], identity_generation=generation),
                'revision': int((controls.get(purpose) or {}).get('revision') or 0),
                'photo_sha256': row['photo_sha256'], 'identity_generation': generation,
            } for purpose in PURPOSES},
            "research_pending": pending_research,
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
        # Legacy API field is an opaque upload token, never an image checksum.
        if not photo_bytes:
            raise ConflictError('photo_empty', 'Uploaded photo is empty')
        if voice_protocol != "voice-chunks-v2":
            raise ConflictError("voice_protocol_unsupported", "voice-chunks-v2 is required")
        identity = {
            "client_story_id": client_story_id, "photo_sha256": photo_sha256.lower(), "photo_mime_type": photo_mime_type,
            "voice_protocol": voice_protocol, "lat": lat, "lon": lon,
        }
        req_digest = digest(identity)
        story_id = "story_" + hashlib.sha256(client_story_id.encode()).hexdigest()[:24]
        with self.store.tx() as db:
            existing_client = db.execute("SELECT * FROM stories WHERE client_story_id=?", (client_story_id,)).fetchone()
            if existing_client:
                if existing_client["photo_sha256"].lower() != photo_sha256.lower():
                    raise ConflictError("client_story_photo_conflict", "client_story_id is bound to another upload token")
                prior = db.execute('SELECT * FROM idempotency WHERE key=?', (key,)).fetchone()
                if prior and prior['action'] == 'create_story' and prior['resource_id'] == story_id:
                    req_digest = prior['request_digest']
                self._idem(db, key, 'create_story', req_digest, 'story', story_id)
                self._restore_source_photo(db, story_id, photo_bytes)
                # Reupload refreshes only ephemeral transport, including a new
                # encoding; accumulated editorial/location state is authoritative.
                db.execute('UPDATE stories SET photo_mime_type=? WHERE id=?', (photo_mime_type, story_id))
                return self._story_repr(db, self._story_row(db, story_id))
            replay = self._idem(db, key, "create_story", req_digest, "story", story_id)
            if replay:
                row = self._story_row(db, replay)
                self._restore_source_photo(db, story_id, photo_bytes)
                return self._story_repr(db, row)
            self._temporary_photos.put(story_id, photo_bytes)
            now = self.store.now()
            db.execute(
                "INSERT INTO stories(id,client_story_id,photo_sha256,photo_mime_type,photo_path,latitude,longitude,voice_protocol,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (story_id, client_story_id, photo_sha256.lower(), photo_mime_type, '', lat, lon, voice_protocol, "photo_ready", now, now),
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
            from .fact_ledger import set_owner_selection
            set_owner_selection(db, story_id, list(selected_ids), self.store.now())
            return self._story_repr(db, self._story_row(db, story_id))

    def mutate_research_control(self, story_id: str, key: str, body: dict[str, Any]):
        from .research_control import resume_research, stop_research
        if not isinstance(body, dict) or body.get('action') not in {'stop', 'resume'}:
            raise InvalidStateError('research_control_invalid', 'Укажите action: stop или resume.')
        purpose = body.get('purpose', 'all')
        if purpose not in {'all', 'identity', 'facts'}:
            raise InvalidStateError('research_control_invalid', 'Укажите purpose: identity, facts или all.')
        control_revision = body.get('expected_control_revision')
        if control_revision is not None and (type(control_revision) is not int or control_revision < 0):
            raise InvalidStateError('research_control_invalid', 'Ревизия управления должна быть целым неотрицательным числом.')
        generation = body.get('expected_identity_generation')
        if generation is not None and (type(generation) is not int or generation < 0):
            raise InvalidStateError('research_control_invalid', 'Поколение объекта должно быть целым неотрицательным числом.')
        with self.store.tx() as db:
            self._story_row(db, story_id)
            if self._idem(db, key, 'research_control', digest({'story_id': story_id, **body}), 'story', story_id):
                return {'action': body['action'], 'purposes': ['identity', 'facts'] if purpose == 'all' else [purpose],
                        'changed': [], 'story': self._story_repr(db, self._story_row(db, story_id))}
            operation = stop_research if body['action'] == 'stop' else resume_research
            return operation(self, story_id, purpose=purpose, expected_photo_sha256=body.get('expected_photo_sha256'),
                             expected_identity_generation=generation, expected_control_revision=control_revision, _db=db)

    async def close(self):
        researcher = getattr(self.providers, 'research', None)
        close = getattr(researcher, 'close', None)
        if callable(close):
            await close()

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
                from .fact_ledger import set_owner_selection
                set_owner_selection(db, story_id, list(selected_ids), self.store.now())
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
            from .fact_ledger import eligibility_issues_for_ids
            issues = eligibility_issues_for_ids(db, story_id, selected)
            if issues:
                raise InvalidStateError(
                    "fact_review_required",
                    "Requested facts still need semantic review before visual generation.",
                )
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

    def _rejected_publication_can_be_reconfirmed(self, db, story) -> bool:
        """Only definite pre-dispatch rejection permits a new intent after review.

        A lost response, accepted operation or generic provider failure is NOT
        evidence that publication did not happen. Never replay those here.
        """
        if story["state"] != "needs_review" or story["error_code"] != "provider_permanent_error":
            return False
        job = db.execute(
            "SELECT * FROM jobs WHERE story_id=? AND kind='publish' ORDER BY created_at DESC LIMIT 1",
            (story["id"],),
        ).fetchone()
        if not job or job["state"] != "failed" or job["last_error"] != "VibePublish request failed: HTTP 422":
            return False
        intent_id = json.loads(job["payload_json"] or "{}").get("intent_id")
        intent = db.execute("SELECT * FROM publish_intents WHERE id=? AND story_id=?", (intent_id, story["id"])).fetchone()
        return bool(intent and not intent["vibepublish_operation_id"])

    def mutate_publish(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        scheduled_raw = str(body.get("scheduled_for") or "").strip()
        if scheduled_raw:
            try:
                scheduled_dt = datetime.fromisoformat(scheduled_raw.replace("Z", "+00:00"))
            except ValueError:
                raise ConflictError("publish_time_invalid", "scheduled_for must be ISO-8601") from None
            if scheduled_dt.tzinfo is None:
                raise ConflictError("publish_time_invalid", "scheduled_for must include an offset")
            now_dt = datetime.now(timezone.utc)
            scheduled_utc = scheduled_dt.astimezone(timezone.utc)
            if scheduled_utc <= now_dt or scheduled_utc > now_dt + timedelta(days=90):
                raise ConflictError("publish_time_invalid", "scheduled_for must be within the next 90 days")
            scheduled_iso = scheduled_dt.isoformat()
        else:
            delay = int(body.get("delay_minutes", 60))
            if delay < 1 or delay > 24 * 60:
                raise ConflictError("publish_delay_invalid", "delay_minutes must be between 1 and 1440")
            scheduled_iso = (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat().replace("+00:00", "Z")
        if datetime.fromisoformat(scheduled_iso.replace("Z", "+00:00")).astimezone(timezone.utc) < datetime.now(timezone.utc) + timedelta(seconds=90):
            raise ConflictError("publish_time_too_soon", "Choose a publication time at least two minutes from now; native scheduling needs delivery lead time")
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            from .fact_ledger import selected_eligibility_issues
            issues = selected_eligibility_issues(db, story_id)
            if issues:
                raise InvalidStateError(
                    "fact_review_required",
                    "Selected facts still need semantic review or arbitration before publication.",
                )
            existing = db.execute("SELECT * FROM publish_intents WHERE request_key=?", (key,)).fetchone()
            replay = self._idem(db, key, "publish", req_digest, "publish_intent", existing["id"] if existing else "pending")
            if existing:
                return self._story_repr(db, story)
            if replay:
                raise ConflictError("publish_intent_missing", "Idempotency record points to a missing publish intent")
            publish_ready = story["state"] in {"ready_to_publish", "scheduling", "scheduled"}
            if not publish_ready:
                publish_ready = self._rejected_publication_can_be_reconfirmed(db, story)
            if not publish_ready or not story["vibepublish_asset_ref"]:
                raise InvalidStateError("visual_not_ready", "A successful verified visual is required before photo publication")
            destinations = [str(x) for x in body.get("destinations", [])]
            if not destinations:
                raise ConflictError("publish_destinations_required", "At least one destination is required")
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

    def _fail_retry_exhausted(self, db, job: dict[str, Any], last_error: str) -> None:
        now = self.store.now()
        changed = db.execute(
            "UPDATE jobs SET state='failed',lease_until=0,last_error=?,updated_at=? WHERE id=? AND state=? AND attempts=?",
            (last_error, now, job["id"], job['state'], job['attempts']),
        ).rowcount
        if not changed:
            return
        if not self._preserve_background_fact_value(db, job, error=last_error):
            db.execute(
                "UPDATE stories SET state='needs_review',error_code=?,error_message=?,revision=revision+1,updated_at=? "
                "WHERE id=? AND (state!='needs_review' OR COALESCE(error_code,'')!=?)",
                (RETRY_EXHAUSTED_ERROR, RETRY_EXHAUSTED_MESSAGE, now, job["story_id"], RETRY_EXHAUSTED_ERROR),
            )
        if job['kind'] in {'research', 'refinement'}:
            self._resume_joined_fact_request(db, job['story_id'])

    def _preserve_background_fact_value(self, db, job: dict[str, Any], *,
                                        error: str = 'provider_failure', terminal: bool = True) -> bool:
        """A failed fact worker cannot discard usable confirmed editorial value."""
        if job['kind'] not in {'research', 'refinement'}:
            return False
        payload = json.loads(job['payload_json'] or '{}')
        story = self._story_row(db, job['story_id'])
        research = json.loads(story['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        generation = int(research.get('identity_generation') or 0)
        if (payload.get('identity_generation', generation) != generation
                or payload.get('photo_sha256', story['photo_sha256']) != story['photo_sha256']):
            return True  # A superseded worker cannot damage the current story.
        usable = identity.get('status') in {'match', 'owner_confirmed'} and db.execute(
            "SELECT 1 FROM facts f JOIN fact_assertions a "
            "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
            "WHERE f.story_id=? AND f.evidence_supported=1 AND a.eligibility='eligible' LIMIT 1",
            (job['story_id'],),
        ).fetchone() is not None
        reason = error if re.fullmatch(r'[A-Za-z0-9._:-]{1,120}', error) else 'provider_failure'
        # The research worker already addresses its run by this frozen job/input.
        # Mark only that existing run; never change completed source coverage or
        # a different worker, and retain chunk/provider receipts for later work.
        if payload.get('input_revision'):
            run_id = 'research_' + hashlib.sha256(
                f"{job['id']}:{payload['input_revision']}".encode('utf-8')).hexdigest()[:24]
            db.execute("UPDATE research_runs SET state=?,status_detail=?,updated_at=? "
                       "WHERE run_id=? AND story_id=? AND identity_generation=? "
                       "AND state NOT IN ('completed','cancelled')",
                       ('partial' if usable or not terminal else 'failed', reason, self.store.now(),
                        run_id, job['story_id'], generation))
        if not usable:
            return False
        # Research enqueue may have replaced the stage with 'researching'. A
        # retained draft is still reviewable; all other product stages stay put.
        db.execute(
            "UPDATE stories SET state=CASE WHEN draft_text IS NULL OR trim(draft_text)='' "
            "THEN 'facts_ready' ELSE 'review' END,revision=revision+1,updated_at=? "
            "WHERE id=? AND state='researching' AND error_code IS NULL",
            (self.store.now(), job['story_id']),
        )
        logging.getLogger('uvicorn.error').info('street_story_research_failed_partial_value %s', canonical({
            'component': 'durable_worker', 'story_id': job['story_id'], 'job_id': job['id'],
            'kind': job['kind'], 'attempt': job['attempts'], 'eligible_facts_retained': True,
            'editorial_state_preserved': True, 'reason': reason, 'terminal': terminal,
        }))
        return True

    def _resume_joined_fact_request(self, db, story_id: str) -> bool:
        scheduler = getattr(self, '_schedule_joined_fact_request', None)
        if not callable(scheduler):
            return False
        active = db.execute(
            "SELECT 1 FROM jobs WHERE story_id=? AND kind IN ('research','refinement') "
            "AND state IN ('ready','retry','running') LIMIT 1", (story_id,)
        ).fetchone()
        if active:
            return False
        return bool(scheduler(db, story_id))

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
            # Owner actions and their visual continuation precede speculative
            # backfill. Legacy unmarked visual/automatic-fact jobs stay background.
            row = db.execute(
                "SELECT * FROM jobs WHERE ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?)) "
                "ORDER BY CASE WHEN kind IN ('identity','refinement','visual','publish') THEN 0 "
                "WHEN json_extract(payload_json,'$.queue_priority')='interactive' THEN 0 "
                "WHEN kind='research' AND semantic_key NOT LIKE 'automatic-facts:%' "
                "AND coalesce(json_extract(payload_json,'$.queue_priority'),'')<>'background' THEN 0 "
                "ELSE 1 END,created_at,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?", (now + 90, now, row["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def _schedule_identity_visual(self):
        researcher = getattr(self.providers, 'research', None)
        if researcher is None or not researcher.vision_available:
            return
        scheduled = 0
        with self.store.tx() as db:
            # Bound new work, not the stories inspected before eligibility checks.
            # Skipped and already queued stories must not hide later hypotheses.
            for row in db.execute(
                "SELECT stories.*,EXISTS(SELECT 1 FROM jobs origin WHERE origin.story_id=stories.id "
                "AND origin.kind='identity' AND json_extract(origin.payload_json,'$.identity_generation')="
                "coalesce(json_extract(stories.research_json,'$.identity_generation'),0) "
                "AND json_extract(origin.payload_json,'$.queue_priority')='interactive') AS interactive_identity "
                "FROM stories WHERE state IN ('needs_review','identifying','photo_ready') "
                "ORDER BY interactive_identity DESC,created_at,id"):
                research = json.loads(row['research_json'] or '{}')
                identity = research.get('visual_identity') or {}
                generation = int(research.get('identity_generation') or 0)
                from .research_control import research_stopped
                if (identity.get('status') in {'match','owner_confirmed'} or not identity.get('candidates')
                        or research_stopped(research, 'identity', photo_sha256=row['photo_sha256'], identity_generation=generation)):
                    continue
                semantic = f"identity-visual:{row['id']}:{generation}:{row['photo_sha256']}"
                if db.execute('SELECT 1 FROM jobs WHERE semantic_key=?', (semantic,)).fetchone():
                    continue
                self._enqueue_job(db,row['id'],'identity_visual',semantic,
                    {'identity_generation':generation,
                     'queue_priority': 'interactive' if row['interactive_identity'] else 'background'})
                scheduled += 1
                if scheduled >= 20:
                    break
        if scheduled:
            logging.getLogger(__name__).info('research_scheduler %s', canonical({
                'component': 'research_scheduler', 'stage': 'identity_visual', 'scheduled': scheduled}))

    def _schedule_confirmed_facts(self):
        """After visual confirmation, collect evidence without requiring voice.

        One frozen initial request uses the existing jobs/run intake. Subsequent
        scopes are model-owned; selection, concept and draft remain editorial.
        """
        researcher = getattr(self.providers, 'research', None)
        if researcher is None or not getattr(researcher, 'facts_available', False):
            return
        from .research_control import research_stopped
        scheduled = 0
        with self.store.tx() as db:
            for row in db.execute("SELECT * FROM stories WHERE state='identity_ready' ORDER BY created_at,id"):
                research = json.loads(row['research_json'] or '{}')
                identity = research.get('visual_identity') or {}
                generation = int(research.get('identity_generation') or 0)
                automatic = research.get('automatic_fact_request') or {}
                if (identity.get('status') not in {'match', 'owner_confirmed'} or research.get('input_revision')
                        or automatic.get('photo_sha256') == row['photo_sha256'] and automatic.get('identity_generation') == generation
                        or research_stopped(research, 'facts', photo_sha256=row['photo_sha256'], identity_generation=generation)
                        or db.execute("SELECT 1 FROM jobs WHERE story_id=? AND kind IN ('research','refinement') "
                                      "AND state IN ('ready','retry','running')", (row['id'],)).fetchone()):
                    continue
                revision = 'automatic-facts:' + digest([row['id'], row['photo_sha256'], generation, identity.get('candidate_id')])
                if db.execute('SELECT 1 FROM jobs WHERE semantic_key=?', (revision,)).fetchone():
                    continue
                payload = {'input_revision': revision, 'photo_sha256': row['photo_sha256'], 'queue_priority': 'background',
                    'identity_generation': generation, 'voice_session_ids': [], 'mode': 'initial',
                    'coverage_goal': 'Найди проверенные сведения о подтверждённом объекте для будущей публикации. '
                                     'Переиспользуй известные факты; исследуй недостающие полезные аспекты. '
                                     'Сохрани источники, не выбирай факты и не изменяй концепцию или текст автора.',
                    'extraction_scope': 'initial-confirmed-poi-v1'}
                self._enqueue_job(db, row['id'], 'research', revision, payload)
                research['automatic_fact_request'] = {'input_revision': revision, 'identity_generation': generation,
                                                      'photo_sha256': row['photo_sha256']}
                db.execute("UPDATE stories SET research_json=?,state='researching',revision=revision+1,updated_at=? WHERE id=?",
                           (canonical(research), self.store.now(), row['id']))
                scheduled += 1
                if scheduled >= 20:
                    break
        if scheduled:
            logging.getLogger(__name__).info('research_scheduler %s', canonical({
                'component': 'research_scheduler', 'stage': 'confirmed_facts', 'scheduled': scheduled}))

    async def run_once(self) -> bool:
        self._schedule_identity_visual()
        self._schedule_confirmed_facts()
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
            if job["kind"] == "identity_visual":
                from .headless_identity import HeadlessIdentity
                await HeadlessIdentity(self).run(job)
            elif job["kind"] == "identity":
                handler = getattr(self, "_run_identity", None)
                if not callable(handler):
                    raise PermanentProviderError("Identity worker is unavailable")
                await handler(job)
            elif job["kind"] in {"research", "refinement"}:
                await self._run_research(job)
            elif job["kind"] == "visual":
                await self._run_visual(job)
            elif job["kind"] == "publish":
                await self._run_publish(job)
            else:
                raise PermanentProviderError(f"Unknown job kind {job['kind']}")
            with self.store.tx() as db:
                changed = db.execute("UPDATE jobs SET state='done',lease_until=0,last_error=NULL,updated_at=? WHERE id=? AND state='running' AND attempts=?", (self.store.now(), job["id"], job['attempts'])).rowcount
                if not changed:
                    return True
                if job['kind'] in {'research', 'refinement'}:
                    self._resume_joined_fact_request(db, job['story_id'])
        except RetryableProviderError as exc:
            reason = str(getattr(exc, 'code', None) or exc)
            retry_at = research_retry_at(reason, self.store.now(), exc.retry_at) if job['kind'] in RESEARCH_JOB_KINDS else exc.retry_at
            logging.getLogger('uvicorn.error').info('street_story_worker_waiting %s', canonical({
                'component': 'durable_worker', 'story_id': job['story_id'], 'job_id': job['id'],
                'kind': job['kind'], 'attempt': job['attempts'], 'error_type': type(exc).__name__,
                'reason': reason if re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', reason) else type(exc).__name__,
                'retry_at': retry_at,
            }))
            with self.store.tx() as db:
                if not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?", (job['id'], job['attempts'])).fetchone():
                    return True
                error = self.settings.redact(str(exc))
                if job["kind"] in {'identity', 'identity_visual', 'research', 'refinement'}:
                    db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=? AND state='running' AND attempts=?",
                        (retry_at, error,self.store.now(),job["id"], job['attempts']))
                    self._preserve_background_fact_value(db, job, error=reason, terminal=False)
                elif job["attempts"] >= MAX_JOB_ATTEMPTS:
                    self._fail_retry_exhausted(db, job, error)
                else:
                    backoff = min(300, 2 ** min(job["attempts"], 8))
                    available_at = max(self.store.now()+1, exc.retry_at) if exc.retry_at is not None else self.store.now()+backoff
                    db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=? AND state='running' AND attempts=?", (available_at, error, self.store.now(), job["id"], job['attempts']))
            return True
        except (PermanentProviderError, ConflictError, InvalidStateError) as exc:
            with self.store.tx() as db:
                changed = db.execute("UPDATE jobs SET state='failed',lease_until=0,last_error=?,updated_at=? WHERE id=? AND state='running' AND attempts=?", (self.settings.redact(str(exc)), self.store.now(), job["id"], job['attempts'])).rowcount
                if not changed:
                    return True
                if job["kind"] != "visual" and not self._preserve_background_fact_value(db, job, error=self.settings.redact(str(exc))):
                    db.execute("UPDATE stories SET state='needs_review',error_code=?,error_message=?,revision=revision+1,updated_at=? WHERE id=?", ("provider_permanent_error", "Не удалось выполнить обработку. Требуется проверка настроек сервиса.", self.store.now(), job["story_id"]))
                if job['kind'] in {'research', 'refinement'}:
                    self._resume_joined_fact_request(db, job['story_id'])
            return True
        except Exception as exc:
            import traceback
            logging.getLogger('uvicorn.error').error('street_story_worker_failure %s', canonical({
                'component': 'durable_worker', 'story_id': job['story_id'], 'job_id': job['id'],
                'kind': job['kind'], 'attempt': job['attempts'], 'error_type': type(exc).__name__,
                'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                           for frame in traceback.extract_tb(exc.__traceback__)[-6:]],
            }))
            with self.store.tx() as db:
                if not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?", (job['id'], job['attempts'])).fetchone():
                    return True
                error = f"worker_failure:{type(exc).__name__}"
                failure_attempts = job["attempts"]
                if job['kind'] in {'research', 'refinement'}:
                    # Queue claims include quota and lease waits. Only actual
                    # worker failures consume the research failure budget.
                    row = db.execute("SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage=?",
                                     (job['id'], 'worker_non_wait_failures')).fetchone()
                    failure_attempts = int(json.loads(row[0]).get('count', 0)) + 1 if row else 1
                    db.execute("INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?) "
                               "ON CONFLICT(job_id,stage) DO UPDATE SET value_json=excluded.value_json",
                               (job['id'], 'worker_non_wait_failures', canonical({'count': failure_attempts}), self.store.now()))
                    logging.getLogger('uvicorn.error').info('street_story_worker_failure_budget %s', canonical({
                        'component': 'durable_worker', 'story_id': job['story_id'], 'job_id': job['id'],
                        'kind': job['kind'], 'attempt': job['attempts'], 'failure_count': failure_attempts,
                        'error_type': type(exc).__name__,
                    }))
                if failure_attempts >= MAX_JOB_ATTEMPTS:
                    self._fail_retry_exhausted(db, job, error)
                else:
                    db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=? AND state='running' AND attempts=?", (self.store.now()+5, error, self.store.now(), job["id"], job['attempts']))
                    self._preserve_background_fact_value(db, job, error=error, terminal=False)
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
                self._source_photo_for_job(story_id), story["photo_mime_type"], transcript, osm, wikipedia, previous_facts
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
                source = {
                    "type": "wikipedia",
                    "title": str(page.get("title", url)),
                    "url": url,
                }
                extract = str(page.get("extract") or "").strip()
                if extract:
                    source["supports"] = [{
                        "kind": "wikipedia_extract",
                        "source_url": url.rstrip("/"),
                        "text": extract[:9000],
                    }]
                source_objects[url.rstrip("/")] = source
        for source in grounded.grounding_sources:
            source_objects[source["url"].rstrip("/")] = source
        incoming = [
            item
            for item in (grounded.payload.get("facts", []) or [])
            if isinstance(item, dict)
        ]
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
                "claim_key": item.get("claim_key"),
                "text": text,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
                "evidence_supported": bool(sources), "selected": bool(sources), "sources": sources,
            })
        with self.store.tx() as db:
            from .fact_ledger import persist_fact_candidates
            persist_fact_candidates(
                db,
                story_id=story_id,
                poi_key=None,
                facts=normalized,
                run_id=str(job["id"]),
                batch_id="legacy-grounded-research",
                model_name="legacy-research-model",
                prompt_version="legacy-research-ledger-v1",
                now=self.store.now(),
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

    async def asset(self, story_id: str) -> tuple[bytes, str]:
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            asset_ref = row['vibepublish_asset_ref']
            if not asset_ref:
                raise NotFoundError("processed asset not found")
        return await self.providers.vibepublish.read_asset(asset_ref)
