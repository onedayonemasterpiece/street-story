from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .product import ProductStreetStoryService, _receipt
from .providers import PermanentProviderError, RetryableProviderError
from .service import ConflictError, InvalidStateError, _durable_write, canonical, digest


class MvpProductStreetStoryService(ProductStreetStoryService):
    """Narrow MVP correctness layer over the proven product service.

    This class intentionally leaves capture, ASR, persistence and provider boundaries
    in the existing core. It only freezes the visual input snapshot and makes the
    owner-controlled prompt/reconciliation semantics explicit.
    """

    PROMPT_VERSION = "street-story-image-v1"
    PROMPT_TOKEN = "{{CITY_NOTE_THEMES}}"

    def _prompt_template(self) -> tuple[str, str]:
        path = Path(__file__).resolve().parents[1] / "prompts" / "street-story-image-v1.txt"
        try:
            template = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InvalidStateError(
                "visual_prompt_missing", "Street Story owner image prompt is unavailable"
            ) from exc
        if template.count(self.PROMPT_TOKEN) != 1 or "{{OWNER_FINAL_IMAGE_PROMPT}}" in template:
            raise InvalidStateError(
                "visual_prompt_invalid",
                "Street Story owner image prompt is not the versioned production template",
            )
        return template, hashlib.sha256(template.encode("utf-8")).hexdigest()

    def _visual_brief(self, story: dict[str, Any], context: dict[str, Any]) -> str:
        template, _ = self._prompt_template()
        selected = context.get("selected_facts") if isinstance(context.get("selected_facts"), list) else []
        notes: list[str] = []
        place = str(story.get("place_name") or context.get("place_name") or "").strip()
        if place:
            notes.append(f"Место: {place}.")
        intent = str(context.get("user_voice_intent") or "").strip()
        if intent:
            notes.append("Авторское наблюдение: " + intent[:600])
        facts = [
            str(item.get("text") or "").strip()
            for item in selected
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if facts:
            notes.append(
                "Проверенные факты: " + " ".join(f"• {fact[:240]}" for fact in facts[:6])
            )
        if not notes:
            notes.append(
                "Передай атмосферу и узнаваемую городскую среду без выдуманных исторических утверждений."
            )
        themes = "\n".join(notes)
        brief = template.replace(self.PROMPT_TOKEN, themes)
        if re.search(r"[А-Яа-яЁё]", themes):
            brief += (
                "\n\nLanguage rule: all visible handwritten city notes and annotations in the "
                "generated image must be in Russian. Keep them concise and legible."
            )
        return brief

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        context = json.loads(row["visual_context_json"] or "{}")
        visual = result.setdefault("visual", {})
        for key in ("prompt_version", "prompt_sha256", "content_revision"):
            if context.get(key) is not None:
                visual[key] = context[key]
        return result

    def mutate_visual(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        _, template_sha = self._prompt_template()
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if self._idem(db, key, "visual", req_digest, "story", story_id):
                return self._story_repr(db, story)

            selected_ids = [str(value) for value in body.get("selected_fact_ids", [])]
            facts = {
                row["fact_id"]: row
                for row in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))
            }
            selected_facts: list[dict[str, Any]] = []
            for fact_id in selected_ids:
                row = facts.get(fact_id)
                if row is None or not row["evidence_supported"]:
                    raise ConflictError(
                        "visual_fact_unsupported", f"Fact {fact_id} is not externally supported"
                    )
                selected_facts.append(
                    {
                        "fact_id": fact_id,
                        "text": row["text"],
                        "sources": json.loads(row["sources_json"]),
                    }
                )

            research = json.loads(story["research_json"] or "{}")
            voice_ids = [
                row["session_id"]
                for row in db.execute(
                    "SELECT session_id FROM voice_sessions WHERE story_id=? AND recording_finished=1 "
                    "ORDER BY created_at,session_id",
                    (story_id,),
                )
            ]
            frozen = {
                "source_photo_sha256": story["photo_sha256"],
                "ordered_voice_ids": voice_ids,
                "input_revision": research.get("input_revision"),
                "visual_identity": research.get("visual_identity"),
                "selected_facts": [
                    {"fact_id": item["fact_id"], "text": item["text"]}
                    for item in selected_facts
                ],
                "prompt_version": self.PROMPT_VERSION,
                "prompt_sha256": template_sha,
            }
            content_revision = digest(frozen)
            context = {
                **frozen,
                "content_revision": content_revision,
                "selected_facts": selected_facts,
                "place_name": story["place_name"],
                "user_voice_intent": research.get("transcript", ""),
            }
            context["brief"] = self._visual_brief(dict(story), context)
            now = self.store.now()
            db.execute(
                "UPDATE stories SET state='visual_processing',visual_context_json=?,"
                "vibepublish_asset_ref=NULL,processed_image_url=NULL,error_code=NULL,error_message=NULL,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (canonical(context), now, story_id),
            )
            self._enqueue_job(
                db,
                story_id,
                "visual",
                f"visual:{key}",
                {"content_revision": content_revision, "selected_fact_ids": selected_ids},
            )
            return self._story_repr(db, self._story_row(db, story_id))

    def _merge_visual_context(
        self, story_id: str, content_revision: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            context = json.loads(row["visual_context_json"] or "{}")
            if context.get("content_revision") != content_revision:
                return None
            context.update(updates)
            db.execute(
                "UPDATE stories SET visual_context_json=?,updated_at=? WHERE id=?",
                (canonical(context), self.store.now(), story_id),
            )
            return context

    async def _run_visual(self, job: dict[str, Any]) -> None:
        story_id = job["story_id"]
        payload = json.loads(job["payload_json"] or "{}")
        content_revision = str(payload.get("content_revision") or "")
        try:
            await self.providers.vibepublish.bootstrap()
            with self.store.connection() as db:
                story = dict(self._story_row(db, story_id))
            context = json.loads(story["visual_context_json"] or "{}")
            if not content_revision or context.get("content_revision") != content_revision:
                return
            brief = str(context.get("brief") or "")
            if not brief:
                raise PermanentProviderError("Frozen Street Story visual prompt is missing")

            source_asset = str(context.get("source_asset_ref") or "")
            if not source_asset:
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
                merged = self._merge_visual_context(
                    story_id,
                    content_revision,
                    {"source_asset_ref": source_asset, "source_ingress_key": ingress_key},
                )
                if merged is None:
                    return
                context = merged

            command = context.get("visual_command")
            visual_key = str(context.get("visual_request_key") or "")
            if not isinstance(command, dict) or not visual_key:
                command = {
                    "command": {
                        "kind": "tune",
                        "source": {"source": {"kind": "asset", "id": source_asset}},
                        "brief": brief,
                        "candidates": 1,
                        "formats": ["post_4_5"],
                        "selection": "human",
                        "copy": {},
                    }
                }
                visual_key = "ss-vp-visual-" + hashlib.sha256(
                    canonical([story_id, content_revision, source_asset, command]).encode()
                ).hexdigest()[:48]
                merged = self._merge_visual_context(
                    story_id,
                    content_revision,
                    {"visual_command": command, "visual_request_key": visual_key},
                )
                if merged is None:
                    return
                context = merged

            operation_id = str(context.get("operation_id") or "")
            if operation_id:
                receipt = await self.providers.vibepublish.status(operation_id)
            else:
                receipt = await self.providers.vibepublish.visual(command, visual_key)
                operation_id = str(receipt.get("operation_id") or "")
                if not operation_id:
                    raise RetryableProviderError(
                        "VibePublish visual accepted no recoverable operation_id"
                    )
                merged = self._merge_visual_context(
                    story_id,
                    content_revision,
                    {
                        "operation_id": operation_id,
                        "visual_job_id": receipt.get("visual_job_id"),
                    },
                )
                if merged is None:
                    return
                context = merged

            current = _receipt(await self.providers.vibepublish.status(operation_id), operation_id)
            state = str(current.get("state") or _receipt(receipt, operation_id).get("state") or "")
            if state in {"accepted", "running", "queued", "processing"}:
                raise RetryableProviderError(f"VibePublish visual operation is {state}")
            if state in {"blocked", "failed", "outcome_unknown"}:
                raise PermanentProviderError(f"VibePublish visual operation ended in {state}")

            if state == "verified":
                selected = current
                candidate_id = context.get("candidate_id")
                visual_job_id = current.get("visual_job_id") or context.get("visual_job_id")
            elif state == "needs_selection":
                candidates = current.get("candidates")
                if (
                    not isinstance(candidates, list)
                    or len(candidates) != 1
                    or not isinstance(candidates[0], dict)
                ):
                    raise PermanentProviderError(
                        "Street Story requires exactly one VibePublish visual candidate"
                    )
                candidate = candidates[0]
                visual_job_id = str(current.get("visual_job_id") or "")
                revision = current.get("visual_revision")
                token = candidate.get("selection_token")
                candidate_id = str(candidate.get("id") or "")
                if (
                    not visual_job_id
                    or not candidate_id
                    or not isinstance(revision, int)
                    or not isinstance(token, str)
                ):
                    raise PermanentProviderError(
                        "VibePublish visual selection receipt is incomplete"
                    )
                select_key = str(context.get("select_request_key") or "") or (
                    "ss-vp-select-"
                    + hashlib.sha256(
                        f"{operation_id}:{candidate_id}:{revision}".encode()
                    ).hexdigest()[:48]
                )
                merged = self._merge_visual_context(
                    story_id,
                    content_revision,
                    {
                        "visual_job_id": visual_job_id,
                        "candidate_id": candidate_id,
                        "select_request_key": select_key,
                    },
                )
                if merged is None:
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
                selected = await self.providers.vibepublish.visual(select_command, select_key)
                if str(selected.get("operation_id") or "") != operation_id:
                    raise PermanentProviderError(
                        "VibePublish selected a different visual operation"
                    )
                if selected.get("state") != "verified":
                    selected = _receipt(
                        await self.providers.vibepublish.status(operation_id), operation_id
                    )
            else:
                raise RetryableProviderError(
                    f"VibePublish visual operation is {state or 'pending'}"
                )

            if selected.get("state") != "verified":
                raise RetryableProviderError(
                    f"VibePublish selected visual is {selected.get('state') or 'pending'}"
                )
            selected_asset = str(selected.get("selected_asset_ref") or "")
            selected_sha = str(selected.get("selected_sha256") or "").lower()
            if not selected_asset or not re.fullmatch(r"[0-9a-f]{64}", selected_sha):
                raise PermanentProviderError(
                    "VibePublish verified visual lacks immutable asset identity"
                )
            merged = self._merge_visual_context(
                story_id,
                content_revision,
                {
                    "visual_job_id": visual_job_id,
                    "candidate_id": candidate_id,
                    "selected_asset_ref": selected_asset,
                    "selected_sha256": selected_sha,
                },
            )
            if merged is None:
                return

            processed, mime = await self.providers.vibepublish.read_asset(selected_asset)
            if hashlib.sha256(processed).hexdigest() != selected_sha:
                raise PermanentProviderError("VibePublish processed asset readback hash mismatch")
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise PermanentProviderError(
                    "VibePublish processed asset has an unsupported mime type"
                )
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
            }[mime]
            target = self.settings.data_dir / "stories" / story_id / f"processed{suffix}"
            _durable_write(target, processed)
            with self.store.tx() as db:
                current_row = self._story_row(db, story_id)
                latest = json.loads(current_row["visual_context_json"] or "{}")
                if latest.get("content_revision") != content_revision:
                    return
                db.execute(
                    "UPDATE stories SET state='ready_to_publish',processed_image_path=?,"
                    "processed_image_url=?,vibepublish_asset_ref=?,error_code=NULL,error_message=NULL,"
                    "revision=revision+1,updated_at=? WHERE id=?",
                    (
                        str(target),
                        f"/v1/assets/{story_id}/processed",
                        selected_asset,
                        self.store.now(),
                        story_id,
                    ),
                )
        except RetryableProviderError:
            raise
        except (PermanentProviderError, ConflictError, InvalidStateError) as exc:
            with self.store.tx() as db:
                row = self._story_row(db, story_id)
                latest = json.loads(row["visual_context_json"] or "{}")
                if latest.get("content_revision") != content_revision:
                    return
                db.execute(
                    "UPDATE stories SET state='needs_review',error_code='vibepublish_visual_error',"
                    "error_message=?,revision=revision+1,updated_at=? WHERE id=?",
                    (self.settings.redact(str(exc)), self.store.now(), story_id),
                )
