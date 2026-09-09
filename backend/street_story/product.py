from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, reveal
from .providers import PermanentProviderError, RetryableProviderError, infer_provider
from .service import (
    ConflictError,
    InvalidStateError,
    ProviderBundle,
    StreetStoryService,
    _durable_write,
    canonical,
    digest,
)


_VOCAL_FILLERS = re.compile(
    r"(?iu)(?<!\w)(?:э(?:[-–—]?э)+|ээ+|эм+|м(?:[-–—]?м)+|а(?:[-–—]?а)+)(?!\w)[,;:\s]*"
)
_FALSE_START = re.compile(r"(?iu)\b[а-яёa-z]{1,3}[-–—]\s+(?=[а-яёa-z])")
_REPEAT_WORD = re.compile(r"(?iu)\b([а-яёa-z0-9][а-яёa-z0-9-]*)\b(?:\s*[,;]?\s+\1\b)+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def normalize_display_text(raw: str) -> str:
    """Conservatively clean ASR disfluencies without inventing or summarising content."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _VOCAL_FILLERS.sub("", text)
    text = _FALSE_START.sub("", text)
    text = _REPEAT_WORD.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip(" ,;:\n\t")
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text or raw.strip()


def _provider(alias: str, label: str, *explicit: Any) -> str | None:
    for value in explicit:
        candidate = str(value or "").strip().lower()
        if candidate in {"telegram", "vk", "max"}:
            return candidate
    return infer_provider(alias, label)


def project_destinations_v2(bootstrap: dict[str, Any]) -> list[dict[str, Any]]:
    caps = {
        str(cap.get("destination")): cap
        for cap in bootstrap.get("capabilities", [])
        if cap.get("operation") == "publish" and cap.get("surface") == "post"
    }
    projected: list[dict[str, Any]] = []
    for item in bootstrap.get("destinations", []):
        if item.get("kind") != "destination":
            continue
        alias = str(item.get("alias", ""))
        label = str(item.get("label", ""))
        cap = caps.get(alias)
        if not cap:
            continue
        provider = _provider(alias, label, item.get("provider"), cap.get("provider"))
        if provider is None:
            continue
        status = str(cap.get("status", "unsupported"))
        normalized = f"{alias} {label}".lower().replace("ё", "е")
        primary = "полюбить калининград" in normalized and provider in {"telegram", "vk"}
        projected.append(
            {
                "alias": alias,
                "label": label,
                "provider": provider,
                "status": status,
                "capability_status": status,
                "selected": bool(primary and status in {"supported", "needs_review"}),
            }
        )
    return projected


def _receipt(payload: dict[str, Any], operation_id: str | None = None) -> dict[str, Any]:
    receipts = payload.get("receipts") if isinstance(payload, dict) else None
    if isinstance(receipts, list) and receipts:
        if operation_id:
            for item in receipts:
                if isinstance(item, dict) and item.get("operation_id") == operation_id:
                    return item
        if isinstance(receipts[0], dict):
            return receipts[0]
    return payload


def _deliveries(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    value = receipt.get("deliveries")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class VibePublishBoundary:
    """Exact HTTP boundary used by current VibePublish asset/visual/social runtime."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = http

    def configured(self) -> bool:
        return bool(self.settings.vibepublish_base_url and reveal(self.settings.vibepublish_bearer_token))

    def _headers(self, *, key: str | None = None, content_type: str | None = None) -> dict[str, str]:
        if not self.configured():
            raise PermanentProviderError("VibePublish runtime is not configured")
        headers = {
            "Authorization": f"Bearer {reveal(self.settings.vibepublish_bearer_token)}",
            "Accept": "application/json",
        }
        if key:
            headers["Idempotency-Key"] = key
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def _response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=35)
        try:
            response = await client.request(method, self.settings.vibepublish_base_url + path, **kwargs)
            if response.status_code >= 500 or response.status_code in {408, 425, 429}:
                raise RetryableProviderError(f"VibePublish HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except RetryableProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError(f"VibePublish response outcome unknown: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise PermanentProviderError(f"VibePublish request failed: HTTP {exc.response.status_code}") from exc
        finally:
            if own:
                await client.aclose()

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        response = await self._response(method, path, json=body, headers=self._headers(key=key))
        try:
            payload = response.json()
        except ValueError as exc:
            raise PermanentProviderError("VibePublish returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentProviderError("VibePublish returned a non-object response")
        return payload

    async def bootstrap(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/bootstrap")

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str) -> dict[str, Any]:
        response = await self._response(
            "POST",
            "/v1/assets",
            content=data,
            headers=self._headers(key=request_key, content_type=mime_type),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PermanentProviderError("VibePublish asset ingress returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentProviderError("VibePublish asset ingress returned a non-object response")
        return payload

    async def read_asset(self, asset_id: str) -> tuple[bytes, str]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", asset_id):
            raise PermanentProviderError("VibePublish asset id is invalid")
        response = await self._response("GET", f"/v1/assets/{asset_id}", headers=self._headers())
        return response.content, response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]

    async def visual(self, payload: dict[str, Any], request_key: str) -> dict[str, Any]:
        return await self._json("POST", "/v1/visuals/commands", body=payload, key=request_key)

    async def publish(self, payload: dict[str, Any], request_key: str) -> dict[str, Any]:
        return await self._json("POST", "/v1/publications", body=payload, key=request_key)

    async def status(self, operation_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/operations/{operation_id}")

    async def publication_update(
        self,
        publication_id: str,
        expected_revision: int,
        change: dict[str, Any],
        request_key: str,
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/v1/publications/{publication_id}/commands",
            body={"expected_revision": expected_revision, "change": change},
            key=request_key,
        )


class ProductStreetStoryService(StreetStoryService):
    """Product checkpoint service layered over the proven durable Street Story core."""

    def __init__(self, settings: Settings, providers: ProviderBundle | None = None):
        super().__init__(settings, providers)
        if providers is None:
            self.providers.vibepublish = VibePublishBoundary(settings)
        self._migrate_product_projection()

    def _migrate_product_projection(self) -> None:
        additions = {
            "voice_sessions": [("display_text", "TEXT")],
            "publish_intents": [
                ("vibepublish_publication_id", "TEXT"),
                ("vibepublish_revision", "INTEGER"),
                ("receipt_json", "TEXT"),
                ("cancel_operation_id", "TEXT"),
                ("cancel_request_key", "TEXT"),
            ],
        }
        with self.store.tx() as db:
            for table, columns in additions.items():
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, declaration in columns:
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            rows = list(
                db.execute(
                    "SELECT session_id,transcript FROM voice_sessions WHERE transcript IS NOT NULL AND display_text IS NULL"
                )
            )
            for row in rows:
                db.execute(
                    "UPDATE voice_sessions SET display_text=? WHERE session_id=?",
                    (normalize_display_text(row["transcript"]), row["session_id"]),
                )

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        messages: list[dict[str, Any]] = []
        for voice in db.execute(
            "SELECT session_id,kind,metadata_json,recording_finished,transcript,display_text,created_at,updated_at "
            "FROM voice_sessions WHERE story_id=? ORDER BY created_at,session_id",
            (row["id"],),
        ):
            if not voice["recording_finished"] and not voice["transcript"]:
                continue
            metadata = json.loads(voice["metadata_json"] or "{}")
            complete = metadata.get("complete") if isinstance(metadata.get("complete"), dict) else {}
            messages.append(
                {
                    "session_id": voice["session_id"],
                    "kind": voice["kind"],
                    "raw_transcript": voice["transcript"],
                    "display_text": voice["display_text"],
                    "started_at": metadata.get("started_at"),
                    "ended_at": complete.get("ended_at"),
                }
            )
        result["voice_messages"] = messages

        visual = json.loads(row["visual_context_json"] or "{}")
        safe_visual_keys = (
            "source_asset_ref",
            "operation_id",
            "visual_job_id",
            "candidate_id",
            "selected_asset_ref",
            "selected_sha256",
        )
        result["visual"] = {key: visual.get(key) for key in safe_visual_keys if visual.get(key) is not None}
        if result["visual"]:
            result["visual"]["state"] = row["state"]

        intent = db.execute(
            "SELECT * FROM publish_intents WHERE story_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        if intent:
            request = json.loads(intent["request_json"] or "{}")
            receipt = json.loads(intent["receipt_json"] or "{}")
            deliveries = _deliveries(receipt)
            by_alias: dict[str, dict[str, Any]] = {}
            for delivery in deliveries:
                alias = str(
                    delivery.get("destination")
                    or delivery.get("alias")
                    or delivery.get("destination_alias")
                    or ""
                )
                if alias:
                    by_alias[alias] = delivery
            projected = []
            for detail in request.get("destination_details", []):
                if not isinstance(detail, dict):
                    continue
                alias = str(detail.get("alias", ""))
                delivery = by_alias.get(alias, {})
                observed = str(delivery.get("observed") or intent["state"] or detail.get("status") or "unknown")
                projected.append(
                    {
                        "alias": alias,
                        "label": str(detail.get("label") or alias),
                        "provider": str(detail.get("provider") or "unknown"),
                        "status": observed,
                        "capability_status": str(detail.get("status") or "unknown"),
                        "selected": True,
                        "scheduled_for": request.get("scheduled_for"),
                    }
                )
            result["destinations"] = projected
            result["publication"] = {
                "state": intent["state"],
                "publication_id": intent["vibepublish_publication_id"],
                "revision": intent["vibepublish_revision"],
                "operation_id": intent["vibepublish_operation_id"],
                "cancel_operation_id": intent["cancel_operation_id"],
            }
        return result

    async def _transcribe_session(self, session_id: str) -> str:
        with self.store.connection() as db:
            session = db.execute("SELECT * FROM voice_sessions WHERE session_id=?", (session_id,)).fetchone()
            if session and session["transcript"] is not None:
                if session["display_text"] is None:
                    display = normalize_display_text(session["transcript"])
                    with self.store.tx() as write_db:
                        write_db.execute(
                            "UPDATE voice_sessions SET display_text=COALESCE(display_text,?),updated_at=? WHERE session_id=?",
                            (display, self.store.now(), session_id),
                        )
                return session["transcript"]
        raw = await super()._transcribe_session(session_id)
        display = normalize_display_text(raw)
        with self.store.tx() as db:
            db.execute(
                "UPDATE voice_sessions SET display_text=COALESCE(display_text,?),updated_at=? WHERE session_id=?",
                (display, self.store.now(), session_id),
            )
        return raw

    async def capabilities(self) -> dict[str, Any]:
        try:
            bootstrap = await self.providers.vibepublish.bootstrap()
        except (RetryableProviderError, PermanentProviderError):
            bootstrap = {"destinations": [], "capabilities": []}
        result: dict[str, Any] = {"destinations": project_destinations_v2(bootstrap)}
        pool = getattr(self.providers.gemini, "pool", None)
        if pool is not None:
            result["gemini"] = {
                operation: pool.snapshot(operation) for operation in ("transcription", "grounded_research")
            }
        return result

    @staticmethod
    def _visual_brief(story: dict[str, Any], context: dict[str, Any]) -> str:
        selected = context.get("selected_facts") if isinstance(context.get("selected_facts"), list) else []
        facts = "\n".join(f"- {item.get('text')}" for item in selected if isinstance(item, dict) and item.get("text"))
        return (
            "Create a polished editorial Street Story image from the supplied source photograph. "
            "Keep the photographed place recognisable and preserve the source as the visual anchor. "
            "Improve composition, atmosphere and photographic finish without adding text, logos or unsupported historical details. "
            "Use only the evidence-backed context below as optional visual context; do not invent facts.\n"
            f"Place: {story.get('place_name') or 'unknown'}\n"
            f"Owner intent: {context.get('user_voice_intent') or ''}\n"
            f"Evidence-backed facts:\n{facts}"
        )

    async def _run_visual(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        try:
            await self.providers.vibepublish.bootstrap()
            with self.store.connection() as db:
                story = dict(self._story_row(db, story_id))
            context = json.loads(story["visual_context_json"] or "{}")
            photo = Path(story["photo_path"]).read_bytes()
            ingress_key = "ss-vp-asset-" + hashlib.sha256(
                f"{story_id}:{story['photo_sha256']}".encode()
            ).hexdigest()[:48]
            ingress = await self.providers.vibepublish.ingress_asset(
                photo, story["photo_mime_type"], ingress_key
            )
            source_asset = str(ingress.get("asset_id") or "")
            source_sha = str(ingress.get("source_sha256") or "").lower()
            if not source_asset or source_sha != story["photo_sha256"].lower():
                raise PermanentProviderError("VibePublish asset ingress identity mismatch")

            command = {
                "command": {
                    "kind": "tune",
                    "source": {"source": {"kind": "asset", "id": source_asset}},
                    "brief": self._visual_brief(story, context),
                    "candidates": 1,
                    "formats": ["post_4_5"],
                    "selection": "human",
                    "copy": {},
                }
            }
            visual_key = "ss-vp-visual-" + hashlib.sha256(
                canonical([story_id, source_asset, command]).encode()
            ).hexdigest()[:48]
            receipt = await self.providers.vibepublish.visual(command, visual_key)
            operation_id = str(receipt.get("operation_id") or context.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish visual accepted no recoverable operation_id")
            current = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
            state = str(current.get("state") or receipt.get("state") or "")
            if state in {"accepted", "running", "queued", "processing"}:
                raise RetryableProviderError(f"VibePublish visual operation is {state}")
            if state in {"blocked", "failed", "outcome_unknown"}:
                raise PermanentProviderError(f"VibePublish visual operation ended in {state}")
            if state != "needs_selection":
                if state != "verified":
                    raise RetryableProviderError(f"VibePublish visual operation is {state or 'pending'}")
                selected = current
                candidate_id = context.get("candidate_id")
                visual_job_id = current.get("visual_job_id") or context.get("visual_job_id")
            else:
                candidates = current.get("candidates")
                if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
                    raise PermanentProviderError("Street Story requires exactly one VibePublish visual candidate")
                candidate = candidates[0]
                visual_job_id = str(current.get("visual_job_id") or "")
                revision = current.get("visual_revision")
                token = candidate.get("selection_token")
                candidate_id = str(candidate.get("id") or "")
                if not visual_job_id or not candidate_id or not isinstance(revision, int) or not isinstance(token, str):
                    raise PermanentProviderError("VibePublish visual selection receipt is incomplete")
                select_command = {
                    "command": {
                        "kind": "select",
                        "job_id": visual_job_id,
                        "candidate_id": candidate_id,
                        "expected_revision": revision,
                        "token": token,
                    }
                }
                select_key = "ss-vp-select-" + hashlib.sha256(
                    f"{operation_id}:{candidate_id}:{revision}".encode()
                ).hexdigest()[:48]
                selected = await self.providers.vibepublish.visual(select_command, select_key)
                if str(selected.get("operation_id") or "") != operation_id:
                    raise PermanentProviderError("VibePublish selected a different visual operation")
                if selected.get("state") != "verified":
                    selected = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
            if selected.get("state") != "verified":
                raise RetryableProviderError(
                    f"VibePublish selected visual is {selected.get('state') or 'pending'}"
                )
            selected_asset = str(selected.get("selected_asset_ref") or "")
            selected_sha = str(selected.get("selected_sha256") or "").lower()
            if not selected_asset or not re.fullmatch(r"[0-9a-f]{64}", selected_sha):
                raise PermanentProviderError("VibePublish verified visual lacks immutable asset identity")
            processed, mime = await self.providers.vibepublish.read_asset(selected_asset)
            if hashlib.sha256(processed).hexdigest() != selected_sha:
                raise PermanentProviderError("VibePublish processed asset readback hash mismatch")
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise PermanentProviderError("VibePublish processed asset has an unsupported mime type")
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            target = self.settings.data_dir / "stories" / story_id / f"processed{suffix}"
            _durable_write(target, processed)
            context.update(
                {
                    "source_asset_ref": source_asset,
                    "operation_id": operation_id,
                    "visual_job_id": visual_job_id,
                    "candidate_id": candidate_id,
                    "selected_asset_ref": selected_asset,
                    "selected_sha256": selected_sha,
                }
            )
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='ready_to_publish',processed_image_path=?,processed_image_url=?,"
                    "vibepublish_asset_ref=?,visual_context_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        str(target),
                        f"/v1/assets/{story_id}/processed",
                        selected_asset,
                        canonical(context),
                        self.store.now(),
                        story_id,
                    ),
                )
        except RetryableProviderError:
            raise
        except (PermanentProviderError, ConflictError) as exc:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='needs_review',error_code='vibepublish_visual_error',error_message=?,"
                    "revision=revision+1,updated_at=? WHERE id=?",
                    (self.settings.redact(str(exc)), self.store.now(), story_id),
                )

    async def _run_publish(self, job: dict[str, Any]) -> None:
        intent_id = json.loads(job["payload_json"])["intent_id"]
        with self.store.connection() as db:
            intent = dict(db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone())
        request = json.loads(intent["request_json"])
        bootstrap = await self.providers.vibepublish.bootstrap()
        projected = project_destinations_v2(bootstrap)
        by_alias = {item["alias"]: item for item in projected}
        requested_aliases = [str(value) for value in request["destinations"]]
        eligible = {
            alias
            for alias, item in by_alias.items()
            if item["status"] in {"supported", "needs_review"}
        }
        if not set(requested_aliases).issubset(eligible):
            raise PermanentProviderError("Requested destination is not eligible in current VibePublish bootstrap")
        if not request.get("destination_details"):
            request["destination_details"] = [by_alias[alias] for alias in requested_aliases]
            with self.store.tx() as db:
                db.execute("UPDATE publish_intents SET request_json=?,updated_at=? WHERE id=?", (canonical(request), self.store.now(), intent_id))

        operation_id = intent.get("vibepublish_operation_id")
        if not operation_id:
            payload = {
                "to": requested_aliases,
                "content": {"text": request.get("text") or "", "format": "plain"},
                "media": [{"source": {"kind": "asset", "id": request["asset_ref"]}, "role": "image"}],
                "surface": "post",
                "delivery": {"kind": "at", "at": request["scheduled_for"]},
                "mode": "execute",
                "routing_revision": bootstrap["routing_revision"],
            }
            receipt = await self.providers.vibepublish.publish(payload, intent["vibepublish_request_key"])
            operation_id = str(receipt.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish accepted no recoverable publication operation_id")
        else:
            receipt = await self.providers.vibepublish.status(operation_id)
        current = _receipt(receipt, operation_id)
        if current.get("operation_id") != operation_id:
            current = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
        state = str(current.get("state") or "")
        publication_id = str(current.get("resource_id") or intent.get("vibepublish_publication_id") or "") or None
        revision = current.get("revision") if isinstance(current.get("revision"), int) else intent.get("vibepublish_revision")
        with self.store.tx() as db:
            db.execute(
                "UPDATE publish_intents SET vibepublish_operation_id=?,vibepublish_publication_id=COALESCE(?,vibepublish_publication_id),"
                "vibepublish_revision=COALESCE(?,vibepublish_revision),receipt_json=?,state=?,updated_at=? WHERE id=?",
                (operation_id, publication_id, revision, canonical(current), state or "accepted", self.store.now(), intent_id),
            )
        if state in {"scheduled", "verified"}:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='scheduled',scheduled_for=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",
                    (request["scheduled_for"], self.store.now(), job["story_id"]),
                )
            return
        if state in {"failed", "blocked", "outcome_unknown", "cancelled"}:
            raise PermanentProviderError(f"VibePublish publication operation ended in {state}")
        raise RetryableProviderError(f"VibePublish publication operation is {state or 'pending'}")

    def mutate_cancel(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            intent = db.execute(
                "SELECT * FROM publish_intents WHERE story_id=? ORDER BY created_at DESC LIMIT 1", (story_id,)
            ).fetchone()
            if not intent or not intent["vibepublish_publication_id"] or intent["vibepublish_revision"] is None:
                raise InvalidStateError("publication_not_cancellable", "No scheduled VibePublish publication can be cancelled")
            self._idem(db, key, "cancel", req_digest, "publish_intent", intent["id"])
            cancel_key = "ss-vp-cancel-" + hashlib.sha256(f"{intent['id']}:{key}".encode()).hexdigest()[:48]
            db.execute(
                "UPDATE publish_intents SET cancel_request_key=COALESCE(cancel_request_key,?),state=CASE WHEN state='cancelled' THEN state ELSE 'cancel_pending' END,updated_at=? WHERE id=?",
                (cancel_key, self.store.now(), intent["id"]),
            )
            self._enqueue_job(db, story_id, "cancel", f"cancel:{intent['id']}", {"intent_id": intent["id"]})
            return self._story_repr(db, story)

    async def _run_cancel(self, job: dict[str, Any]) -> None:
        intent_id = json.loads(job["payload_json"])["intent_id"]
        with self.store.connection() as db:
            intent = dict(db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone())
        if intent["state"] == "cancelled":
            return
        operation_id = intent.get("cancel_operation_id")
        if not operation_id:
            receipt = await self.providers.vibepublish.publication_update(
                str(intent["vibepublish_publication_id"]),
                int(intent["vibepublish_revision"]),
                {"kind": "cancel"},
                str(intent["cancel_request_key"]),
            )
            operation_id = str(receipt.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish cancel accepted no recoverable operation_id")
        else:
            receipt = await self.providers.vibepublish.status(operation_id)
        current = _receipt(receipt, operation_id)
        if current.get("operation_id") != operation_id:
            current = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
        state = str(current.get("state") or "")
        revision = current.get("revision") if isinstance(current.get("revision"), int) else intent.get("vibepublish_revision")
        with self.store.tx() as db:
            db.execute(
                "UPDATE publish_intents SET cancel_operation_id=?,vibepublish_revision=COALESCE(?,vibepublish_revision),"
                "receipt_json=?,state=?,updated_at=? WHERE id=?",
                (operation_id, revision, canonical(current), state or "cancel_pending", self.store.now(), intent_id),
            )
        if state == "cancelled":
            return
        if state in {"failed", "blocked", "outcome_unknown"}:
            raise PermanentProviderError(f"VibePublish cancel operation ended in {state}")
        raise RetryableProviderError(f"VibePublish cancel operation is {state or 'pending'}")

    async def run_once(self) -> bool:
        with self.store.connection() as db:
            cancel_ready = db.execute(
                "SELECT 1 FROM jobs WHERE kind='cancel' AND ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?)) LIMIT 1",
                (self.store.now(), self.store.now()),
            ).fetchone()
        if not cancel_ready:
            return await super().run_once()
        now = self.store.now()
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE kind='cancel' AND ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return False
            db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?",
                (now + 90, now, row["id"]),
            )
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(15)
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND attempts=?",
                        (self.store.now() + 90, job["id"], job["attempts"]),
                    )

        lease = asyncio.create_task(heartbeat())
        try:
            await self._run_cancel(job)
            with self.store.tx() as db:
                db.execute(
                    "UPDATE jobs SET state='done',lease_until=0,last_error=NULL,updated_at=? WHERE id=?",
                    (self.store.now(), job["id"]),
                )
            return True
        except RetryableProviderError as exc:
            backoff = min(300, 2 ** min(job["attempts"], 8))
            available = max(self.store.now() + 1, exc.retry_at) if exc.retry_at is not None else self.store.now() + backoff
            with self.store.tx() as db:
                db.execute(
                    "UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? WHERE id=?",
                    (available, self.settings.redact(str(exc)), self.store.now(), job["id"]),
                )
            return True
        except PermanentProviderError as exc:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE jobs SET state='failed',lease_until=0,last_error=?,updated_at=? WHERE id=?",
                    (self.settings.redact(str(exc)), self.store.now(), job["id"]),
                )
                db.execute(
                    "UPDATE stories SET error_code='cancel_failed',error_message=?,revision=revision+1,updated_at=? WHERE id=?",
                    (self.settings.redact(str(exc)), self.store.now(), job["story_id"]),
                )
            return True
        finally:
            lease.cancel()
            await asyncio.gather(lease, return_exceptions=True)
