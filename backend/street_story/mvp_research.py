from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import MalformedProviderResponse
from .mvp import MvpProductStreetStoryService
from .product import project_destinations_v2
from .providers import PermanentProviderError
from .service import ConflictError, InvalidStateError, NotFoundError, canonical, digest


_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{4,}")


def _norm_url(value: Any) -> str:
    text = str(value or "").strip()
    return text.rstrip("/") if text.startswith("https://") else ""


def _claim_id(claim_key: str, text: str) -> str:
    identity = re.sub(r"\s+", " ", (claim_key or text).strip().lower())
    return "claim_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _excerpt_supports(claim: str, excerpt: str) -> bool:
    claim_words = {word.lower() for word in _WORD.findall(claim) if len(word) >= 5}
    if not claim_words:
        return False
    lowered = excerpt.lower()
    hits = sum(1 for word in claim_words if word in lowered)
    return hits >= min(2, len(claim_words))


class MvpResearchMixin:
    """Explicit multi-message research and evidence semantics for the MVP."""

    def complete_voice(self, story_id: str, session_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        expected = body.get("chunks") or []
        with self.store.tx() as db:
            session = db.execute(
                "SELECT * FROM voice_sessions WHERE session_id=? AND story_id=?", (session_id, story_id)
            ).fetchone()
            if not session:
                raise NotFoundError("voice session not found")
            self._idem(db, key, "complete_voice", req_digest, "voice_session", session_id)
            actual = [
                {"index": row["chunk_index"], "sha256": row["sha256"]}
                for row in db.execute(
                    "SELECT chunk_index,sha256 FROM voice_chunks WHERE session_id=? ORDER BY chunk_index",
                    (session_id,),
                )
            ]
            expected_identity = [
                {"index": int(item["index"]), "sha256": str(item["sha256"]).lower()}
                for item in expected
            ]
            exact_indices = [item["index"] for item in expected_identity] == list(
                range(len(expected_identity))
            )
            if (
                not exact_indices
                or actual != expected_identity
                or int(body.get("chunk_count", -1)) != len(actual)
            ):
                raise ConflictError(
                    "voice_manifest_mismatch",
                    "Exact ordered chunk manifest is required; reconcile and retry",
                )
            if session["recording_finished"]:
                if json.loads(session["manifest_json"] or "[]") != actual:
                    raise ConflictError("voice_manifest_conflict", "Completed voice manifest is immutable")
                return self._voice_receipt(db, session_id)

            now = self.store.now()
            db.execute(
                "UPDATE voice_sessions SET recording_finished=1,manifest_json=?,metadata_json=?,updated_at=? "
                "WHERE session_id=?",
                (
                    canonical(actual),
                    canonical({**json.loads(session["metadata_json"]), "complete": body}),
                    now,
                    session_id,
                ),
            )
            story = self._story_row(db, story_id)
            has_research = bool(json.loads(story["research_json"] or "{}"))
            state = "review" if has_research else "voice_ready"
            db.execute(
                "UPDATE stories SET state=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? "
                "WHERE id=?",
                (state, now, story_id),
            )
            return self._voice_receipt(db, session_id)

    def mutate_refinement(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        """Accept legacy refinement sync without implicitly starting research."""
        session_id = str(body.get("voice_session_id") or "")
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            self._story_row(db, story_id)
            if self._idem(db, key, "refinement", req_digest, "story", story_id):
                return self._story_repr(db, self._story_row(db, story_id))
            session = db.execute(
                "SELECT * FROM voice_sessions WHERE session_id=? AND story_id=?",
                (session_id, story_id),
            ).fetchone()
            if not session or session["kind"] != "refinement" or not session["recording_finished"]:
                raise InvalidStateError(
                    "refinement_voice_not_complete",
                    "A completed refinement voice session is required",
                )
            return self._story_repr(db, self._story_row(db, story_id))

    def _research_request(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(body.get("candidate_id") or "").strip() or None
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if self._idem(db, key, "research", req_digest, "story", story_id):
                return self._story_repr(db, self._story_row(db, story_id))
            sessions = list(
                db.execute(
                    "SELECT session_id,kind,manifest_json,created_at FROM voice_sessions "
                    "WHERE story_id=? AND recording_finished=1 ORDER BY created_at,session_id",
                    (story_id,),
                )
            )
            if not sessions:
                raise InvalidStateError(
                    "research_voice_required", "At least one completed voice message is required"
                )
            ordered_ids = [row["session_id"] for row in sessions]
            revision_basis = {
                "photo_sha256": story["photo_sha256"],
                "voices": [
                    {
                        "session_id": row["session_id"],
                        "kind": row["kind"],
                        "manifest": json.loads(row["manifest_json"] or "[]"),
                    }
                    for row in sessions
                ],
                "candidate_id": candidate_id,
            }
            input_revision = digest(revision_basis)
            self._enqueue_job(
                db,
                story_id,
                "research",
                f"research-explicit:{input_revision}",
                {
                    "voice_session_ids": ordered_ids,
                    "input_revision": input_revision,
                    "confirmed_candidate_id": candidate_id,
                },
            )
            now = self.store.now()
            db.execute(
                "UPDATE stories SET state='researching',revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? "
                "WHERE id=?",
                (now, story_id),
            )
            return self._story_repr(db, self._story_row(db, story_id))

    def _selected_outputs(self, db, story_id: str, place_name: str | None, author_note: str = "") -> tuple[str | None, str]:
        selected = [
            str(row["text"]).strip()
            for row in db.execute(
                "SELECT text FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                (story_id,),
            )
            if str(row["text"]).strip()
        ]
        post_parts: list[str] = []
        if author_note.strip():
            post_parts.append(author_note.strip())
        post_parts.extend(selected)
        draft = "\n\n".join(post_parts).strip() or None
        image_notes = "\n".join(selected[:6])
        if place_name and image_notes:
            image_notes = f"{place_name}:\n{image_notes}"
        return draft, image_notes

    def _mark_visual_stale(self, db, story, selected_ids: list[str]) -> None:
        context = json.loads(story["visual_context_json"] or "{}")
        if not context:
            return
        frozen_ids = [
            str(item.get("fact_id"))
            for item in context.get("selected_facts", [])
            if isinstance(item, dict) and item.get("fact_id")
        ]
        if frozen_ids == selected_ids:
            return
        context["stale"] = True
        context["stale_reason"] = "fact_selection_changed"
        state = story["state"]
        clear_asset = state != "scheduled"
        db.execute(
            "UPDATE stories SET visual_context_json=?,vibepublish_asset_ref=CASE WHEN ? THEN NULL ELSE vibepublish_asset_ref END,"
            "state=CASE WHEN ? THEN 'needs_review' ELSE state END,error_code=CASE WHEN ? THEN 'visual_stale' ELSE error_code END,"
            "error_message=CASE WHEN ? THEN 'Выбор фактов изменился; изображение нужно обновить перед новой публикацией.' ELSE error_message END,"
            "revision=revision+1,updated_at=? WHERE id=?",
            (
                canonical(context),
                int(clear_asset),
                int(clear_asset),
                int(clear_asset),
                int(clear_asset),
                self.store.now(),
                story["id"],
            ),
        )

    def mutate_facts(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        if str(body.get("action") or "") == "research":
            return self._research_request(story_id, key, body)

        selected_ids = [str(value) for value in body.get("selected_fact_ids", [])]
        selected_set = set(selected_ids)
        req_digest = digest({"story_id": story_id, **body})
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if self._idem(db, key, "facts", req_digest, "story", story_id):
                return self._story_repr(db, self._story_row(db, story_id))
            facts = list(db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,)))
            valid = {row["fact_id"] for row in facts}
            unknown = selected_set - valid
            if unknown:
                raise ConflictError("fact_id_unknown", f"Unknown fact ids: {sorted(unknown)}")
            for row in facts:
                selected = row["fact_id"] in selected_set and bool(row["evidence_supported"])
                db.execute(
                    "UPDATE facts SET selected=? WHERE story_id=? AND fact_id=?",
                    (int(selected), story_id, row["fact_id"]),
                )
            research = json.loads(story["research_json"] or "{}")
            decisions = {
                row["fact_id"]: bool(row["selected"])
                for row in db.execute("SELECT fact_id,selected FROM facts WHERE story_id=?", (story_id,))
            }
            research["claim_decisions"] = decisions
            generated_draft, image_notes = self._selected_outputs(
                db, story_id, story["place_name"], str(research.get("author_note") or "")
            )
            # Live/manual selection can update evidence without erasing an already
            # authored publication draft. Legacy callers keep the historical rebuild.
            preserve_draft = bool(body.get("preserve_draft", False))
            draft = str(story["draft_text"] or "") if preserve_draft else generated_draft
            research["image_notes"] = image_notes
            db.execute(
                "UPDATE stories SET draft_text=?,research_json=?,updated_at=? WHERE id=?",
                (draft, canonical(research), self.store.now(), story_id),
            )
            supported_selected = [
                row["fact_id"]
                for row in db.execute(
                    "SELECT fact_id FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
            self._mark_visual_stale(db, story, supported_selected)
            return self._story_repr(db, self._story_row(db, story_id))

    @staticmethod
    def _candidate_catalog(osm: dict[str, Any], wikipedia: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in wikipedia:
            title = str(page.get("title") or "").strip()
            if not title:
                continue
            raw_id = str(page.get("pageid") or hashlib.sha256(title.encode()).hexdigest()[:12])
            cid = f"wiki:{raw_id}"
            if cid not in seen:
                seen.add(cid)
                candidates.append(
                    {
                        "candidate_id": cid,
                        "name": title,
                        "type": "wikipedia",
                        "url": _norm_url(page.get("url")),
                        "reference_excerpt": str(page.get("extract") or "")[:1200],
                    }
                )
        for item in [osm.get("reverse", {}), *osm.get("nearby", [])]:
            osm_type = str(item.get("osm_type") or item.get("type") or "")
            osm_id = item.get("osm_id") or item.get("id")
            tags = item.get("tags") or {}
            name = str(tags.get("name") or item.get("display_name") or "").strip()
            if not name or osm_type not in {"node", "way", "relation"} or osm_id is None:
                continue
            cid = f"osm:{osm_type}:{osm_id}"
            if cid in seen:
                continue
            seen.add(cid)
            candidates.append(
                {
                    "candidate_id": cid,
                    "name": name,
                    "type": "osm",
                    "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                    "reference_excerpt": canonical(tags)[:1200],
                }
            )
        return candidates[:12]

    async def _identify_photo(
        self,
        story: dict[str, Any],
        transcript: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        custom = getattr(self.providers.gemini, "identify_photo", None)
        if callable(custom):
            return await custom(Path(story["photo_path"]), story["photo_mime_type"], transcript, candidates)
        gemini = self.providers.gemini
        if not hasattr(gemini, "_generate") or not hasattr(gemini, "executor"):
            raise PermanentProviderError("Gemini visual identity capability is unavailable")
        from google.genai import types

        schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["match", "uncertain", "mismatch"]},
                "candidate_id": {"type": "string"},
                "confidence": {"type": "number"},
                "observations": {"type": "array", "items": {"type": "string"}},
                "alternative_candidate_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status", "candidate_id", "confidence", "observations", "alternative_candidate_ids"],
        }
        prompt = (
            "Ты выполняешь только визуальную идентификацию объекта Street Story, до исследования фактов. "
            "Сравни исходное фото с кандидатами OSM/Wikipedia по наблюдаемым признакам. Не выдавай исторические факты. "
            "status=match только если конкретный кандидат визуально достаточно убедителен; при сомнении uncertain, "
            "при явном несовпадении mismatch. candidate_id обязан быть из списка или пустой строкой. "
            "Кратко перечисли видимые признаки, на которых основано решение.\n"
            + json.dumps({"voice_context": transcript, "candidates": candidates}, ensure_ascii=False)
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_json_schema=schema
        )
        photo = Path(story["photo_path"]).read_bytes()

        async def call(api_key, timeout):
            response = await gemini._generate(
                api_key,
                timeout,
                [types.Part.from_bytes(data=photo, mime_type=story["photo_mime_type"]), prompt],
                config,
            )
            try:
                payload = json.loads(response.text or "{}")
                status = payload.get("status")
                confidence = float(payload.get("confidence", 0.0))
                if status not in {"match", "uncertain", "mismatch"} or not math.isfinite(confidence):
                    raise ValueError
                if not isinstance(payload.get("observations"), list):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                raise MalformedProviderResponse("gemini:malformed_visual_identity") from None
            return payload

        return await gemini.executor.execute("grounded_research", call)

    async def _research_claims(
        self,
        story: dict[str, Any],
        transcript: str,
        identity: dict[str, Any],
        previous: list[dict[str, Any]],
    ) -> dict[str, Any]:
        custom = getattr(self.providers.gemini, "research_v2", None)
        if callable(custom):
            return await custom(Path(story["photo_path"]), story["photo_mime_type"], transcript, identity, previous)
        gemini = self.providers.gemini
        if not hasattr(gemini, "_generate") or not hasattr(gemini, "executor"):
            raise PermanentProviderError("Gemini grounded research capability is unavailable")
        from google.genai import types

        prompt = (
            "Ты исследователь Street Story. Идентичность объекта уже определена отдельным visual step; исследуй ИМЕННО этот объект. "
            "Используй Google Search grounding напрямую. Возвращай только проверяемые исторические/городские claims. "
            "claim_key — короткая стабильная семантическая идентичность утверждения, не зависящая от перефразирования. "
            "source_urls перечисляй только для источников, реально поддерживающих конкретный claim и реально увиденных через grounding. "
            "Не считай собственный ответ источником и не выдумывай цитаты. author_note может содержать только субъективное впечатление "
            "пользователя из voice context, без добавленных исторических сведений. "
            "Верни только один JSON-объект без Markdown и комментариев строго такой формы: "
            '{"summary":"...","author_note":"...","facts":[{"claim_key":"...","text":"...","confidence":0.0,"source_urls":["https://..."]}]}.\\n'
            + json.dumps(
                {
                    "confirmed_identity": identity,
                    "voice_context": transcript,
                    "previous_claims_and_owner_decisions": previous,
                },
                ensure_ascii=False,
            )
        )
        # Google Search + response schema is not supported by Gemini 3.1 Flash-Lite.
        # Identity is already confirmed by the visual step, so research is text-only
        # and uses Search grounding with an explicit JSON contract in the prompt.
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )

        async def call(api_key, timeout):
            response = await gemini._generate(api_key, timeout, [prompt], config)
            try:
                raw = str(response.text or "").strip()
                fence = "`" * 3
                if raw.startswith(fence) and raw.endswith(fence):
                    fenced = raw.splitlines()
                    if len(fenced) >= 3 and fenced[0].strip().lower() in {fence, fence + "json"} and fenced[-1].strip() == fence:
                        raw = "\n".join(fenced[1:-1]).strip()
                payload = json.loads(raw or "{}")
                if not isinstance(payload.get("summary"), str) or not isinstance(payload.get("author_note"), str):
                    raise ValueError
                if not isinstance(payload.get("facts"), list):
                    raise ValueError
                for fact in payload["facts"]:
                    if (
                        not isinstance(fact, dict)
                        or not isinstance(fact.get("claim_key"), str)
                        or not isinstance(fact.get("text"), str)
                        or not isinstance(fact.get("source_urls"), list)
                        or not math.isfinite(float(fact.get("confidence", 0.0)))
                    ):
                        raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                raise MalformedProviderResponse("gemini:malformed_grounded_research") from None

            chunks: list[dict[str, str]] = []
            supports: list[dict[str, str]] = []
            for candidate in getattr(response, "candidates", []) or []:
                metadata = getattr(candidate, "grounding_metadata", None)
                local_chunks: list[dict[str, str]] = []
                for chunk in getattr(metadata, "grounding_chunks", []) or []:
                    web = getattr(chunk, "web", None)
                    url = _norm_url(getattr(web, "uri", None))
                    source = {
                        "type": "web",
                        "title": str(getattr(web, "title", "") or url),
                        "url": url,
                    }
                    local_chunks.append(source)
                    if url:
                        chunks.append(source)
                for support in getattr(metadata, "grounding_supports", []) or []:
                    segment = getattr(support, "segment", None)
                    segment_text = str(getattr(segment, "text", "") or "").strip()
                    for index in getattr(support, "grounding_chunk_indices", []) or []:
                        if isinstance(index, int) and 0 <= index < len(local_chunks):
                            url = local_chunks[index]["url"]
                            if url and segment_text:
                                supports.append(
                                    {
                                        "kind": "google_grounding",
                                        "source_url": url,
                                        "text": segment_text[:600],
                                    }
                                )
            unique_chunks = {item["url"]: item for item in chunks if item["url"]}
            if not unique_chunks or not supports:
                raise MalformedProviderResponse("gemini:missing_search_grounding")
            return {
                "payload": payload,
                "grounding_sources": list(unique_chunks.values()),
                "grounding_supports": supports,
            }

        return await gemini.executor.execute("grounded_research", call)

    async def _run_research(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        payload = json.loads(job["payload_json"] or "{}")
        session_ids = [str(value) for value in payload.get("voice_session_ids", [])]
        live_transcript = str(payload.get("live_transcript") or "").strip()
        input_revision = str(payload.get("input_revision") or "")
        if (not session_ids and not live_transcript) or not input_revision:
            raise PermanentProviderError("Explicit research snapshot is incomplete")

        with self.store.connection() as db:
            story = dict(self._story_row(db, story_id))
            prior = json.loads(story["research_json"] or "{}")
            previous = [
                {
                    "fact_id": row["fact_id"],
                    "text": row["text"],
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (story_id,))
            ]

        if live_transcript:
            transcript = live_transcript[:12000]
        else:
            transcripts: list[str] = []
            for session_id in session_ids:
                transcripts.append(await self._transcribe_session(session_id))
            transcript = "\n\n".join(text.strip() for text in transcripts if text.strip())

        lat, lon = story["latitude"], story["longitude"]
        osm: dict[str, Any] = {"reverse": {}, "nearby": []}
        wikipedia: list[dict[str, Any]] = []
        if lat is not None and lon is not None:
            osm = self.store.checkpoint_get(job["id"], "osm")
            if osm is None:
                osm = await self.providers.osm.lookup(float(lat), float(lon))
                self.store.checkpoint_put(job["id"], "osm", osm)
            wikipedia = self.store.checkpoint_get(job["id"], "wikipedia")
            if wikipedia is None:
                wikipedia = await self.providers.wikipedia.nearby(float(lat), float(lon))
                self.store.checkpoint_put(job["id"], "wikipedia", wikipedia)
        candidates = self._candidate_catalog(osm, wikipedia)

        confirmed_candidate_id = str(payload.get("confirmed_candidate_id") or "")
        previous_identity = prior.get("visual_identity") if isinstance(prior.get("visual_identity"), dict) else None
        catalog = {item["candidate_id"]: item for item in candidates}
        if confirmed_candidate_id:
            chosen = catalog.get(confirmed_candidate_id)
            if not chosen:
                raise ConflictError("candidate_id_unknown", "Selected place candidate is not available")
            identity = {
                "status": "owner_confirmed",
                "candidate_id": confirmed_candidate_id,
                "candidate_name": chosen["name"],
                "confidence": None,
                "observations": ["Кандидат выбран владельцем; это подтверждение, а не результат visual match."],
                "candidates": candidates,
            }
        elif (
            previous_identity
            and previous_identity.get("status") in {"match", "owner_confirmed"}
            and previous_identity.get("candidate_id") in catalog
        ):
            identity = {**previous_identity, "candidates": candidates}
        elif not candidates:
            identity = {
                "status": "uncertain",
                "candidate_id": None,
                "candidate_name": None,
                "confidence": 0.0,
                "observations": ["Нет GPS-кандидатов: требуется название/адрес от владельца."],
                "candidates": [],
            }
        else:
            raw_identity = self.store.checkpoint_get(job["id"], "visual_identity")
            if raw_identity is None:
                raw_identity = await self._identify_photo(story, transcript, candidates)
                self.store.checkpoint_put(job["id"], "visual_identity", raw_identity)
            candidate_id = str(raw_identity.get("candidate_id") or "")
            status = str(raw_identity.get("status") or "uncertain")
            if candidate_id not in catalog:
                status = "uncertain"
                candidate_id = ""
            chosen = catalog.get(candidate_id)
            identity = {
                "status": status,
                "candidate_id": candidate_id or None,
                "candidate_name": chosen["name"] if chosen else None,
                "confidence": max(0.0, min(1.0, float(raw_identity.get("confidence", 0.0)))),
                "observations": [str(value)[:300] for value in raw_identity.get("observations", [])[:6]],
                "alternative_candidate_ids": [
                    str(value)
                    for value in raw_identity.get("alternative_candidate_ids", [])[:6]
                    if str(value) in catalog
                ],
                "candidates": candidates,
            }

        if identity["status"] not in {"match", "owner_confirmed"}:
            research = {
                **prior,
                "input_revision": input_revision,
                "ordered_voice_ids": session_ids,
                "transcript": transcript,
                "visual_identity": identity,
                "osm": osm,
                "wikipedia": wikipedia,
            }
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state='needs_review',research_json=?,error_code='visual_identity_uncertain',"
                    "error_message='Выберите подходящий объект перед поиском фактов.',revision=revision+1,updated_at=? WHERE id=?",
                    (canonical(research), self.store.now(), story_id),
                )
            return

        saved = self.store.checkpoint_get(job["id"], "grounded_research_v2")
        if saved is None:
            saved = await self._research_claims(story, transcript, identity, previous)
            self.store.checkpoint_put(job["id"], "grounded_research_v2", saved)

        grounding_sources = saved.get("grounding_sources", [])
        grounding_supports = saved.get("grounding_supports", [])
        grounding_by_url: dict[str, list[dict[str, str]]] = {}
        for support in grounding_supports:
            url = _norm_url(support.get("source_url"))
            if url:
                grounding_by_url.setdefault(url, []).append(support)

        known: dict[str, dict[str, Any]] = {}
        for page in wikipedia:
            url = _norm_url(page.get("url"))
            if url:
                known[url] = {
                    "type": "wikipedia",
                    "title": str(page.get("title") or url),
                    "url": url,
                    "excerpt": str(page.get("extract") or ""),
                }
        for source in grounding_sources:
            url = _norm_url(source.get("url"))
            if url:
                known[url] = {
                    "type": "web",
                    "title": str(source.get("title") or url),
                    "url": url,
                }

        incoming = saved.get("payload", {}).get("facts", [])[:20]
        is_refinement = bool(prior.get("input_revision"))
        old_decisions = {
            row["fact_id"]: bool(row["selected"])
            for row in previous
            if row.get("fact_id")
        }
        normalized: list[dict[str, Any]] = []
        seen_claims: set[str] = set()
        for item in incoming:
            text = str(item.get("text") or "").strip()
            claim_key = str(item.get("claim_key") or "").strip()
            if not text:
                continue
            fact_id = _claim_id(claim_key, text)
            if fact_id in seen_claims:
                continue
            seen_claims.add(fact_id)
            sources: list[dict[str, Any]] = []
            for raw_url in item.get("source_urls", []) or []:
                url = _norm_url(raw_url)
                source = known.get(url)
                if not source:
                    continue
                supports: list[dict[str, str]] = []
                supports.extend(grounding_by_url.get(url, []))
                excerpt = str(source.get("excerpt") or "")
                if excerpt and _excerpt_supports(text, excerpt):
                    supports.append(
                        {
                            "kind": "retrieved_excerpt",
                            "source_url": url,
                            "text": excerpt[:600],
                        }
                    )
                if supports:
                    sources.append(
                        {
                            "type": source["type"],
                            "title": source["title"],
                            "url": url,
                            "supports": supports,
                        }
                    )
            evidence_supported = bool(sources)
            default_selected = evidence_supported and (not is_refinement)
            selected = evidence_supported and old_decisions.get(fact_id, default_selected)
            normalized.append(
                {
                    "fact_id": fact_id,
                    "text": text,
                    "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
                    "evidence_supported": evidence_supported,
                    "selected": selected,
                    "sources": sources,
                }
            )

        with self.store.tx() as db:
            db.execute("DELETE FROM facts WHERE story_id=?", (story_id,))
            for fact in normalized:
                db.execute(
                    "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        story_id,
                        fact["fact_id"],
                        fact["text"],
                        fact["confidence"],
                        int(fact["evidence_supported"]),
                        int(fact["selected"]),
                        canonical(fact["sources"]),
                    ),
                )
            chosen = catalog.get(str(identity.get("candidate_id") or ""))
            place_name = str(identity.get("candidate_name") or (chosen or {}).get("name") or "").strip() or None
            author_note = str(saved.get("payload", {}).get("author_note") or "").strip()
            draft, image_notes = self._selected_outputs(db, story_id, place_name, author_note)
            source_urls = {
                source["url"]
                for fact in normalized
                for source in fact["sources"]
                if source.get("url")
            }
            research = {
                "input_revision": input_revision,
                "ordered_voice_ids": session_ids,
                "transcript": transcript,
                "visual_identity": identity,
                "osm": osm,
                "wikipedia": wikipedia,
                "grounding_sources": grounding_sources,
                "source_count": len(source_urls),
                "author_note": author_note,
                "image_notes": image_notes,
                "claim_decisions": {fact["fact_id"]: bool(fact["selected"]) for fact in normalized},
            }
            db.execute(
                "UPDATE stories SET state='review',place_name=?,summary=?,draft_text=?,research_json=?,"
                "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (
                    place_name,
                    str(saved.get("payload", {}).get("summary") or "").strip() or None,
                    draft,
                    canonical(research),
                    self.store.now(),
                    story_id,
                ),
            )

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        research = json.loads(row["research_json"] or "{}")
        identity = research.get("visual_identity")
        if isinstance(identity, dict):
            result["visual_identity"] = identity
        if research.get("input_revision"):
            result["research_revision"] = research["input_revision"]
        unique: dict[str, dict[str, str]] = {}
        for fact in result.get("facts", []):
            for source in fact.get("sources", []):
                if not isinstance(source, dict):
                    continue
                url = _norm_url(source.get("url"))
                if url:
                    unique[url] = {
                        "type": str(source.get("type") or "web"),
                        "title": str(source.get("title") or url),
                        "url": url,
                    }
        result["sources"] = list(unique.values())
        result["source_count"] = len(unique)
        visual = result.setdefault("visual", {})
        context = json.loads(row["visual_context_json"] or "{}")
        if context.get("stale"):
            visual["stale"] = True
            visual["stale_reason"] = context.get("stale_reason")
        return result

    async def capabilities(self) -> dict[str, Any]:
        result = await super().capabilities()
        telegram = [
            item
            for item in result.get("destinations", [])
            if item.get("provider") == "telegram" and item.get("status") == "supported"
        ]
        for item in telegram:
            item["selected"] = True
        result["destinations"] = telegram
        result["mvp_providers"] = {"telegram": "active", "vk": "deferred", "max": "deferred"}
        return result

    def mutate_publish(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text_override")
        if text is not None and len(str(text)) > 1024:
            raise ConflictError(
                "publish_text_too_long",
                "Telegram photo caption exceeds the current VibePublish 1024-character limit",
            )
        result = super().mutate_publish(story_id, key, body)
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            intent = db.execute(
                "SELECT * FROM publish_intents WHERE story_id=? AND request_key=?", (story_id, key)
            ).fetchone()
            if intent:
                request = json.loads(intent["request_json"] or "{}")
                caption = str(request.get("text") or "")
                if len(caption) > 1024:
                    raise ConflictError(
                        "publish_text_too_long",
                        "Telegram photo caption exceeds the current VibePublish 1024-character limit",
                    )
                visual = json.loads(story["visual_context_json"] or "{}")
                research = json.loads(story["research_json"] or "{}")
                voice_ids = [
                    row["session_id"]
                    for row in db.execute(
                        "SELECT session_id FROM voice_sessions WHERE story_id=? AND recording_finished=1 "
                        "ORDER BY created_at,session_id",
                        (story_id,),
                    )
                ]
                request["story_snapshot"] = {
                    "source_photo_sha256": story["photo_sha256"],
                    "ordered_voice_ids": voice_ids,
                    "input_revision": research.get("input_revision"),
                    "selected_fact_ids": [
                        row["fact_id"]
                        for row in db.execute(
                            "SELECT fact_id FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                            (story_id,),
                        )
                    ],
                    "prompt_version": visual.get("prompt_version"),
                    "prompt_sha256": visual.get("prompt_sha256"),
                    "visual_content_revision": visual.get("content_revision"),
                    "image_asset_ref": story["vibepublish_asset_ref"],
                    "caption_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
                }
                db.execute(
                    "UPDATE publish_intents SET request_json=?,updated_at=? WHERE id=?",
                    (canonical(request), self.store.now(), intent["id"]),
                )
        return result

    async def _run_publish(self, job: dict[str, Any]) -> None:
        intent_id = json.loads(job["payload_json"])["intent_id"]
        with self.store.connection() as db:
            intent = dict(
                db.execute("SELECT * FROM publish_intents WHERE id=?", (intent_id,)).fetchone()
            )
        request = json.loads(intent["request_json"] or "{}")
        bootstrap = await self.providers.vibepublish.bootstrap()
        projected = project_destinations_v2(bootstrap)
        by_alias = {item["alias"]: item for item in projected}
        for alias in request.get("destinations", []):
            detail = by_alias.get(str(alias))
            if not detail or detail.get("provider") != "telegram" or detail.get("status") != "supported":
                raise PermanentProviderError(
                    "Street Story MVP only schedules currently-supported Telegram destinations"
                )
        return await super()._run_publish(job)


class MvpResearchStreetStoryService(MvpResearchMixin, MvpProductStreetStoryService):
    pass
