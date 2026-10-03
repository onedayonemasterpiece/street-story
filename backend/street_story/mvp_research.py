from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


from .errors import MalformedProviderResponse
from .camera_hints import reference_order, model_camera_hints
from .fact_conflicts import analyze_fact_conflicts
from .model_facts import merge_model_fact_inventory, model_fact_id, normalized_claim_key, validated_model_fact_text
from .identity_candidate_policy import wikipedia_identity_eligible
from .gemini import GeminiUnavailable
from .identity_lifecycle import IdentityLifecycleMixin
from .identity_visual import identify_nearest
from .mvp import MvpProductStreetStoryService
from .poi_memory import persist_research_memory, prior_facts, processed_sources
from .providers import PermanentProviderError
from .service import ConflictError, InvalidStateError, NotFoundError, canonical, digest


def _is_internal_acceptance_destination(alias: Any) -> bool:
    value = str(alias or "").strip().lower()
    return value == "street_story_e2e_tg" or value.startswith("street_story_e2e_")


def _norm_url(value: Any) -> str:
    text = str(value or "").strip()
    return text.rstrip("/") if text.startswith("https://") else ""


class MvpResearchMixin(IdentityLifecycleMixin):
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
            research = json.loads(story["research_json"] or "{}")
            has_research = bool(research.get("input_revision"))
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            state = "review" if has_research else (
                "identity_ready" if identity.get("status") in {"match", "owner_confirmed"} else "voice_ready"
            )
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
            selected_text = [
                str(row["text"])
                for row in db.execute(
                    "SELECT text FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
            research["image_notes"] = "\n".join(selected_text[:6])
            research["draft_needs_refresh"] = True
            db.execute(
                "UPDATE stories SET research_json=?,updated_at=? WHERE id=?",
                (canonical(research), self.store.now(), story_id),
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
    def _candidate_catalog(osm: dict[str, Any], wikipedia: list[dict[str, Any]], excluded_ids: set[str] | None = None) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        wiki_refs: dict[str, list[str]] = {}
        wikipedia_names: set[str] = set()

        for page in wikipedia:
            title = str(page.get("title") or "").strip()
            if not title:
                continue
            normalized = re.sub(r"\s+", " ", title.casefold())
            wikipedia_names.add(normalized)
            raw_id = str(page.get("pageid") or hashlib.sha256(title.encode()).hexdigest()[:12])
            cid = f"wiki:{raw_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            refs: list[str] = []
            for raw in (page.get("thumbnail_url"), page.get("image_url")):
                value = str(raw or "").strip()
                parsed = urlparse(value)
                if parsed.scheme == "https" and parsed.hostname == "upload.wikimedia.org" and value not in refs:
                    refs.append(value)
            wiki_refs[normalized] = refs[:2]
            candidates.append({
                "candidate_id": cid,
                "name": title,
                "type": "wikipedia",
                "url": _norm_url(page.get("url")),
                "reference_excerpt": str(page.get("extract") or "")[:1200],
                "reference_image_urls": refs[:2],
                "identity_eligible": wikipedia_identity_eligible(title, str(page.get("extract") or "")),
                "distance_m": page.get("distance_m"),
                "selection_bucket": "wikipedia",
                "salience_rank": 0,
            })

        for item in [osm.get("reverse", {}), *osm.get("nearby", [])]:
            osm_type = str(item.get("osm_type") or item.get("type") or "")
            osm_id = item.get("osm_id") or item.get("id")
            tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
            address_name = " ".join(
                part for part in (
                    str(tags.get("addr:street") or "").strip(),
                    str(tags.get("addr:housenumber") or "").strip(),
                ) if part
            )
            name = str(tags.get("name") or address_name or item.get("display_name") or "").strip()
            normalized = re.sub(r"\s+", " ", name.casefold())
            if (
                not name
                or osm_type not in {"node", "way", "relation"}
                or osm_id is None
                or (
                    str(item.get("selection_bucket") or "") != "reverse"
                    and normalized
                    and normalized in wikipedia_names
                )
            ):
                continue
            if osm_type == "relation" and (
                tags.get("route")
                or tags.get("boundary")
                or str(tags.get("type") or "") in {"route", "boundary", "network"}
            ):
                continue
            if (item.get("class") == "boundary" or item.get("category") == "boundary"
                    or item.get("type") == "administrative" or tags.get("boundary")
                    or (tags.get("highway") and not (tags.get("bridge") or tags.get("historic") or tags.get("building")))):
                continue
            cid = f"osm:{osm_type}:{osm_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            candidates.append({
                "candidate_id": cid,
                "name": name,
                "type": "osm",
                "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                "reference_excerpt": canonical(tags)[:1200],
                "reference_image_urls": wiki_refs.get(normalized, []),
                "distance_m": item.get("distance_m"),
                "selection_bucket": item.get("selection_bucket"),
                "salience_rank": item.get("salience_rank", 3),
            })

        # Exclusion precedes the shortlist cap so a previously 17th candidate
        # can be considered after the owner rejects the current result.
        excluded = excluded_ids or set()
        candidates = [item for item in candidates if item.get("candidate_id") not in excluded]

        def distance(item: dict[str, Any]) -> float:
            try:
                value = float(item.get("distance_m"))
                return value if math.isfinite(value) and value >= 0 else 10_000.0
            except (TypeError, ValueError):
                return 10_000.0

        shortlist: list[dict[str, Any]] = []
        chosen: set[str] = set()

        def take(items: list[dict[str, Any]], limit: int, bucket: str) -> None:
            limit = min(limit, 16 - len(shortlist))
            if limit <= 0:
                return
            added = 0
            for item in items:
                cid = str(item.get("candidate_id") or "")
                if not cid or cid in chosen:
                    continue
                chosen.add(cid)
                shortlist.append({**item, "shortlist_bucket": bucket})
                added += 1
                if added >= limit:
                    return

        reverse = sorted(
            [item for item in candidates if item.get("selection_bucket") == "reverse"],
            key=distance,
        )
        nearby = sorted(
            [item for item in candidates if item.get("selection_bucket") == "nearby"],
            key=distance,
        )
        landmarks = [item for item in candidates if item.get("selection_bucket") == "landmark"]
        wikipedia_candidates = sorted(
            [item for item in candidates if item.get("type") == "wikipedia"],
            key=lambda item: (0 if item.get("reference_image_urls") else 1, distance(item)),
        )

        take(reverse, 1, "reverse")
        take(nearby, 3, "nearby")

        for low, high, limit, label in (
            (0.0, 200.0, 3, "landmark_near"),
            (200.0, 400.0, 3, "landmark_mid"),
            (400.0, 600.1, 3, "landmark_far"),
        ):
            band = [
                item for item in landmarks
                if low <= distance(item) < high
            ]
            band.sort(
                key=lambda item: (
                    int(item.get("salience_rank", 3)),
                    0 if item.get("reference_image_urls") else 1,
                    distance(item),
                )
            )
            take(band, limit, label)

        take(wikipedia_candidates, 3, "wikipedia")

        # Fill unused capacity with the best remaining landmarks/nearby objects,
        # but never exceed the bounded visual-analysis shortlist.
        remaining = sorted(
            [item for item in candidates if str(item.get("candidate_id") or "") not in chosen],
            key=lambda item: (
                int(item.get("salience_rank", 3)),
                0 if item.get("reference_image_urls") else 1,
                distance(item),
            ),
        )
        take(remaining, 16 - len(shortlist), "fill")
        shortlist.sort(key=lambda item: (distance(item), 0 if item.get("reference_image_urls") else 1))
        return shortlist[:16]

    async def _candidate_reference_images(self, candidates, limit=6, *, story_id=None, evidence=None):
        from .identity_references import reference_images
        return await reference_images(self, candidates, limit, story_id=story_id, evidence=evidence)

    async def _identify_photo(self, story, transcript, candidates):
        return await identify_nearest(self, story, transcript, candidates)

    async def _identify_photo_batch(
        self,
        story: dict[str, Any],
        transcript: str,
        candidates: list[dict[str, Any]],
        reference_limit: int = 2,
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
            "Сравни исходное фото с shortlist-кандидатами OSM/Wikipedia по наблюдаемым признакам. GPS — точка съёмки, "            "а не координата объекта: кандидат в сотнях метров может быть правильнее ближайшего. Расстояние — только prior; "            "визуальное совпадение важнее. Не выдавай исторические факты. "
            "Это текущая ближайшая группа кандидатов; дальние будут проверены только при необходимости. "
            "status=match только при совпадении отличительных деталей с приложенным REFERENCE_IMAGE; "
            "близость GPS или известность названия сами по себе не доказательство. При сомнении uncertain, "
            "при явном несовпадении mismatch. candidate_id обязан быть из списка или пустой строкой. "
            "Кандидат с identity_eligible=false — только географический/поисковый контекст; его нельзя выбирать как match. "
            "Параметры объектива не определяют расстояние до объекта; эквивалентное фокусное и зум не перемножай. "
            "camera_alignment и угловое отклонение — лишь подсказки по неточному компасу и центру OSM-объекта, "
            "не основание исключать кандидата или подтверждать совпадение. "
            "Фото может показывать только часть объекта с другого ракурса: детали вне кадра не считаются несовпадением. "
            "Ищи конкретные повторяющиеся формы, пропорции, проёмы и декор на видимой части, а не сходство общего стиля. "
            "alternative_candidate_ids указывай только для других физических объектов, которые после сравнения реально "
            "остаются визуально неотличимыми. Не перечисляй туда просто остальные кандидаты, страницы города/района, "
            "явно несовпавшие эталоны или современное/историческое имя того же здания. "
            "Кратко, по-русски перечисли видимые признаки, на которых основано решение.\n"
            + json.dumps({"voice_context": transcript, "candidates": candidates,
                          "capture_hints": model_camera_hints(story.get('_camera_hints') or {})}, ensure_ascii=False)
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_json_schema=schema,
            system_instruction="Все observations пиши по-русски. Название города или района само по себе не является идентификацией конкретного здания. Несколько изображений одного объекта — не разные альтернативные объекты."
        )
        from PIL import Image, ImageOps
        import io
        with Image.open(story["photo_path"]) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1280, 1280))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            photo = output.getvalue()
        reference_candidates = reference_order(candidates)
        reference_evidence = []
        reference_images = await self._candidate_reference_images(
            reference_candidates, limit=reference_limit, story_id=story["id"], evidence=reference_evidence)
        from .identity_telemetry import record_identity_event
        record_identity_event(self, story['id'], 'identity_reference_priority', {
            'candidate_ids': [item['candidate_id'] for item in reference_candidates],
            'priority_applied': reference_candidates != candidates,
            'reference_ids_sent': [item[0] for item in reference_images],
            'alignment_available_count': sum('camera_alignment' in item for item in candidates),
        })
        parts: list[Any] = [
            types.Part.from_bytes(data=photo, mime_type="image/jpeg"),
            prompt,
        ]
        for candidate_id, mime_type, data in reference_images:
            parts.append(f"REFERENCE_IMAGE candidate_id={candidate_id}")
            parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))

        async def call(api_key, timeout, *, model=None, quota=None):
            response = await gemini._generate(
                api_key,
                timeout,
                parts,
                config,
                operation="grounded_research",
                model=model,
                quota=quota,
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
            considered = [x["candidate_id"] for x in reference_candidates if x.get("reference_image_urls")][:reference_limit]
            return {**payload, "_references_unavailable_ids": [cid for cid in considered if cid not in {x[0] for x in reference_images}],
                    "_references_sent": [item[0] for item in reference_images],
                    "_reference_evidence": reference_evidence,
                    "_references_rate_limited": getattr(self, "_wikimedia_reference_wait_until", 0) > __import__("time").monotonic()}

        routes = getattr(gemini, "research_routes", None)
        if not routes:
            return await gemini.executor.execute("grounded_research", call)
        retry_at: list[float] = []
        for model, _pool, quota, executor in routes:
            async def routed_call(api_key, timeout, *, _model=model, _quota=quota):
                return await call(api_key, timeout, model=_model, quota=_quota)
            try:
                return await executor.execute("grounded_research", routed_call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
                continue
            except PermanentProviderError as exc:
                if str(exc) == "gemini:unsupported_model":
                    continue
                raise
        if retry_at:
            raise GeminiUnavailable(min(retry_at), "all_visual_identity_models_unavailable")
        raise PermanentProviderError("gemini:unsupported_model")

    async def _research_claims(
        self,
        story: dict[str, Any],
        transcript: str,
        identity: dict[str, Any],
        previous: list[dict[str, Any]],
        poi_history: list[dict[str, Any]] | None = None,
        processed_source_history: list[dict[str, Any]] | None = None,
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
            "Используй Google Search grounding напрямую. Возвращай только проверяемые исторические/городские ФАКТЫ. "
            "Верни до 32 facts. Каждый facts[].text — один атомарный тезис, обычно до 500 знаков: дата, человек, архитектор, событие, функция, "
            "реконструкция, посещение или другой конкретный факт. Не пиши вместо факта описание источника, вводные "
            "вроде «сайт сообщает», URL или несколько разных утверждений в одном пункте. Самые важные факты ставь первыми. "
            "claim_key — короткая стабильная семантическая идентичность утверждения, не зависящая от перефразирования. "
            "Если факт семантически совпадает с previous_claims_and_owner_decisions, верни точный fact_id в existing_fact_id; "
            "для нового факта existing_fact_id оставь пустой строкой. Это решение о тождестве принимает модель. "
            "Сначала обязательно ищи официальный источник объекта/учреждения, если он существует. Официальным считается сайт "
            "владельца, музея, учреждения, муниципалитета или оператора, но не Wikipedia, СМИ, агрегатор или туристический каталог. "
            "URL официальных источников, реально увиденных через grounding, перечисли в official_source_urls. Факты из официального "
            "источника имеют приоритет; для каждого факта source_urls перечисляй только реально поддерживающие его источники. "
            "previously_considered_poi_facts — факты об этом же объекте из предыдущих тем и, когда доступно, из Regional Knowledge/POI: "
            "используй их как модельный контекст для смыслового сопоставления и поиска противоречий; не считай их автоматически истинными "
            "и не повторяй в новой публикации без причины. previously_processed_sources — уже обработанные URL и запросы: не трать поиск "
            "на повторный обход тех же страниц без явной задачи перепроверки/конфликта; ищи пробелы, новые аспекты и независимое подтверждение. "
            "Если пользователь не просит повторить/обновить, ищи новую фактологию. "
            "Не считай собственный ответ источником и не выдумывай цитаты. author_note может содержать только субъективное впечатление "
            "пользователя из voice context, без добавленных исторических сведений. "
            "Верни только один JSON-объект без Markdown и комментариев строго такой формы: "
            '{"summary":"...","author_note":"...","official_source_urls":[],"facts":[{"claim_key":"...","existing_fact_id":"","text":"...","confidence":0.0,"source_urls":["https://..."]}]}.\\n'
            + json.dumps(
                {
                    "confirmed_identity": identity,
                    "voice_context": transcript,
                    "previous_claims_and_owner_decisions": previous,
                    "previously_considered_poi_facts": (poi_history or [])[:60],
                    "previously_processed_sources": (processed_source_history or [])[:80],
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
                if (
                    not isinstance(payload.get("official_source_urls", []), list)
                    or any(not isinstance(url, str) for url in payload.get("official_source_urls", []))
                    or not isinstance(payload.get("facts"), list)
                ):
                    raise ValueError
                # Per-fact semantic metadata is normalized fail-soft below. A single
                # malformed claim_key/confidence must not invalidate the whole model response.
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
            blocked_official_hosts = {
                "wikipedia.org", "wikimedia.org", "openstreetmap.org", "google.com",
            }
            official_urls: list[str] = []
            for raw_url in payload.get("official_source_urls", []):
                url = _norm_url(raw_url)
                host = (urlparse(url).hostname or "").lower().removeprefix("www.")
                blocked = any(host == domain or host.endswith("." + domain) for domain in blocked_official_hosts)
                if url in unique_chunks and not blocked and url not in official_urls:
                    official_urls.append(url)
            payload["official_source_urls"] = official_urls
            official_set = set(official_urls)
            grounding = [
                {**item, "type": "official" if item["url"] in official_set else item["type"]}
                for item in unique_chunks.values()
            ]
            return {
                "payload": payload,
                "grounding_sources": grounding,
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
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
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
        excluded = set(prior.get("identity_rejected_ids") or [])
        candidates = self._candidate_catalog(osm, wikipedia, excluded_ids=excluded)
        binding = prior.get("visual_identity") or {}
        if binding.get("status") in {"match", "owner_confirmed"}:
            saved_candidate = next((item for item in binding.get("candidates", []) if item.get("candidate_id") == binding.get("candidate_id")), None)
            if saved_candidate and saved_candidate["candidate_id"] not in {item["candidate_id"] for item in candidates}:
                candidates.append(saved_candidate)

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
                latest = json.loads(self._story_row(db, story_id)["research_json"] or "{}")
                if int(latest.get("identity_generation") or 0) != int(prior.get("identity_generation") or 0):
                    return
                db.execute(
                    "UPDATE stories SET state='needs_review',research_json=?,error_code='visual_identity_uncertain',"
                    "error_message='Выберите подходящий объект перед поиском фактов.',revision=revision+1,updated_at=? WHERE id=?",
                    (canonical(research), self.store.now(), story_id),
                )
            return

        with self.store.connection() as db:
            poi_history = prior_facts(db, identity, story_id)
            processed_source_history = processed_sources(db, identity)
        saved = self.store.checkpoint_get(job["id"], "grounded_research_v3")
        if saved is None:
            saved = await self._research_claims(
                story,
                transcript,
                identity,
                previous,
                poi_history,
                processed_source_history,
            )
            self.store.checkpoint_put(job["id"], "grounded_research_v3", saved)

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
                    "type": str(source.get("type") or "web"),
                    "title": str(source.get("title") or url),
                    "url": url,
                }

        incoming = saved.get("payload", {}).get("facts", [])[:32]
        is_refinement = bool(prior.get("input_revision"))
        old_decisions = {
            row["fact_id"]: bool(row["selected"])
            for row in previous
            if row.get("fact_id")
        }
        previous_fact_ids = set(old_decisions)
        normalized: list[dict[str, Any]] = []
        seen_claims: set[str] = set()
        for item in incoming:
            text = validated_model_fact_text(item.get("text"))
            if text is None:
                continue
            claim_key = normalized_claim_key(item.get("claim_key"))
            if claim_key is None:
                claim_key = "exact-text:" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:24]
            existing_fact_id = str(item.get("existing_fact_id") or "").strip()
            fact_id = (
                existing_fact_id
                if existing_fact_id in previous_fact_ids
                else model_fact_id(claim_key, text)
            )
            if fact_id in seen_claims:
                continue
            seen_claims.add(fact_id)
            sources: list[dict[str, Any]] = []
            for raw_url in item.get("source_urls", []) or []:
                url = _norm_url(raw_url)
                source = known.get(url)
                if not source:
                    continue
                supports = list(grounding_by_url.get(url, []))
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
            try:
                confidence = float(item.get("confidence", 0.0))
                if not math.isfinite(confidence):
                    raise ValueError
            except (TypeError, ValueError):
                confidence = 0.0
            normalized.append(
                {
                    "fact_id": fact_id,
                    "claim_key": claim_key,
                    "text": text,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "evidence_supported": evidence_supported,
                    "selected": selected,
                    "sources": sources,
                }
            )

        reusable_poi = [
            {**item, "selected": False}
            for item in poi_history
            if item.get("origin") == "poi_research"
        ]
        inventory = merge_model_fact_inventory([
            *reusable_poi,
            *[
                {
                    **item,
                    "evidence_supported": bool(item.get("evidence_supported")),
                }
                for item in previous
            ],
            *normalized,
        ])

        chosen = catalog.get(str(identity.get("candidate_id") or ""))
        place_name = str(identity.get("candidate_name") or (chosen or {}).get("name") or "").strip() or None
        author_note = str(saved.get("payload", {}).get("author_note") or "").strip()
        selected_for_draft = [
            {
                "fact_id": str(fact.get("fact_id") or ""),
                "text": str(fact.get("text") or ""),
                "sources": fact.get("sources") or [],
            }
            for fact in inventory
            if fact.get("selected") and fact.get("evidence_supported")
        ]
        compose = getattr(self.providers.gemini, "compose_publication", None)
        if not callable(compose):
            raise PermanentProviderError("Gemini publication composition capability is unavailable")
        composition = await compose(
            place_name=place_name,
            concept=str(prior.get("publication_concept") or ""),
            author_note=author_note,
            facts=selected_for_draft,
        )
        draft = str(composition.get("draft_text") or "").strip()
        if not draft:
            raise PermanentProviderError("Gemini publication composition returned empty draft")
        publication_concept = (
            str(prior.get("publication_concept") or "").strip()
            or str(composition.get("concept") or "").strip()
        )
        image_notes = "\n".join(str(fact["text"]) for fact in selected_for_draft[:6])
        if place_name and image_notes:
            image_notes = f"{place_name}:\n{image_notes}"

        with self.store.tx() as db:
            latest = json.loads(self._story_row(db, story_id)["research_json"] or "{}")
            if int(latest.get("identity_generation") or 0) != int(prior.get("identity_generation") or 0):
                return
            # Rebuild the topic inventory from the accumulated, quality-filtered,
            # semantically merged facts. This removes legacy title/snippet pollution
            # while preserving valid previously considered facts and decisions.
            db.execute("DELETE FROM facts WHERE story_id=?", (story_id,))
            for fact in inventory:
                db.execute(
                    "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        story_id,
                        fact["fact_id"],
                        fact["text"],
                        fact["confidence"],
                        int(fact["evidence_supported"]),
                        int(fact["selected"] and fact["evidence_supported"]),
                        canonical(fact["sources"]),
                    ),
                )
            persist_research_memory(
                db,
                identity,
                normalized,
                [
                    {
                        **source,
                        "supports": grounding_by_url.get(_norm_url(source.get("url")), []),
                    }
                    for source in grounding_sources
                    if isinstance(source, dict)
                ],
                "automatic_identity_research",
                self.store.now(),
            )
            all_fact_rows = list(db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (story_id,)))
            source_urls = {
                source["url"]
                for row in all_fact_rows
                for source in json.loads(row["sources_json"])
                if isinstance(source, dict) and source.get("url")
            }
            research = {
                **prior,
                "content_identity_changed": False,
                "input_revision": input_revision,
                "ordered_voice_ids": session_ids,
                "transcript": transcript,
                "visual_identity": identity,
                "osm": osm,
                "wikipedia": wikipedia,
                "grounding_sources": grounding_sources,
                "source_count": len(source_urls),
                "author_note": author_note,
                "publication_concept": publication_concept[:1200] or None,
                "draft_composed_by": "gemini_model",
                "draft_needs_refresh": False,
                "image_notes": image_notes,
                "poi_key": str(identity.get("candidate_id") or "") or None,
                "prior_poi_fact_count": len(poi_history),
                "claim_decisions": {row["fact_id"]: bool(row["selected"]) for row in all_fact_rows},
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

        conflict_input = [
            *[
                {
                    **item,
                    "evidence_supported": bool(item.get("evidence_supported")),
                }
                for item in previous
            ],
            *poi_history,
            *normalized,
        ]
        await analyze_fact_conflicts(
            self,
            story_id,
            str(identity.get("candidate_id") or "") or None,
            conflict_input,
            context={
                "place_name": place_name,
                "source": "research_job",
                "publication_concept": str(prior.get("publication_concept") or "")[:500],
            },
        )

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        research = json.loads(row["research_json"] or "{}")
        identity = research.get("visual_identity")
        from .identity_progress import from_history, current_projection
        result["identity_progress"] = current_projection(
            research.get("identity_progress") or from_history(db, row["id"], int(research.get("identity_generation") or 0)),
            identity if isinstance(identity, dict) else None)
        if isinstance(identity, dict):
            result["visual_identity"] = identity
        if research.get("input_revision"):
            result["research_revision"] = research["input_revision"]
        result["publication_concept"] = str(research.get("publication_concept") or "")[:1200] or None
        intent = db.execute(
            "SELECT state,request_json,scheduled_for FROM publish_intents "
            "WHERE story_id=? ORDER BY created_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if intent:
            try:
                request = json.loads(intent["request_json"] or "{}")
            except (TypeError, ValueError):
                request = {}
            aliases = [str(value)[:120] for value in request.get("destinations", []) if str(value).strip()][:8]
            existing_publication = result.get("publication") if isinstance(result.get("publication"), dict) else {}
            result["publication"] = {
                **existing_publication,
                "state": str(row["state"] if row["state"] in {"scheduled", "published"} else intent["state"]),
                "destinations": aliases,
                "scheduled_for": row["scheduled_for"] or intent["scheduled_for"],
            }
        else:
            result["publication"] = None
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
        test_alias = self.settings.publication_test_alias
        telegram = [
            item
            for item in result.get("destinations", [])
            if item.get("provider") == "telegram"
            and item.get("status") in {"supported", "needs_review"}
            and (
                item.get("alias") == test_alias if test_alias
                else not _is_internal_acceptance_destination(item.get("alias"))
            )
        ]
        for item in telegram:
            item["selected"] = True
        result["destinations"] = telegram
        result["mvp_providers"] = {"telegram": "active", "vk": "deferred", "max": "deferred"}
        return result

    def mutate_publish(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        test_alias = self.settings.publication_test_alias
        if test_alias and body.get("destinations") != [test_alias]:
            raise ConflictError("test_destination_only", "This Street Story deployment only publishes to its configured test group")
        text = body.get("text_override")
        if text is not None and len(str(text)) > 1024:
            raise ConflictError(
                "publish_text_too_long",
                "Telegram photo caption exceeds the current VibePublish 1024-character limit",
            )
        with self.store.connection() as db:
            story = self._story_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            if research.get("content_identity_changed"):
                raise ConflictError("identity_content_review_required", "После смены объекта нужно проверить и обновить текст публикации.")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise ConflictError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
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
        # Base publication logic refreshes a needs_review Telegram destination
        # through a no-dispatch preview before the authoritative supported check.
        # Do not reject that refreshable state in the MVP wrapper.
        return await super()._run_publish(job)


class MvpResearchStreetStoryService(MvpResearchMixin, MvpProductStreetStoryService):
    pass
