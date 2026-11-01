from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

import httpx

from .config import Settings, reveal
from .errors import ProviderRejected
from .providers import PermanentProviderError, RetryableProviderError, infer_provider
from .publication_content import publication_content, publication_plain_content
from .publication_slots import DEFAULT_DESTINATIONS
from .service import (
    ConflictError,
    InvalidStateError,
    ProviderBundle,
    StreetStoryService,
    canonical,
    digest,
)


LOG = logging.getLogger(__name__)

POST_4_5_FRAME_INSTRUCTION = (
    "\n\nOUTPUT FRAME: exact portrait 4:5 (1080x1350); "
    "no white bars, blank padding or borders. Fit the complete original photograph "
    "with uniform scaling only. Keep its camera angle, perspective, roofline, entrances "
    "and every architectural proportion. Never outpaint or invent surrounding streets, "
    "trees or buildings. Fill any remaining space with deliberate flat editorial graphic "
    "areas and legible fact annotations, not reconstructed scenery. "
    "Do not crop the subject, stretch it, correct perspective or clip labels. "
    "Verify the final image is 4:5 and the complete original camera frame is preserved."
)
POST_4_5_FRAME_CONTRACT = "post_4_5_v1"
ORIGINAL_PHOTO_FRAME_CONTRACT = 'source-original-frame-v1'
ORIGINAL_ART_FRAME_CONTRACT = 'original-art-aspect-v1'

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
        primary = alias in DEFAULT_DESTINATIONS
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
        if self.settings.vibepublish_http_host:
            headers["Host"] = self.settings.vibepublish_http_host
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
            raise ProviderRejected(f"VibePublish request failed: HTTP {exc.response.status_code}",
                                   exc.response.status_code) from exc
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

    def _enqueue_job(self, db, story_id, kind, semantic_key, payload):
        # Freeze the contract in the same transaction as the new durable intent.
        # Observation/recomposition of existing operations must keep its contract.
        row = self._story_row(db, story_id)
        context = json.loads(row["visual_context_json"] or "{}")
        if kind == 'visual' and not any(context.get(k) for k in (
                'output_frame_contract', 'operation_id', 'visual_command', 'recompose_source')):
            from .fact_ledger import fact_revision_bundle
            research = json.loads(row['research_json'] or '{}')
            ids = [item['fact_id'] for item in context.get('selected_facts', [])]
            context.update(output_frame_contract=POST_4_5_FRAME_CONTRACT,
                           source_photo_sha256=row['photo_sha256'],
                           identity_generation=int(research.get('identity_generation') or 0),
                           fact_revision_bundle=fact_revision_bundle(db, story_id, ids))
            context.setdefault('content_revision', digest(context))
            db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (canonical(context), story_id))
        elif kind == 'publish' and context.get('output_frame_contract'):
            intent = db.execute('SELECT * FROM publish_intents WHERE id=?', (payload['intent_id'],)).fetchone()
            request = json.loads(intent['request_json'])
            request['output_frame_contract'] = context['output_frame_contract']
            request['visual_content_revision'] = context.get('content_revision')
            try:
                self._guard_publish_frame(db, story_id, request, possibly_sent=False)
            except PermanentProviderError as exc:
                raise InvalidStateError('visual_format_unverified', str(exc)) from exc
            db.execute('UPDATE publish_intents SET request_json=? WHERE id=?',
                       (canonical(request), intent['id']))
        return super()._enqueue_job(db, story_id, kind, semantic_key, payload)

    def _frame_inputs_current(self, db, row, context):
        from .fact_ledger import eligibility_issues_for_ids, fact_revision_bundle
        research = json.loads(row['research_json'] or '{}')
        ids = [item['fact_id'] for item in context.get('selected_facts', [])]
        return (context.get('source_photo_sha256') == row['photo_sha256']
                and context.get('identity_generation') == int(research.get('identity_generation') or 0)
                and context.get('fact_revision_bundle') == fact_revision_bundle(db, row['id'], ids)
                and not eligibility_issues_for_ids(db, row['id'], ids))

    def _frame_work_current(self, db, row, context, job):
        latest = json.loads(row['visual_context_json'] or '{}')
        active = db.execute('SELECT state,attempts FROM jobs WHERE id=?', (job['id'],)).fetchone() if job else None
        return (latest.get('content_revision') == context.get('content_revision')
                and self._frame_inputs_current(db, row, context)
                and self._photo_format_current(row, context)
                and not (active and (active['state'] == 'cancelled' or active['attempts'] != job['attempts'])))

    def _photo_format_current(self, row, context):
        from .photo_format import choice
        current = choice(json.loads(row['research_json'] or '{}'))
        transform = context.get('source_image_transform')
        if transform is None:
            return current['revision'] == 0  # Historical frozen work is never rewritten.
        return (context.get('photo_format_revision') == current['revision']
            and transform.get('mode') == current['mode']
            and (current['mode'] == 'original' or transform.get('crop_rect') == current.get('crop_rect')))

    @staticmethod
    def _frame_dimensions_match(context, width, height):
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            return False
        if context.get('output_frame_contract') == POST_4_5_FRAME_CONTRACT:
            return 5*width == 4*height
        if context.get('output_frame_contract') == ORIGINAL_PHOTO_FRAME_CONTRACT:
            return [width, height] == (context.get('source_image_transform') or {}).get('output_size')
        if context.get('output_frame_contract') == ORIGINAL_ART_FRAME_CONTRACT:
            # Same proportion up to one output pixel on each side: an original with
            # coprime sides has no exact smaller frame, and the generator may round
            # both sides (1087x1447 for 3:4). Some exact-proportion frame w'xh' must
            # satisfy |width-w'| <= 1 and |height-h'| <= 1.
            size = (context.get('source_image_transform') or {}).get('output_size')
            return bool(size) and any(abs(height * size[0] - near * size[1]) <= size[0]
                                      for near in (width - 1, width, width + 1))
        return False

    def _record_visual_frame(self, story_id, context, receipt, *, candidate=None, job=None):
        if not context.get('output_frame_contract'):
            return context
        selected_asset = receipt.get('selected_asset_ref')
        selected_sha = receipt.get('selected_sha256')
        if candidate is None:
            candidates = receipt.get('candidates')
            matches = [item for item in (candidates if isinstance(candidates, list) else []) if isinstance(item, dict)
                       and item.get('asset_ref') == selected_asset and item.get('sha256') == selected_sha]
            if len(matches) == 1:
                candidate = matches[0]
            else:
                # A select response may omit candidates. Reuse only evidence from
                # the exact candidate checked before that same selection.
                candidate = context.get('output_frame_evidence') or (
                    candidates[0] if isinstance(candidates, list) and len(candidates) == 1
                    and isinstance(candidates[0], dict) else {})
        width, height = candidate.get('width'), candidate.get('height')
        reason = None
        if receipt.get('operation_id') != context.get('operation_id'):
            reason = 'visual_format_operation_mismatch'
        elif (context.get('output_frame_contract') not in {POST_4_5_FRAME_CONTRACT, ORIGINAL_PHOTO_FRAME_CONTRACT, ORIGINAL_ART_FRAME_CONTRACT}
                or not candidate.get('candidate_id', candidate.get('id'))
                or not candidate.get('asset_ref')
                or not re.fullmatch(r'[a-f0-9]{64}', str(candidate.get('sha256') or ''))):
            reason = 'visual_format_candidate_missing'
        elif selected_asset and (candidate.get('asset_ref') != selected_asset
                                 or candidate.get('sha256') != selected_sha):
            reason = 'visual_format_asset_mismatch'
        elif type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            reason = 'visual_format_dimensions_missing'
        elif not self._frame_dimensions_match(context, width, height):
            reason = 'visual_format_mismatch'
        proof = {
            'contract': context['output_frame_contract'], 'state': 'rejected' if reason else 'verified',
            'reason': reason, 'operation_id': str(context.get('operation_id') or receipt.get('operation_id') or '')[:160],
            'observed_operation_id': str(receipt.get('operation_id') or '')[:160],
            'candidate_id': str(candidate.get('candidate_id', candidate.get('id')) or '')[:160],
            'asset_ref': str(candidate.get('asset_ref') or '')[:160],
            'sha256': str(candidate.get('sha256') or '')[:64],
            'width': width if type(width) is int else None, 'height': height if type(height) is int else None,
            'observed_format': str(candidate.get('format') or candidate.get('observed_format') or '')[:40],
            'source_photo_sha256': context.get('source_photo_sha256'),
            'identity_generation': context.get('identity_generation'),
            'content_revision': context.get('content_revision'),
            'fact_revision_bundle': context.get('fact_revision_bundle'),
        }
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            latest = json.loads(row['visual_context_json'] or '{}')
            if not self._frame_work_current(db, row, context, job):
                return None
            latest['output_frame_evidence'] = proof
            if reason:
                db.execute("UPDATE stories SET visual_context_json=?,state='needs_review',error_code=?,"
                           "error_message=?,processed_image_url=NULL,vibepublish_asset_ref=NULL,"
                           'revision=revision+1,updated_at=? WHERE id=?',
                           (canonical(latest), reason, 'Изображение отклонено: его формат не совпал с выбранным. '
                            'Можно создать изображение заново.' if reason == 'visual_format_mismatch'
                            else 'Визуал не соответствует выбранному формату. Нужна проверка.',
                            self.store.now(), story_id))
            else:
                db.execute('UPDATE stories SET visual_context_json=?,updated_at=? WHERE id=?',
                           (canonical(latest), self.store.now(), story_id))
        LOG.info('street_story_visual_frame story_id=%s operation_id=%s state=%s reason=%s width=%s height=%s',
                 story_id, proof['operation_id'], proof['state'], reason, width, height)
        return None if reason else latest

    def _guard_publish_frame(self, db, story_id, request, *, possibly_sent):
        if possibly_sent:
            return  # Preserve the original uncertain dispatch/readback contract.
        row = self._story_row(db, story_id)
        context = json.loads(row['visual_context_json'] or '{}')
        contract = request.get('output_frame_contract')
        if not contract:
            return
        proof = context.get('output_frame_evidence') or {}
        if (contract not in {POST_4_5_FRAME_CONTRACT, ORIGINAL_PHOTO_FRAME_CONTRACT, ORIGINAL_ART_FRAME_CONTRACT} or context.get('output_frame_contract') != contract
                or proof.get('state') != 'verified' or proof.get('contract') != contract
                or proof.get('content_revision') != context.get('content_revision')
                or request.get('visual_content_revision', context.get('content_revision')) != context.get('content_revision')
                or proof.get('source_photo_sha256') != context.get('source_photo_sha256')
                or proof.get('identity_generation') != context.get('identity_generation')
                or proof.get('fact_revision_bundle') != context.get('fact_revision_bundle')
                or proof.get('operation_id') != context.get('operation_id')
                or proof.get('candidate_id') != context.get('candidate_id')
                or proof.get('asset_ref') != context.get('selected_asset_ref')
                or proof.get('sha256') != context.get('selected_sha256')
                or proof.get('asset_ref') != request.get('asset_ref')
                or proof.get('asset_ref') != row['vibepublish_asset_ref']
                or type(proof.get('width')) is not int or type(proof.get('height')) is not int
                or not self._frame_dimensions_match(context, proof['width'], proof['height'])
                or not self._frame_inputs_current(db, row, context) or not self._photo_format_current(row, context)):
            LOG.warning('street_story_publish_frame_blocked story_id=%s', story_id)
            raise PermanentProviderError('visual_format_unverified: Сначала нужен проверенный визуал выбранного формата.')

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

    def _story_repr(self, db, row, *, research=None) -> dict[str, Any]:
        if research is None:
            research = json.loads(row["research_json"] or "{}")
        result = super()._story_repr(db, row, research=research)
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

        live_rows = list(db.execute(
            "SELECT message_key,session_id,role,text,final,created_at,updated_at "
            "FROM live_messages WHERE story_id=? ORDER BY id DESC LIMIT 100",
            (row["id"],),
        ))
        live_rows.reverse()
        result["live_messages"] = [
            {
                "message_id": str(item["message_key"]),
                "session_id": str(item["session_id"]),
                "role": str(item["role"]),
                "text": str(item["text"]),
                "final": bool(item["final"]),
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for item in live_rows
        ]

        visual = json.loads(row["visual_context_json"] or "{}")
        safe_visual_keys = (
            "source_asset_ref",
            "operation_id",
            "visual_job_id",
            "candidate_id",
            "selected_asset_ref",
            "selected_sha256",
            "output_frame_contract",
            "output_frame_evidence",
            "source_frame",
        )
        result["visual"] = {key: visual.get(key) for key in safe_visual_keys if visual.get(key) is not None}
        if result["visual"]:
            result["visual"]["state"] = row["state"]

        # A logical publication may consist of two provider-native deliveries.
        intents = list(db.execute(
            "SELECT * FROM publish_intents WHERE story_id=? ORDER BY created_at DESC,rowid DESC", (row["id"],)
        ))
        if intents:
            newest = intents[0]
            group = json.loads(newest["request_json"] or "{}").get("publication_group")
            members = ([item for item in intents
                        if json.loads(item["request_json"] or "{}").get("publication_group") == group]
                       if group else [newest])
            projected, targets = [], []
            for intent in reversed(members):
                request = json.loads(intent["request_json"] or "{}")
                receipt = json.loads(intent["receipt_json"] or "{}")
                deliveries = _deliveries(receipt)
                by_alias = {str(item.get("destination") or item.get("alias") or item.get("destination_alias") or ""): item
                            for item in deliveries}
                for detail in request.get("destination_details", []):
                    if not isinstance(detail, dict):
                        continue
                    alias = str(detail.get("alias") or "")
                    delivery = by_alias.get(alias, {})
                    projected.append({
                        "alias": alias,
                        "label": str(detail.get("label") or alias),
                        "provider": str(detail.get("provider") or "unknown"),
                        "status": str(delivery.get("observed") or intent["state"] or detail.get("status") or "unknown"),
                        "capability_status": str(detail.get("status") or "unknown"),
                        "selected": True,
                        "scheduled_for": request.get("scheduled_for"),
                    })
                targets.append({
                    "destinations": request.get("destinations"),
                    "state": intent["state"],
                    "publication_id": intent["vibepublish_publication_id"],
                    "revision": intent["vibepublish_revision"],
                    "operation_id": intent["vibepublish_operation_id"],
                    "scheduled_for": request.get("scheduled_for"),
                    "edit": receipt.get("_publication_edit"),
                })
            result["destinations"] = projected
            result["publication"] = {
                "state": ("scheduled" if all(item["state"] in {"scheduled", "verified"} for item in targets)
                          else "scheduling" if any(item["state"] in {"pending", "accepted", "running", "scheduled"} for item in targets)
                          else newest["state"]),
                "publication_id": newest["vibepublish_publication_id"],
                "revision": newest["vibepublish_revision"],
                "operation_id": newest["vibepublish_operation_id"],
                "cancel_operation_id": newest["cancel_operation_id"],
                "targets": targets,
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
        projected = project_destinations_v2(bootstrap)
        result: dict[str, Any] = {
            "destinations": [
                item for item in projected
                if item["provider"] in {"telegram", "vk"}
                and item["status"] in {"supported", "needs_review"}
            ]
        }
        pool = getattr(self.providers.gemini, "pool", None)
        if pool is not None:
            result["gemini"] = {
                operation: pool.snapshot(operation) for operation in ("transcription", "grounded_research")
            }
        return result

    async def _publication_bootstrap(self, aliases: list[str]) -> dict[str, Any]:
        """Refresh Telegram proof; VK validates native rights at dispatch."""
        bootstrap = await self.providers.vibepublish.bootstrap()
        projected = project_destinations_v2(bootstrap)
        by_alias = {item["alias"]: item for item in projected}
        reviewable: list[str] = []
        for alias in aliases:
            item = by_alias.get(alias)
            if not item or item["provider"] not in {"telegram", "vk"}:
                raise PermanentProviderError("Requested destination is not a configured Telegram/VK destination")
            status = str(item.get("status") or "")
            if status == "supported" or (item["provider"] == "vk" and status == "needs_review"):
                continue  # VK wall.post verifies native rights at execution.
            if status != "needs_review":
                raise PermanentProviderError(
                    f"Requested destination capability is {status or 'unavailable'}"
                )
            reviewable.append(alias)

        for alias in reviewable:
            bucket = int(self.store.now() // 1800)
            request_key = "ss-vp-preview-" + hashlib.sha256(
                f"{alias}:{bucket}".encode()
            ).hexdigest()[:48]
            marker = (
                f"Street Story capability preflight {bucket}. "
                "Preview only; do not dispatch."
            )
            receipt = await self.providers.vibepublish.publish(
                {
                    "to": [alias],
                    "content": {"text": marker},
                    "mode": "preview",
                },
                request_key,
            )
            operation_id = str(receipt.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish preview returned no recoverable operation_id")

            current = _receipt(receipt, operation_id)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 120
            while current.get("operation_complete") is not True:
                if current.get('state') == 'needs_approval' and current.get('operation_complete') is False:
                    raise PermanentProviderError('VibePublish Telegram preview did not prove no-dispatch capability')
                if loop.time() >= deadline:
                    raise RetryableProviderError("VibePublish Telegram preview preflight timed out")
                await asyncio.sleep(0.5)
                current = _receipt(
                    await self.providers.vibepublish.status(operation_id),
                    operation_id,
                )

            state = str(current.get("state") or "")
            deliveries = _deliveries(current)
            matches = [
                row
                for row in deliveries
                if str(row.get("destination") or "") == alias
                and str(row.get("provider") or "").lower() == "telegram"
            ]
            # Current VibePublish native preview responds with needs_approval,
            # a review token, and an explicit not_attempted delivery, but does
            # not expose worker_seen_at/dry_run on that receipt variant.
            # Accept only this exact zero-dispatch proof (or the older dry-run
            # receipt); afterwards the fresh bootstrap must say supported.
            no_dispatch = (
                state == "needs_approval"
                and current.get("operation_complete") is True
                and bool(str(current.get("review_token") or "").strip())
                and len(matches) == 1
                and str(matches[0].get("state") or "") == "needs_approval"
                and str(matches[0].get("observed") or "") == "not_attempted"
            )
            legacy_dry_run = (
                state in {"needs_approval", "verified"}
                and current.get("dry_run") is True
                and bool(str(current.get("worker_seen_at") or "").strip())
                and len(matches) == 1
                and str(matches[0].get("state") or "") in {"needs_approval", "verified"}
                and str(matches[0].get("observed") or "") == "not_attempted"
            )
            if not (no_dispatch or legacy_dry_run):
                raise PermanentProviderError(
                    "VibePublish Telegram preview did not prove no-dispatch capability"
                )

        if reviewable:
            bootstrap = await self.providers.vibepublish.bootstrap()
            refreshed = {item["alias"]: item for item in project_destinations_v2(bootstrap)}
            if any(
                alias not in refreshed
                or refreshed[alias]["provider"] != "telegram"
                or refreshed[alias]["status"] != "supported"
                for alias in aliases
            ):
                raise PermanentProviderError(
                    "VibePublish Telegram capability did not become supported after preview"
                )
        return bootstrap

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
            + POST_4_5_FRAME_INSTRUCTION
        )

    async def _run_visual(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        try:
            await self.providers.vibepublish.bootstrap()
            with self.store.connection() as db:
                story = dict(self._story_row(db, story_id))
            context = json.loads(story["visual_context_json"] or "{}")
            photo = self._source_photo_for_job(story_id)
            ingress_key = "ss-vp-asset-" + hashlib.sha256(
                f"{story_id}:{story['photo_sha256']}".encode()
            ).hexdigest()[:48]
            ingress = await self.providers.vibepublish.ingress_asset(
                photo, story["photo_mime_type"], ingress_key
            )
            source_asset = str(ingress.get("asset_id") or "")
            if not source_asset:
                raise PermanentProviderError("VibePublish asset ingress returned no asset ID")

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
            if context.get('output_frame_contract'):
                with self.store.tx() as db:
                    if not self._frame_work_current(db, self._story_row(db, story_id), context, job):
                        return
                    context.update(visual_command=command, visual_request_key=visual_key, source_asset_ref=source_asset)
                    db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (canonical(context), story_id))
            operation_id = context.get('operation_id')
            receipt = (await self.providers.vibepublish.status(operation_id) if operation_id
                       else await self.providers.vibepublish.visual(command, visual_key))
            operation_id = str(receipt.get("operation_id") or context.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish visual accepted no recoverable operation_id")
            if context.get('output_frame_contract'):
                with self.store.tx() as db:
                    if not self._frame_work_current(db, self._story_row(db, story_id), context, job):
                        return
                    context['operation_id'] = operation_id
                    db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (canonical(context), story_id))
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
                context = self._record_visual_frame(story_id, context, current, candidate=candidate, job=job)
                if context is None:
                    return
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
            if not selected_asset:
                raise PermanentProviderError("VibePublish verified visual lacks immutable asset identity")
            context = self._record_visual_frame(story_id, context, selected, job=job)
            if context is None:
                return
            candidate_id = candidate_id or (context.get('output_frame_evidence') or {}).get('candidate_id')
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
                if context.get('output_frame_contract') and not self._frame_work_current(
                        db, self._story_row(db, story_id), context, job):
                    return
                db.execute(
                    "UPDATE stories SET state=CASE WHEN state IN ('scheduling','scheduled','published') "
                    "THEN state ELSE 'ready_to_publish' END,processed_image_path=?,processed_image_url=?,"
                    "vibepublish_asset_ref=?,visual_context_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        None,
                        f"/v1/assets/{story_id}/processed",
                        selected_asset,
                        canonical(context),
                        self.store.now(),
                        story_id,
                    ),
                )
                self._queue_publication_edits(
                    db, story_id, text=str(self._story_row(db, story_id)["draft_text"] or ""),
                    asset_ref=selected_asset, change_id=f"visual:{operation_id}:{selected_asset}",
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

    def _publish_identity_conflicted(self, db, story_id: str) -> bool:
        row = self._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        return (row['error_code'] == 'visual_identity_conflict'
                or bool(research.get('visual_identity_conflict'))
                or identity.get('status') in {'uncertain', 'mismatch', 'conflict'})

    def _guard_publish_identity(self, db, story_id: str, *, possibly_sent: bool) -> None:
        if self._publish_identity_conflicted(db, story_id):
            LOG.warning('street_story_publish_identity_blocked story_id=%s possibly_sent=%s',
                        story_id, possibly_sent)
            # Without an operation ID a same-key POST might be the first send:
            # the durable intent may precede a crash at the HTTP boundary.
            if possibly_sent:
                raise RetryableProviderError('publication_identity_conflict_outcome_unknown',
                                             retry_at=self.store.now()+300)
            raise PermanentProviderError('publication_identity_conflict_before_send')

    async def _run_publish(self, job: dict[str, Any]) -> None:
        intent_id = json.loads(job["payload_json"])["intent_id"]
        with self.store.connection() as db:
            intent = dict(db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone())
            dispatch = (json.loads(intent['receipt_json'] or '{}').get('_publish_dispatch') or {})
            operation_id = intent.get('vibepublish_operation_id')
            if not operation_id:
                self._guard_publish_identity(db, job['story_id'], possibly_sent=(dispatch.get('phase') == 'possibly_sent'
                    or not dispatch and int(job.get('attempts') or 0) > 1))
                self._guard_publish_frame(db, job['story_id'], json.loads(intent['request_json']),
                    possibly_sent=(dispatch.get('phase') == 'possibly_sent'))
        request = json.loads(intent["request_json"])
        if not operation_id:
            if dispatch.get('phase') == 'possibly_sent':
                if dispatch.get('request_key') != intent['vibepublish_request_key'] or not isinstance(dispatch.get('payload'), dict):
                    raise RetryableProviderError('publication_dispatch_binding_unknown', retry_at=self.store.now()+300)
                payload = dispatch['payload']
            else:
                requested_aliases = [str(value) for value in request["destinations"]]
                bootstrap = await self._publication_bootstrap(requested_aliases)
                projected = project_destinations_v2(bootstrap)
                by_alias = {item["alias"]: item for item in projected}
                eligible = {alias for alias, item in by_alias.items()
                            if item["provider"] in {"telegram", "vk"}
                            and (item["status"] == "supported" or
                                 (item["provider"] == "vk" and item["status"] == "needs_review"))}
                if not set(requested_aliases).issubset(eligible):
                    raise PermanentProviderError("Requested destination is not eligible in current VibePublish bootstrap")
                if not request.get("destination_details"):
                    request["destination_details"] = [by_alias[alias] for alias in requested_aliases]
                    with self.store.tx() as db:
                        db.execute("UPDATE publish_intents SET request_json=?,updated_at=? WHERE id=?",
                                   (canonical(request), self.store.now(), intent_id))
                payload = {
                    "to": requested_aliases,
                    "content": publication_content(request.get("text") or ""),
                    "media": [{"source": {"kind": "asset", "id": asset}, "role": "image"}
                              for asset in request.get('asset_refs') or [request['asset_ref']]],
                    "surface": "post",
                    "delivery": {"kind": "at", "at": request["scheduled_for"]},
                    "mode": "execute",
                    "routing_revision": bootstrap["routing_revision"],
                }
                if any(by_alias[alias]["provider"] == "vk" for alias in requested_aliases):
                    # VibePublish requires explicit plain semantic fallback on VK.
                    # Telegram retains structured clickable named links.
                    payload["renderings"] = {"vk": publication_plain_content(request.get("text") or "")}
            with self.store.tx() as db:
                # Final check and durable intent share a transaction. Conflict
                # arising during asynchronous bootstrap cannot slip past it.
                fresh = dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (intent_id,)).fetchone())
                operation_id = fresh.get('vibepublish_operation_id')
                if not operation_id:
                    prior = json.loads(fresh['receipt_json'] or '{}')
                    existing = prior.get('_publish_dispatch') or {}
                    self._guard_publish_identity(db, job['story_id'], possibly_sent=(existing.get('phase') == 'possibly_sent'
                        or not existing and int(job.get('attempts') or 0) > 1))
                    self._guard_publish_frame(db, job['story_id'], json.loads(fresh['request_json']),
                        possibly_sent=(existing.get('phase') == 'possibly_sent'))
                    if existing.get('phase') == 'possibly_sent':
                        if existing.get('request_key') != intent['vibepublish_request_key'] or not isinstance(existing.get('payload'), dict):
                            raise RetryableProviderError('publication_dispatch_binding_unknown', retry_at=self.store.now()+300)
                        dispatch, payload = existing, existing['payload']
                    else:
                        dispatch = {'phase': 'possibly_sent', 'request_key': intent['vibepublish_request_key'],
                                    'payload': payload, 'recorded_at': self.store.now()}
                    db.execute('UPDATE publish_intents SET receipt_json=?,updated_at=? WHERE id=?',
                               (canonical({**prior, '_publish_dispatch': dispatch}), self.store.now(), intent_id))
            if not operation_id:
                # Persist before awaiting the boundary; timeout/cancellation
                # never converts this marker into a known-unsent request.
                LOG.info('street_story_publish_dispatch story_id=%s intent_id=%s phase=possibly_sent',
                         job['story_id'], intent_id)
                receipt = await self.providers.vibepublish.publish(payload, intent["vibepublish_request_key"])
                operation_id = str(receipt.get("operation_id") or "")
                if not operation_id:
                    raise RetryableProviderError("VibePublish accepted no recoverable publication operation_id")
            else:
                receipt = await self.providers.vibepublish.status(operation_id)
        else:
            # Existing operations remain observable despite conflict or changed
            # destination capability. This branch never executes a POST.
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
                (operation_id, publication_id, revision, canonical({**current, **({'_publish_dispatch': dispatch} if dispatch else {})}),
                 state or "accepted", self.store.now(), intent_id),
            )
        if state in {"scheduled", "verified"}:
            with self.store.tx() as db:
                if not self._publish_identity_conflicted(db, job['story_id']):
                    db.execute(
                        "UPDATE stories SET state='scheduled',scheduled_for=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",
                        (request["scheduled_for"], self.store.now(), job["story_id"]),
                    )
            return
        if state in {"failed", "blocked", "outcome_unknown", "cancelled"}:
            errors = [current.get('error') or {}] + [item.get('error') or {}
                for item in _deliveries(current)]
            codes = {str(error.get('code') or '') for error in errors if isinstance(error, dict)}
            codes.update(str(code) for item in _deliveries(current)
                for code in (item.get('missing_checks') or []))
            if 'provider_text_limit' in codes:
                raise PermanentProviderError('telegram_caption_too_long')
            raise PermanentProviderError(f"VibePublish publication operation ended in {state}")
        raise RetryableProviderError(f"VibePublish publication operation is {state or 'pending'}")

    def _queue_publication_edits(self, db, story_id: str, *, text: str,
                                 asset_ref: str, change_id: str) -> None:
        """Reuse the durable job table; each provider publication is edited in place."""
        for intent in db.execute(
                "SELECT id,state FROM publish_intents WHERE story_id=?", (story_id,)):
            if intent["state"] in {"cancelled", "deleted"}:
                continue
            self._enqueue_job(
                db, story_id, "publication_edit",
                f"publication_edit:{intent['id']}:{change_id}",
                {"intent_id": intent["id"], "text": text, "asset_ref": asset_ref,
                 "asset_refs": [card['asset_ref'] for card in (
                     (json.loads(self._story_row(db, story_id)['visual_context_json'] or '{}').get('comic') or {}).get('cards') or [])]
                     or [asset_ref]},
            )

    async def _run_publication_edit(self, job: dict[str, Any]) -> None:
        """Idempotent CAS update; unknown network outcomes retain their original key."""
        payload = json.loads(job["payload_json"])
        intent_id = payload["intent_id"]
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                return
            intent = dict(row)
            earlier = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='publication_edit' AND id<>? "
                "AND state NOT IN ('done','cancelled') AND created_at<? "
                "AND json_extract(payload_json,'$.intent_id')=? LIMIT 1",
                (job["story_id"], job["id"], job["created_at"], intent_id),
            ).fetchone()
        if earlier:
            raise RetryableProviderError("Earlier provider edit has not completed",
                                         retry_at=self.store.now() + 10)
        if intent["state"] in {"cancelled", "deleted", "failed", "blocked"}:
            raise PermanentProviderError(f"Publication cannot be edited in state {intent['state']}")
        if not intent.get("vibepublish_publication_id") or intent.get("vibepublish_revision") is None:
            raise RetryableProviderError("Waiting for native publication receipt",
                                         retry_at=self.store.now() + 15)
        old_request = json.loads(intent["request_json"])
        receipt_data = json.loads(intent.get("receipt_json") or "{}")
        key = "ss-vp-edit-" + hashlib.sha256(
            f"{intent_id}:{job['id']}".encode()).hexdigest()[:48]
        marker = receipt_data.get("_publication_edit") or {}
        if marker.get("key") != key:
            change = {"kind": "edit"}
            if payload["text"] != old_request.get("text"):
                is_vk = DEFAULT_DESTINATIONS[1] in old_request.get("destinations", []) or any(
                    detail.get("provider") == "vk"
                    for detail in old_request.get("destination_details", [])
                    if isinstance(detail, dict)
                )
                change["content"] = (publication_plain_content(payload["text"]) if is_vk
                                     else publication_content(payload["text"]))
            new_refs = payload.get('asset_refs') or [payload['asset_ref']]
            old_refs = old_request.get('asset_refs') or [old_request.get('asset_ref')]
            is_telegram_album = len(old_refs) > 1 and (
                any(str(alias).startswith('tg_') for alias in old_request.get('destinations', []))
                or any(detail.get('provider') == 'telegram' for detail in
                       old_request.get('destination_details', []) if isinstance(detail, dict)))
            if new_refs != old_refs and is_telegram_album:
                # VibePublish cannot replace several members of a scheduled TG album.
                # Preserve its native IDs; text-only edits still use the same operation.
                with self.store.tx() as db:
                    db.execute('UPDATE stories SET error_code=?,error_message=?,updated_at=? WHERE id=?',
                               ('telegram_album_edit_needs_review',
                                'Картинки запланированного альбома не обновляются. '
                                'Отмените публикацию и опубликуйте заново.',
                                self.store.now(), job['story_id']))
                new_refs = old_refs
            if new_refs != old_refs:
                change["media"] = [
                    {"source": {"kind": "asset", "id": asset}, "role": "image"} for asset in new_refs
                ]
            if len(change) == 1:
                return
            if marker and marker.get("phase") != "verified":
                raise RetryableProviderError("An earlier provider edit has an unresolved outcome",
                                             retry_at=self.store.now() + 60)
            marker = {"key": key, "phase": "possibly_sent", "change": change,
                      "expected_revision": intent["vibepublish_revision"]}
            with self.store.tx() as db:
                current = dict(db.execute(
                    "SELECT receipt_json FROM publish_intents WHERE id=?", (intent_id,)).fetchone())
                saved = json.loads(current["receipt_json"] or "{}")
                previous = saved.get("_publication_edit") or {}
                if previous and previous.get("phase") != "verified":
                    raise RetryableProviderError("Another edit is not yet reconciled",
                                                 retry_at=self.store.now() + 30)
                db.execute("UPDATE publish_intents SET receipt_json=?,updated_at=? WHERE id=?",
                           (canonical({**saved, "_publication_edit": marker}), self.store.now(), intent_id))

        operation_id = marker.get("operation_id")
        if not operation_id:
            response = await self.providers.vibepublish.publication_update(
                str(intent["vibepublish_publication_id"]),
                int(marker["expected_revision"]),
                marker["change"], key,
            )
            operation_id = str(response.get("operation_id") or "")
            if not operation_id:
                raise RetryableProviderError("VibePublish edit accepted without operation ID")
            marker = {**marker, "operation_id": operation_id}
            with self.store.tx() as db:
                saved = json.loads(db.execute(
                    "SELECT receipt_json FROM publish_intents WHERE id=?", (intent_id,)
                ).fetchone()["receipt_json"] or "{}")
                db.execute("UPDATE publish_intents SET receipt_json=?,updated_at=? WHERE id=?",
                           (canonical({**saved, "_publication_edit": marker}), self.store.now(), intent_id))

        current = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
        state = str(current.get("state") or "")
        # A native scheduled post stays "scheduled" after its edit is applied; published posts verify.
        if current.get("operation_complete") is True and state in {"scheduled", "verified"}:
            verified_refs = ([item['source']['id'] for item in marker['change']['media']]
                             if 'media' in marker['change'] else
                             old_request.get('asset_refs') or [old_request['asset_ref']])
            updated = {**old_request, "text": payload["text"], "asset_ref": verified_refs[0],
                       "asset_refs": verified_refs}
            with self.store.tx() as db:
                saved = json.loads(db.execute(
                    "SELECT receipt_json FROM publish_intents WHERE id=?", (intent_id,)
                ).fetchone()["receipt_json"] or "{}")
                verified_marker = {**marker, "phase": "verified", "receipt": current}
                db.execute(
                    "UPDATE publish_intents SET request_json=?,receipt_json=?,"
                    "vibepublish_revision=COALESCE(?,vibepublish_revision),updated_at=? WHERE id=?",
                    (canonical(updated),
                     canonical({**saved, **({"deliveries": _deliveries(current)}
                                            if _deliveries(current) else {}),
                                "_publication_edit": verified_marker}),
                     current.get("revision") if isinstance(current.get("revision"), int) else None,
                     self.store.now(), intent_id),
                )
            return
        if state in {"blocked", "failed", "outcome_unknown", "cancelled"}:
            raise PermanentProviderError(f"VibePublish edit ended in {state}")
        raise RetryableProviderError(f"VibePublish edit is {state or 'pending'}",
                                     retry_at=self.store.now() + 10)

    def mutate_cancel(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            rows = list(db.execute(
                "SELECT * FROM publish_intents WHERE story_id=? ORDER BY created_at DESC,rowid DESC", (story_id,)
            ))
            if not rows:
                raise InvalidStateError("publication_not_cancellable", "No scheduled VibePublish publication can be cancelled")
            group = json.loads(rows[0]["request_json"] or "{}").get("publication_group")
            members = ([item for item in rows
                        if json.loads(item["request_json"] or "{}").get("publication_group") == group]
                       if group else [rows[0]])
            if any(not item["vibepublish_publication_id"] or item["vibepublish_revision"] is None
                   for item in members):
                raise InvalidStateError("publication_not_cancellable", "Both scheduled channels must be ready before cancellation")
            self._idem(db, key, "cancel", req_digest, "publish_intent", rows[0]["id"])
            for intent in members:
                cancel_key = "ss-vp-cancel-" + hashlib.sha256(f"{intent['id']}:{key}".encode()).hexdigest()[:48]
                db.execute(
                    "UPDATE publish_intents SET cancel_request_key=COALESCE(cancel_request_key,?),"
                    "state=CASE WHEN state='cancelled' THEN state ELSE 'cancel_pending' END,updated_at=? WHERE id=?",
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

    async def run_once(self, *, claim_kind=None, exclude_kind=None, claim_story_id=None) -> bool:
        if claim_kind is not None:
            return await super().run_once(claim_kind=claim_kind, exclude_kind=exclude_kind, claim_story_id=claim_story_id)
        cancel_filter, cancel_params = self._worker_filters(claim_kind='cancel',
            exclude_kind=exclude_kind, claim_story_id=claim_story_id)
        with self.store.connection() as db:
            cancel_ready = db.execute(
                "SELECT 1 FROM jobs WHERE ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?))" + cancel_filter + " LIMIT 1",
                (self.store.now(), self.store.now(), *cancel_params),
            ).fetchone()
        if not cancel_ready:
            return await super().run_once(exclude_kind=exclude_kind, claim_story_id=claim_story_id)
        now = self.store.now()
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE ((state IN ('ready','retry') AND available_at<=?) OR (state='running' AND lease_until<=?))" + cancel_filter + " ORDER BY created_at LIMIT 1",
                (now, now, *cancel_params),
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
