from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .product import ProductStreetStoryService, _receipt
from .providers import PermanentProviderError, RetryableProviderError
from .service import ConflictError, InvalidStateError, canonical, digest


class MvpProductStreetStoryService(ProductStreetStoryService):
    """Narrow MVP correctness layer over the proven product service.

    This class intentionally leaves capture, ASR, persistence and provider boundaries
    in the existing core. It only freezes the visual input snapshot and makes the
    owner-controlled prompt/reconciliation semantics explicit.
    """

    PROMPT_VERSION = "street-story-image-v2"
    PROMPT_TOKEN = "{{CITY_NOTE_THEMES}}"
    VIBEPUBLISH_BRIEF_LIMIT = 5000

    def _prompt_template(self) -> tuple[str, str]:
        path = Path(__file__).resolve().parents[1] / "prompts" / "street-story-image-v2.txt"
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
        optional_notes: list[str] = []
        place = str(story.get("place_name") or context.get("place_name") or "").strip()
        if place:
            optional_notes.append(f"Место: {place}.")
        visual_instruction = str(context.get("visual_instruction") or "").strip()
        if visual_instruction:
            notes.append("Текущая визуальная правка автора: " + visual_instruction[:600])
        facts = [
            str(item.get("text") or "").strip()
            for item in selected
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if facts:
            notes.append("Проверенные факты: " + "\n".join(f"• {fact}" for fact in facts))
            notes.append(
                "Обязательное содержание инфографики: каждый выбранный факт должен быть "
                "передан читаемой подписью. Сохрани смысл, даты и существенные уточнения "
                "каждого факта; одни даты без пояснения или общий эпитет не заменяют факт. "
                "Перед завершением проверь подписи по всему списку фактов."
            )
        concept = str(context.get("publication_concept") or "").strip()
        if concept:
            optional_notes.append("Концепция публикации: " + concept[:1200])
        intent = str(context.get("user_voice_intent") or "").strip()
        if intent:
            optional_notes.append("Авторское наблюдение: " + intent[:600])
        if not notes and not optional_notes:
            notes.append(
                "Передай атмосферу и узнаваемую городскую среду без выдуманных исторических утверждений."
            )

        themes = "\n".join(notes)
        language_rule = ""
        if re.search(r"[А-Яа-яЁё]", themes + "\n".join(optional_notes)):
            language_rule = (
                "\n\nLanguage rule: all visible handwritten city notes and annotations in the "
                "generated image must be in Russian. Keep them concise and legible."
            )

        base = template.replace(self.PROMPT_TOKEN, "")
        dynamic_budget = self.VIBEPUBLISH_BRIEF_LIMIT - len(base) - len(language_rule)
        if dynamic_budget <= 0:
            raise InvalidStateError(
                "visual_prompt_too_long",
                "Street Story owner image prompt exceeds the VibePublish brief contract",
            )
        if len(themes) > dynamic_budget:
            raise InvalidStateError(
                "visual_fact_annotations_too_long",
                "Selected facts and the visual instruction exceed the image brief capacity; "
                "revise the visual instruction or fact selection before generation.",
            )
        # Editorial context may be shortened. Selected evidence and the current
        # author correction must reach the model intact, including late qualifiers.
        for note in optional_notes:
            remaining = dynamic_budget - len(themes) - (1 if themes else 0)
            if remaining <= 0:
                break
            themes += ("\n" if themes else "") + note[:remaining]

        brief = template.replace(self.PROMPT_TOKEN, themes) + language_rule
        if len(brief) > self.VIBEPUBLISH_BRIEF_LIMIT:
            raise InvalidStateError(
                "visual_prompt_too_long",
                "Street Story visual prompt exceeds the VibePublish brief contract",
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

    async def request_visual(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get('observe_existing_visual') is True:
            return await self._observe_existing_visual(story_id, key, body)
        # Observe the original operation before replacing its frozen context.
        # A new key expresses a new author request, never an unknown-outcome retry.
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            if db.execute("SELECT 1 FROM idempotency WHERE key=?", (key,)).fetchone():
                return self.mutate_visual(story_id, key, body)
            revision = row['revision']
            previous = json.loads(row['visual_context_json'] or '{}')
        operation = previous.get('operation_id')
        observed = None
        if operation:
            observed = _receipt(await self.providers.vibepublish.status(operation), operation)
            if observed.get('operation_id') == operation and observed.get('state') == 'needs_selection':
                raise ConflictError('visual_observation_required',
                    'Import and review the existing result before requesting another generation.')
            if observed.get('operation_id') != operation or not (
                observed.get('state') in {'verified', 'needs_selection'}
                or (observed.get('state') in {'failed', 'cancelled'} and observed.get('retry_safe') is True)
            ):
                raise ConflictError('visual_outcome_unresolved',
                                    'Observe the original generation outcome before requesting another visual.')
        return self.mutate_visual(story_id, key, body, expected_revision=revision, previous_receipt=observed)

    async def _observe_existing_visual(self, story_id: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        """Resume the normal importer for the saved operation; never generate."""
        with self.store.connection() as db:
            before = self._story_row(db, story_id)
            previous = json.loads(before['visual_context_json'] or '{}')
            operation = previous.get('operation_id')
            revision = before['revision']
        if not operation:
            raise ConflictError('visual_observation_missing', 'There is no existing visual operation to observe.')
        observed = _receipt(await self.providers.vibepublish.status(operation), operation)
        if (observed.get('operation_id') != operation
                or observed.get('state') not in {'verified', 'needs_selection'}):
            raise ConflictError('visual_outcome_unresolved', 'The original operation is not ready for result import.')
        from .fact_ledger import eligibility_issues_for_ids, fact_revision_bundle
        req_digest = digest({'story_id': story_id, **body})
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            if self._idem(db, key, 'visual_observe', req_digest, 'story', story_id):
                return self._story_repr(db, row)
            context = json.loads(row['visual_context_json'] or '{}')
            ids = [str(value) for value in body.get('selected_fact_ids', [])]
            research = json.loads(row['research_json'] or '{}')
            if (row['revision'] != revision or context != previous
                    or context.get('source_photo_sha256') != row['photo_sha256']
                    or set(ids) != {item['fact_id'] for item in context.get('selected_facts', [])}
                    or eligibility_issues_for_ids(db, story_id, ids)
                    or context.get('fact_revision_bundle') != fact_revision_bundle(db, story_id, ids)
                    or context.get('publication_concept') != str(research.get('publication_concept') or '')[:1200]
                    or (body.get('visual_instruction') and body['visual_instruction'] != context.get('visual_instruction'))):
                raise ConflictError('visual_observation_input_changed', 'The saved visual no longer matches the current story.')
            if row['state'] == 'ready_to_publish' and row['processed_image_url']:
                return self._story_repr(db, row)
            self._enqueue_job(db, story_id, 'visual', f'visual-observe:{key}',
                {'content_revision': context['content_revision'], 'selected_fact_ids': ids})
            db.execute("UPDATE stories SET state='visual_processing',error_code=NULL,error_message=NULL,"
                'revision=revision+1,updated_at=? WHERE id=?', (self.store.now(), story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    def mutate_visual(self, story_id: str, key: str, body: dict[str, Any], *,
                      expected_revision: int | None = None, previous_receipt: dict | None = None) -> dict[str, Any]:
        req_digest = digest({"story_id": story_id, **body})
        _, template_sha = self._prompt_template()
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if self._idem(db, key, "visual", req_digest, "story", story_id):
                return self._story_repr(db, story)
            if expected_revision is not None and story['revision'] != expected_revision:
                raise ConflictError('visual_input_changed', 'Story changed while observing the original generation.')
            previous = json.loads(story['visual_context_json'] or '{}')
            if previous and not previous.get('operation_id') and (
                story['state'] == 'visual_processing' or previous.get('visual_request_key')
            ):
                raise ConflictError('visual_outcome_unresolved', 'The previous visual request is still being dispatched.')
            if previous.get('operation_id') and (previous_receipt or {}).get('operation_id') != previous['operation_id']:
                raise ConflictError('visual_outcome_unresolved', 'The original generation must be observed first.')

            selected_ids = [str(value) for value in body.get("selected_fact_ids", [])]
            from .fact_ledger import eligibility_issues_for_ids, fact_revision_bundle
            issues = eligibility_issues_for_ids(db, story_id, selected_ids)
            if issues:
                raise InvalidStateError(
                    "fact_review_required",
                    "Requested facts still need semantic review before visual generation.",
                )
            facts = {
                row["fact_id"]: row
                for row in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))
            }
            revisions = fact_revision_bundle(db, story_id, selected_ids)
            if len(revisions) != len(set(selected_ids)):
                raise ConflictError(
                    "visual_fact_revision_missing",
                    "One or more selected facts have no durable revision.",
                )
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
                        "revision_digest": revisions[fact_id],
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
                    {
                        "fact_id": item["fact_id"],
                        "text": item["text"],
                        "revision_digest": item["revision_digest"],
                    }
                    for item in selected_facts
                ],
                "fact_revision_bundle": revisions,
                "publication_concept": str(research.get("publication_concept") or "")[:1200],
                "visual_instruction": str(body.get("visual_instruction") or "").strip()[:600],
                "prompt_version": self.PROMPT_VERSION,
                "prompt_sha256": template_sha,
            }
            if body.get('reuse_generated_art') is True:
                observed = previous_receipt or {}
                same_inputs = ('source_photo_sha256', 'fact_revision_bundle', 'publication_concept',
                               'prompt_version', 'prompt_sha256', 'visual_instruction')
                if (observed.get('state') != 'verified'
                        or observed.get('visual_job_id') != previous.get('visual_job_id')
                        or observed.get('selected_asset_ref') != previous.get('selected_asset_ref')
                        or not previous.get('candidate_id')
                        or type(observed.get('visual_revision')) is not int
                        or any(previous.get(field) != frozen.get(field) for field in same_inputs)):
                    raise ConflictError('visual_recompose_input_changed',
                                        'Recomposition requires the same verified art and unchanged visual inputs.')
                frozen['recompose_source'] = {
                    'job_id': previous['visual_job_id'], 'candidate_id': previous['candidate_id'],
                    'expected_visual_revision': observed['visual_revision'],
                    'expected_sha256': previous['selected_sha256'], 'format': 'post_4_5',
                }
            if previous.get('operation_id'):
                frozen['generation_attempt'] = digest([story_id, key, previous['operation_id']])
            content_revision = digest(frozen)
            context = {
                **frozen,
                "content_revision": content_revision,
                "selected_facts": selected_facts,
                "place_name": story["place_name"],
                "user_voice_intent": research.get("transcript", ""),
            }
            if previous.get('operation_id'):
                history = list(previous.get('attempt_history') or [])
                history.append({k: previous.get(k) for k in ('operation_id', 'visual_job_id', 'content_revision',
                                                            'visual_request_key', 'candidate_id', 'asset_ref')})
                history[-1]['outcome'] = previous_receipt
                context['attempt_history'] = history
            context["brief"] = self._visual_brief(dict(story), context)
            if frozen.get('recompose_source'):
                context['source_asset_ref'] = previous.get('source_asset_ref')
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
                if context.get('recompose_source'):
                    command = {'command': {'kind': 'recompose', **context['recompose_source']}}
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
            if not selected_asset:
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
                        None,
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
