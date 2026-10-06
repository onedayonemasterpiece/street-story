from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlparse


from .errors import MalformedProviderResponse
from .camera_hints import reference_order, model_camera_hints
from .fact_conflicts import analyze_fact_conflicts
from .fact_ledger import (
    candidate_assertion_id,
    fact_revision_bundle,
    persist_fact_candidates,
    persist_fact_relation_events,
    refresh_review_status,
    revision_bundle_issues,
    set_owner_selection,
)
from .model_facts import merge_model_fact_inventory, normalized_claim_key, validated_model_fact_text
from .identity_candidate_policy import wikipedia_identity_eligible
from .gemini import GeminiUnavailable
from .identity_lifecycle import IdentityLifecycleMixin
from .identity_visual import identify_nearest
from .mvp import MvpProductStreetStoryService
from .poi_memory import persist_research_memory, prior_facts, processed_sources
from .providers import PermanentProviderError
from .research_runs import (
    begin_research_run,
    manifest_complete,
    register_discovered_source,
    run_manifest,
    set_run_state,
)
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
            research = json.loads(story["research_json"] or "{}")
            identity = research.get("visual_identity") or {}
            sessions = list(
                db.execute(
                    "SELECT session_id,kind,manifest_json,created_at FROM voice_sessions "
                    "WHERE story_id=? AND recording_finished=1 ORDER BY created_at,session_id",
                    (story_id,),
                )
            )
            if not sessions and identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "research_voice_required", "At least one completed voice message is required"
                )
            ordered_ids = [row["session_id"] for row in sessions]
            request_revision = int(research.get("fact_request_revision") or 0) + 1
            request_goal = str(body.get("coverage_goal") or body.get("goal") or "").strip()[:1600]
            request_scope = str(body.get("extraction_scope") or "").strip()[:1600] or None
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
                "request_revision": request_revision,
                "identity_generation": int(research.get("identity_generation") or 0),
                "coverage_goal": request_goal,
                "extraction_scope": request_scope,
            }
            input_revision = digest(revision_basis)
            request_payload = {
                "voice_session_ids": ordered_ids,
                "input_revision": input_revision,
                "confirmed_candidate_id": candidate_id,
                "coverage_goal": request_goal,
                "extraction_scope": request_scope,
                "request_revision": request_revision,
                "identity_generation": revision_basis["identity_generation"],
                "photo_sha256": story["photo_sha256"],
            }
            if not ordered_ids:
                request_payload["live_transcript"] = request_goal or "Найди пригодные новые факты о подтверждённом объекте."
            research["fact_request_revision"] = request_revision
            active = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='research' "
                "AND state IN ('ready','retry','running') ORDER BY created_at LIMIT 1", (story_id,)
            ).fetchone()
            if active:
                # Keep the running attempt's frozen input/checkpoints intact.
                # Its terminal commit (or recovery scheduler) consumes this
                # bounded follow-up. Distinct requested aspects remain queued;
                # exact repeated goals join the same follow-up.
                pending = research.get("pending_fact_request")
                if isinstance(pending, dict):
                    queued = pending.pop("queued_requests", [])
                    requests = [pending, *queued]
                    def request_key(item):
                        return (item.get("coverage_goal"), item.get("extraction_scope"), item.get("confirmed_candidate_id"))
                    same = next((i for i, item in enumerate(requests) if request_key(item) == request_key(request_payload)), None)
                    if same is None:
                        requests.append(request_payload)
                    else:
                        requests[same] = request_payload
                    pending = requests[0]
                    if len(requests) > 1:
                        pending["queued_requests"] = requests[1:]
                    research["pending_fact_request"] = pending
                else:
                    research["pending_fact_request"] = request_payload
                db.execute("UPDATE stories SET research_json=?,updated_at=? WHERE id=?",
                           (canonical(research), self.store.now(), story_id))
                return self._story_repr(db, self._story_row(db, story_id))
            self._enqueue_job(
                db,
                story_id,
                "research",
                f"research-explicit:{input_revision}",
                request_payload,
            )
            now = self.store.now()
            db.execute(
                "UPDATE stories SET state='researching',research_json=?,revision=revision+1,error_code=NULL,error_message=NULL,updated_at=? "
                "WHERE id=?",
                (canonical(research), now, story_id),
            )
            return self._story_repr(db, self._story_row(db, story_id))

    def _schedule_joined_fact_request(self, db, story_id: str) -> bool:
        """Consume one durable joined request after an attempt becomes terminal.

        Recovery may call this once no active research job remains. The normal
        worker calls it after its final commit, before marking its job done.
        """
        story = self._story_row(db, story_id)
        research = json.loads(story["research_json"] or "{}")
        pending = research.pop("pending_fact_request", None)
        if not isinstance(pending, dict):
            return False
        queued = pending.pop("queued_requests", [])
        current_generation = int(research.get("identity_generation") or 0)
        valid = (int(pending.get("identity_generation") or 0) == current_generation
                 and pending.get("photo_sha256") == story["photo_sha256"]
                 and not research.get("fact_research_cancelled"))
        if valid:
            self._enqueue_job(db, story_id, "research", f"research-explicit:{pending['input_revision']}", pending)
            if queued:
                research["pending_fact_request"] = queued[0]
                if len(queued) > 1:
                    research["pending_fact_request"]["queued_requests"] = queued[1:]
        db.execute("UPDATE stories SET research_json=?,state=CASE WHEN ? THEN 'researching' ELSE state END,updated_at=? WHERE id=?",
                   (canonical(research), int(valid), self.store.now(), story_id))
        return valid

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
            set_owner_selection(db, story_id, selected_ids, self.store.now())
            research = json.loads(story["research_json"] or "{}")
            decisions = {
                row["fact_id"]: bool(row["owner_selected"])
                for row in db.execute(
                    "SELECT assertion_id AS fact_id,owner_selected FROM fact_assertions "
                    "WHERE story_id=? ORDER BY rowid",
                    (story_id,),
                )
            }
            research["claim_decisions"] = decisions
            selected_text = [
                str(row["text"])
                for row in db.execute(
                    "SELECT f.text FROM facts f JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? AND a.owner_selected=1 AND a.eligibility='eligible' "
                    "AND f.evidence_supported=1 ORDER BY f.rowid",
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
                    "SELECT a.assertion_id AS fact_id FROM fact_assertions a "
                    "JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id "
                    "WHERE a.story_id=? AND a.owner_selected=1 AND f.evidence_supported=1 "
                    "ORDER BY f.rowid",
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
        from .identity_entity_aliases import enrich_entity_links
        return enrich_entity_links(shortlist[:16], osm, wikipedia)

    async def _candidate_reference_images(self, candidates, limit=6, *, story_id=None, evidence=None):
        from .identity_references import reference_images
        return await reference_images(self, candidates, limit, story_id=story_id, evidence=evidence)

    async def _identify_photo(self, story, transcript, candidates):
        if getattr(self.providers, 'research', None) is not None:
            return self._deferred_visual_assignment()
        return await identify_nearest(self, story, transcript, candidates)

    @staticmethod
    def _deferred_visual_assignment():
        # Hypotheses are retained by identity_lifecycle. Actual pixels and a
        # verified model verdict belong to the shared durable visual queue.
        return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                'observations': [], 'alternative_candidate_ids': [],
                '_references_sent': [], '_comparison_deferred': True}

    async def _identify_photo_batch(
        self,
        story: dict[str, Any],
        transcript: str,
        candidates: list[dict[str, Any]],
        reference_limit: int = 2,
    ) -> dict[str, Any]:
        if getattr(self.providers, 'research', None) is not None:
            return self._deferred_visual_assignment()
        custom = getattr(self.providers.gemini, "identify_photo", None)
        if callable(custom):
            return await custom(self._source_photo_bytes(story["id"]), story["photo_mime_type"], transcript, candidates)
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
                          "physical_alternatives": story.get('_identity_shortlist') or candidates,
                          "capture_hints": model_camera_hints(story.get('_camera_hints') or {})}, ensure_ascii=False)
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_json_schema=schema,
            system_instruction="Все observations пиши по-русски. Название города или района само по себе не является идентификацией конкретного здания. Несколько изображений одного объекта — не разные альтернативные объекты."
        )
        photo = self._source_photo_bytes(story["id"])
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
            types.Part.from_bytes(data=photo, mime_type=story["photo_mime_type"]),
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
            record_identity_event(self, story['id'], 'identity_images_reviewed', {
                'generation': story.get('_identity_generation', 0),
                'reference_urls': [item['source_url'] for item in reference_evidence],
                'image_count': len(reference_images), 'comparison_model': model or self.settings.gemini_model,
            })
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
        wikipedia_evidence: list[dict[str, Any]] | None = None,
        research_run_id: str | None = None,
    ) -> dict[str, Any]:
        custom = getattr(self.providers.gemini, "research_v2", None)
        if callable(custom):
            return await custom(self._source_photo_bytes(story["id"]), story["photo_mime_type"], transcript, identity, previous)
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
                    "wikipedia_evidence": [
                        {
                            "title": str(page.get("title") or "")[:240],
                            "url": _norm_url(page.get("url")),
                            "extract": str(page.get("extract") or "")[:4000],
                        }
                        for page in (wikipedia_evidence or [])[:12]
                        if _norm_url(page.get("url")) and str(page.get("extract") or "").strip()
                    ],
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

            candidate_facts = [
                {
                    "claim_key": str(item.get("claim_key") or ""),
                    "text": str(item.get("text") or ""),
                    "source_urls": [
                        _norm_url(url)
                        for url in (item.get("source_urls") or [])
                        if _norm_url(url)
                    ],
                }
                for item in payload.get("facts") or []
                if isinstance(item, dict)
            ]
            binding_sources: list[dict[str, Any]] = []
            bindings: dict[int, list[str]] = {}
            support_by_ref: dict[str, dict[str, Any]] = {}
            binding_status = "no_facts"

            binder = getattr(gemini, "_bind_facts_to_evidence", None)
            support_ref = getattr(gemini, "_support_evidence_ref", None)
            merge_evidence = getattr(gemini, "_merge_evidence_sources", None)

            if candidate_facts and callable(binder) and callable(support_ref) and callable(merge_evidence):
                google_sources: list[dict[str, Any]] = []
                for item in unique_chunks.values():
                    url = item["url"]
                    source_supports = [
                        {
                            **support,
                            "evidence_ref": support_ref(url, support),
                        }
                        for support in supports
                        if _norm_url(support.get("source_url")) == url
                        and str(support.get("text") or "").strip()
                    ]
                    google_sources.append({
                        **item,
                        "supports": source_supports,
                    })

                wiki_sources: list[dict[str, Any]] = []
                for page in wikipedia_evidence or []:
                    url = _norm_url(page.get("url"))
                    extract = str(page.get("extract") or "").strip()
                    if not url or not extract:
                        continue
                    support = {
                        "kind": "wikipedia_extract",
                        "source_url": url,
                        "text": extract[:9000],
                    }
                    support["evidence_ref"] = support_ref(url, support)
                    wiki_sources.append({
                        "type": "wikipedia",
                        "title": str(page.get("title") or url),
                        "url": url,
                        "supports": [support],
                    })

                binding_sources = merge_evidence(google_sources, wiki_sources)
                try:
                    bindings = await binder(
                        api_key,
                        timeout,
                        candidate_facts,
                        binding_sources,
                        model=None,
                        quota=None,
                    )
                    binding_status = "bound"
                except (GeminiUnavailable, PermanentProviderError, MalformedProviderResponse):
                    bindings = {}
                    binding_status = "unavailable"

                for source in binding_sources:
                    url = _norm_url(source.get("url"))
                    for support in source.get("supports") or []:
                        if not isinstance(support, dict):
                            continue
                        evidence_ref = str(support.get("evidence_ref") or "").strip()
                        if evidence_ref:
                            support_by_ref[evidence_ref] = {
                                "source_url": url,
                                "support": support,
                            }
            elif candidate_facts:
                binding_status = "compatibility_unavailable"
                binding_sources = [
                    {**item, "supports": [
                        support
                        for support in supports
                        if _norm_url(support.get("source_url")) == item["url"]
                        and str(support.get("text") or "").strip()
                    ]}
                    for item in unique_chunks.values()
                ]
            else:
                binding_sources = [
                    {**item, "supports": [
                        support
                        for support in supports
                        if _norm_url(support.get("source_url")) == item["url"]
                        and str(support.get("text") or "").strip()
                    ]}
                    for item in unique_chunks.values()
                ]

            bound_facts: list[dict[str, Any]] = []
            for index, item in enumerate(payload.get("facts") or []):
                if not isinstance(item, dict):
                    continue
                if binding_status == "bound":
                    refs = [
                        ref
                        for ref in bindings.get(index, [])
                        if ref in support_by_ref
                    ]
                    bound_urls: list[str] = []
                    for evidence_ref in refs:
                        url = str(support_by_ref[evidence_ref]["source_url"] or "")
                        if url and url not in bound_urls:
                            bound_urls.append(url)
                    bound_facts.append({
                        **item,
                        "source_urls": bound_urls,
                        "evidence_refs": refs,
                    })
                elif binding_status == "unavailable":
                    bound_facts.append({
                        **item,
                        "source_urls": [],
                        "evidence_refs": [],
                    })
                else:
                    bound_facts.append(dict(item))
            payload["facts"] = bound_facts
            payload["evidence_binding_status"] = binding_status

            supports = [
                {
                    **support,
                    "source_url": _norm_url(support.get("source_url")),
                    **(
                        {"evidence_ref": str(support.get("evidence_ref") or "")}
                        if str(support.get("evidence_ref") or "").strip()
                        else {}
                    ),
                }
                for source in binding_sources
                for support in (source.get("supports") or [])
                if isinstance(support, dict)
                and _norm_url(support.get("source_url"))
                and str(support.get("text") or "").strip()
            ]

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
                {
                    **item,
                    "type": (
                        "official"
                        if _norm_url(item.get("url")) in official_set
                        else str(item.get("type") or "web")
                    ),
                }
                for item in binding_sources
            ]
            return {
                "payload": payload,
                "grounding_sources": grounding,
                "grounding_supports": supports,
                "research_run_id": research_run_id,
            }

        return await gemini.executor.execute("grounded_research", call)

    async def _run_research(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        payload = json.loads(job["payload_json"] or "{}")
        session_ids = [str(value) for value in payload.get("voice_session_ids", [])]
        live_transcript = str(payload.get("live_transcript") or "").strip()
        input_revision = str(payload.get("input_revision") or "")
        if not input_revision:
            raise PermanentProviderError("Explicit research snapshot is incomplete")

        with self.store.connection() as db:
            story = dict(self._story_row(db, story_id))
            prior = json.loads(story["research_json"] or "{}")
            previous = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": str(row["semantic_key"] or ""),
                    "text": row["text"],
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT f.*,a.semantic_key FROM facts f "
                    "LEFT JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? ORDER BY f.rowid",
                    (story_id,),
                )
            ]
        if (payload.get("photo_sha256") is not None and payload["photo_sha256"] != story["photo_sha256"]
                or payload.get("identity_generation") is not None
                and int(payload["identity_generation"]) != int(prior.get("identity_generation") or 0)):
            return
        if prior.get('fact_research_cancelled'):
            return
        if not session_ids and not live_transcript and (prior.get('visual_identity') or {}).get('status') not in {'match','owner_confirmed'}:
            raise PermanentProviderError('Explicit unidentified research requires initial author context')

        if live_transcript:
            transcript = live_transcript[:12000]
        else:
            transcripts: list[str] = []
            for session_id in session_ids:
                transcripts.append(await self._transcribe_session(session_id))
            transcript = "\n\n".join(text.strip() for text in transcripts if text.strip())
        request_goal = str(payload.get("coverage_goal") or "").strip()
        if request_goal and request_goal != transcript:
            transcript += "\n\nТекущий запрос исследования: " + request_goal

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
                # A request joined while identity was in flight must remain
                # visible to the recovery scheduler even when identification
                # needs more evidence and this legacy attempt stops here.
                for field in ("fact_request_revision", "pending_fact_request"):
                    if field in latest:
                        research[field] = latest[field]
                db.execute(
                    "UPDATE stories SET state='needs_review',research_json=?,error_code='visual_identity_uncertain',"
                    "error_message='Выберите подходящий объект перед поиском фактов.',revision=revision+1,updated_at=? WHERE id=?",
                    (canonical(research), self.store.now(), story_id),
                )
            return

        expected_story_revision = int(story.get("revision") or 0)
        expected_identity_generation = int(prior.get("identity_generation") or 0)
        run_id = "research_" + hashlib.sha256(
            f"{job['id']}:{input_revision}".encode("utf-8")
        ).hexdigest()[:24]
        with self.store.tx() as db:
            begin_research_run(
                db,
                story_id=story_id,
                poi_key=str(identity.get("candidate_id") or "") or None,
                goal=(
                    str(payload.get("coverage_goal") or "").strip()
                    or str(prior.get("publication_concept") or "").strip()
                    or transcript[:1600]
                    or "source-backed research"
                ),
                scope=payload.get("extraction_scope"),
                expected_story_revision=expected_story_revision,
                identity_generation=expected_identity_generation,
                run_id=run_id,
                now=self.store.now(),
            )
            set_run_state(
                db,
                run_id,
                "discovering",
                detail="automatic_research",
                now=self.store.now(),
            )

        with self.store.connection() as db:
            poi_history = prior_facts(db, identity, story_id)
            processed_source_history = processed_sources(db, identity)
        researcher = getattr(self.providers, 'research', None)
        if callable(getattr(researcher, 'extract_fact_page', None)) and getattr(researcher, 'facts_available', False):
            from .headless_facts import HeadlessFacts
            await HeadlessFacts(self).run(job, run_id, request_goal or 'source-backed research',
                str(payload.get('extraction_scope') or request_goal or 'source-backed research'))
            with self.store.tx() as db:
                current = self._story_row(db, story_id)
                latest = json.loads(current['research_json'] or '{}')
                if current['photo_sha256'] != story['photo_sha256'] or int(latest.get('identity_generation') or 0) != expected_identity_generation:
                    return
                latest['input_revision'] = input_revision
                db.execute('UPDATE stories SET research_json=?,state=CASE WHEN draft_text IS NULL THEN \'facts_ready\' ELSE state END,updated_at=? WHERE id=?',
                           (canonical(latest),self.store.now(),story_id))
            return
        saved = self.store.checkpoint_get(job["id"], "grounded_research_v3")
        if saved is None:
            saved = await self._research_claims(
                story,
                transcript,
                identity,
                previous,
                poi_history,
                processed_source_history,
                wikipedia_evidence=wikipedia,
                research_run_id=run_id,
            )
            self.store.checkpoint_put(job["id"], "grounded_research_v3", saved)

        grounding_sources = saved.get("grounding_sources", [])
        with self.store.tx() as db:
            for source in grounding_sources:
                if not isinstance(source, dict):
                    continue
                url = _norm_url(source.get("url"))
                if not url:
                    continue
                register_discovered_source(
                    db,
                    run_id=run_id,
                    url=url,
                    title=str(source.get("title") or url),
                    status="snippet_only",
                    now=self.store.now(),
                )
            set_run_state(
                db,
                run_id,
                "reconciling",
                detail="grounding_received",
                now=self.store.now(),
            )

        with self.store.connection() as db:
            current = dict(self._story_row(db, story_id))
            current_research = json.loads(current.get("research_json") or "{}")
        if (
            int(current.get("revision") or 0) != expected_story_revision
            or int(current_research.get("identity_generation") or 0) != expected_identity_generation
        ):
            with self.store.tx() as db:
                set_run_state(
                    db,
                    run_id,
                    "cancelled",
                    detail="story_revision_or_identity_changed",
                    now=self.store.now(),
                    completed=True,
                )
            return

        grounding_sources = saved.get("grounding_sources", [])
        grounding_supports = saved.get("grounding_supports", [])
        grounding_by_url: dict[str, list[dict[str, str]]] = {}
        for support in grounding_supports:
            url = _norm_url(support.get("source_url"))
            if url:
                grounding_by_url.setdefault(url, []).append(support)
        binding_status = str(saved.get("payload", {}).get("evidence_binding_status") or "")
        binding_enforced = binding_status in {"bound", "unavailable"}

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
                previous_known = known.get(url) or {}
                known[url] = {
                    "type": str(source.get("type") or previous_known.get("type") or "web"),
                    "title": str(source.get("title") or previous_known.get("title") or url),
                    "url": url,
                    "excerpt": str(previous_known.get("excerpt") or ""),
                }

        incoming = [
            item
            for item in (saved.get("payload", {}).get("facts", []) or [])
            if isinstance(item, dict)
        ]
        previous_ids = {
            str(item.get("fact_id") or "")
            for item in previous
            if str(item.get("fact_id") or "")
        }
        reconciliation_matches: dict[int, str] = {
            index: str(item.get("existing_fact_id") or "")
            for index, item in enumerate(incoming)
            if str(item.get("existing_fact_id") or "") in previous_ids
        }
        reconciliation_decisions: list[dict[str, Any]] = [
            {
                "incoming_index": index,
                "relation": "equivalent",
                "existing_fact_id": fact_id,
                "rationale": "Upstream extraction explicitly referenced this durable fact ID.",
                "model_name": "upstream_existing_fact_id",
                "prompt_version": "fact-identity-reconciliation-v1",
            }
            for index, fact_id in sorted(reconciliation_matches.items())
        ]
        reconciliation_meta: dict[str, Any] = {
            "status": "not_needed",
            "pages_reviewed": 0,
            "existing_fact_count": len(previous),
            "incoming_fact_count": len(incoming),
        }
        reconciler = getattr(self.providers.gemini, "reconcile_fact_identities", None)
        if incoming and previous and callable(reconciler):
            try:
                reconciliation = await reconciler(incoming, previous)
                reconciliation_matches = {
                    int(index): str(fact_id)
                    for index, fact_id in (reconciliation.get("matches") or {}).items()
                    if str(fact_id) in previous_ids
                }
                reconciliation_decisions = [
                    dict(item)
                    for item in (reconciliation.get("decisions") or [])
                    if isinstance(item, dict)
                ]
                reconciliation_meta = {
                    "status": "complete" if reconciliation.get("complete") is True else "partial",
                    "pages_reviewed": int(reconciliation.get("pages_reviewed") or 0),
                    "existing_fact_count": int(
                        reconciliation.get("existing_fact_count") or len(previous)
                    ),
                    "incoming_fact_count": int(
                        reconciliation.get("incoming_fact_count") or len(incoming)
                    ),
                    "matched_count": len(reconciliation_matches),
                    "unmatched_count": int(reconciliation.get("unmatched_count") or 0),
                }
            except (
                GeminiUnavailable,
                MalformedProviderResponse,
                PermanentProviderError,
            ) as exc:
                reconciliation_meta = {
                    **reconciliation_meta,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
        elif incoming and previous:
            reconciliation_meta["status"] = "compatibility_unavailable"

        # Live-created drafts and hydrated POI facts do not necessarily have a
        # legacy voice research input_revision. Their owner decisions still
        # make this an accumulating request rather than a new publication.
        is_refinement = bool(prior.get("input_revision") or previous or story.get("draft_text")
                             or prior.get("publication_concept"))
        old_decisions = {
            row["fact_id"]: bool(row["selected"])
            for row in previous
            if row.get("fact_id")
        }
        previous_fact_ids = set(old_decisions)
        normalized: list[dict[str, Any]] = []
        seen_claims: set[str] = set()
        for item_index, item in enumerate(incoming):
            text = validated_model_fact_text(item.get("text"))
            if text is None:
                continue
            claim_key = normalized_claim_key(item.get("claim_key"))
            if claim_key is None:
                claim_key = "exact-text:" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:24]
            provided_existing_fact_id = str(item.get("existing_fact_id") or "").strip()
            existing_fact_id = reconciliation_matches.get(item_index) or (
                provided_existing_fact_id
                if provided_existing_fact_id in previous_fact_ids
                else ""
            )
            fact_id = (
                existing_fact_id
                if existing_fact_id
                else candidate_assertion_id(claim_key, text)
            )
            if fact_id in seen_claims:
                continue
            seen_claims.add(fact_id)
            sources: list[dict[str, Any]] = []
            evidence_refs = {
                str(value).strip()
                for value in (item.get("evidence_refs") or [])
                if str(value).strip()
            }
            for raw_url in item.get("source_urls", []) or []:
                url = _norm_url(raw_url)
                source = known.get(url)
                if not source:
                    continue
                if binding_enforced:
                    supports = [
                        support
                        for support in grounding_by_url.get(url, [])
                        if isinstance(support, dict)
                        and str(support.get("evidence_ref") or "") in evidence_refs
                        and str(support.get("text") or "").strip()
                    ]
                else:
                    supports = [
                        support
                        for support in grounding_by_url.get(url, [])
                        if isinstance(support, dict)
                        and str(support.get("text") or "").strip()
                    ]
                    if not supports:
                        excerpt = str(source.get("excerpt") or "").strip()
                        if excerpt:
                            supports = [{
                                "kind": "wikipedia_extract",
                                "source_url": url,
                                "text": excerpt,
                            }]
                if not supports:
                    continue
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
                    "existing_fact_id": existing_fact_id or None,
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
        preserve_draft = (is_refinement and previous_identity is not None
                          and previous_identity.get("candidate_id") == identity.get("candidate_id"))
        if preserve_draft:
            # Research adds evidence. The author controls when an existing
            # publication draft/concept is recomposed from the expanded pool.
            draft = story.get("draft_text")
            publication_concept = prior.get("publication_concept")
        else:
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
            current_row = dict(self._story_row(db, story_id))
            latest = json.loads(current_row["research_json"] or "{}")
            if (
                int(current_row.get("revision") or 0) != expected_story_revision
                or int(latest.get("identity_generation") or 0) != expected_identity_generation
            ):
                set_run_state(
                    db,
                    run_id,
                    "cancelled",
                    detail="story_revision_or_identity_changed_before_commit",
                    now=self.store.now(),
                    completed=True,
                )
                return
            now = self.store.now()
            persist_fact_relation_events(
                db,
                story_id=story_id,
                run_id=run_id,
                incoming_facts=incoming,
                decisions=reconciliation_decisions,
                now=now,
            )
            persist_fact_candidates(
                db,
                story_id=story_id,
                poi_key=str(identity.get("candidate_id") or "") or None,
                facts=normalized,
                run_id=run_id,
                batch_id="grounded_research_v3",
                model_name="configured_research_model",
                prompt_version="automatic-research-ledger-v1",
                now=now,
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
                research_run_id=run_id,
            )
            all_fact_rows = list(db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (story_id,)))
            draft_fact_ids = [
                str(item.get("fact_id") or "")
                for item in selected_for_draft
                if str(item.get("fact_id") or "")
            ]
            draft_fact_revisions = (prior.get("draft_fact_revisions", []) if preserve_draft
                                    else fact_revision_bundle(db, story_id, draft_fact_ids))
            source_urls = {
                source["url"]
                for row in all_fact_rows
                for source in json.loads(row["sources_json"])
                if isinstance(source, dict) and source.get("url")
            }
            research = {
                **latest,
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
                "publication_concept": publication_concept if preserve_draft else publication_concept[:1200] or None,
                "draft_composed_by": prior.get("draft_composed_by") if preserve_draft else "gemini_model",
                "draft_needs_refresh": prior.get("draft_needs_refresh", False) if preserve_draft else False,
                "draft_stale_reason": prior.get("draft_stale_reason") if preserve_draft else None,
                "draft_fact_revisions": draft_fact_revisions,
                "image_notes": image_notes,
                "poi_key": str(identity.get("candidate_id") or "") or None,
                "research_run_id": run_id,
                "prior_poi_fact_count": len(poi_history),
                "claim_decisions": {row["fact_id"]: bool(row["selected"]) for row in all_fact_rows},
                "fact_reconciliation": reconciliation_meta,
            }
            set_run_state(
                db,
                run_id,
                "verifying",
                detail="facts_persisted",
                now=self.store.now(),
            )
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
        with self.store.tx() as db:
            now = self.store.now()
            refresh_review_status(db, story_id, now)
            manifest = run_manifest(db, run_id)
            scan = db.execute(
                "SELECT status,coverage_complete FROM fact_conflict_scans "
                "WHERE story_id=? ORDER BY id DESC LIMIT 1",
                (story_id,),
            ).fetchone()
            review_ok = bool(
                scan
                and str(scan["status"]) in {"ok", "no_candidates"}
                and int(scan["coverage_complete"] or 0) == 1
            )
            reconciliation_complete = reconciliation_meta.get("status") in {"not_needed", "complete"}
            complete = manifest_complete(manifest) and review_ok and reconciliation_complete
            set_run_state(
                db,
                run_id,
                "completed" if complete else "partial",
                detail=(
                    "manifest_review_and_reconciliation_complete"
                    if complete
                    else (
                        "fact_reconciliation_incomplete"
                        if not reconciliation_complete
                        else "manifest_or_semantic_review_incomplete"
                    )
                ),
                now=now,
                completed=True,
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
            from .fact_ledger import eligible_selected_fact_ids, selected_eligibility_issues
            issues = selected_eligibility_issues(db, story_id)
            if issues:
                raise ConflictError(
                    "fact_review_required",
                    "Selected facts still need semantic review or arbitration before publication.",
                )
            selected_ids = eligible_selected_fact_ids(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            if revision_bundle_issues(
                db,
                story_id,
                research.get("draft_fact_revisions"),
                expected_fact_ids=selected_ids,
            ):
                raise ConflictError(
                    "publication_text_stale",
                    "Evidence revisions used by the publication text changed; refresh the draft.",
                )
            visual = json.loads(story["visual_context_json"] or "{}")
            visual_fact_ids = [
                str(item.get("fact_id") or "")
                for item in (visual.get("selected_facts") or [])
                if isinstance(item, dict) and str(item.get("fact_id") or "")
            ]
            if revision_bundle_issues(
                db,
                story_id,
                visual.get("fact_revision_bundle"),
                expected_fact_ids=visual_fact_ids,
            ):
                raise ConflictError(
                    "visual_not_ready",
                    "Evidence revisions used by the visual changed; regenerate the visual.",
                )
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
                    "selected_fact_ids": eligible_selected_fact_ids(db, story_id),
                    "fact_revision_bundle": fact_revision_bundle(
                        db,
                        story_id,
                        eligible_selected_fact_ids(db, story_id),
                    ),
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
