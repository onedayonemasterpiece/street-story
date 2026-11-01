"""Selected story photos and comic cards on the existing SQLite/jobs/VibePublish paths."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid
from pathlib import Path

from .publication_slots import DEFAULT_DESTINATIONS
from .service import ConflictError, InvalidStateError, _durable_write, canonical, digest


def selected_photos(db, story_id):
    return [dict(row) for row in db.execute(
        "SELECT * FROM story_photos WHERE story_id=? AND position IS NOT NULL ORDER BY position",
        (story_id,))]


class StoryPhotosMixin:
    """Extend the runtime projection, not the identity or fact source of truth."""

    def add_story_photo(self, story_id, key, photo_id, data, mime):
        try:
            photo_id = str(uuid.UUID(str(photo_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConflictError("photo_id_invalid", "X-Photo-Id должен быть UUID.") from exc
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not data:
            raise ConflictError("photo_format_invalid", "Нужен JPEG, PNG или WebP.")
        from .visual_source import oriented_visual_source
        oriented_visual_source(data, mime)
        sha = hashlib.sha256(data).hexdigest()
        binding = digest({"story_id": story_id, "photo_id": photo_id, "sha256": sha, "mime": mime})
        path = self.settings.data_dir / "stories" / story_id / "photos" / f"{photo_id}.original"
        with self.store.tx() as db:
            self._story_row(db, story_id)
            self._idem(db, key, "story_photo", binding, "story_photo", f"{story_id}:{photo_id}")
            existing = db.execute("SELECT sha256,mime_type FROM story_photos WHERE story_id=? AND photo_id=?",
                                  (story_id, photo_id)).fetchone()
            if existing:
                if existing["sha256"] != sha or existing["mime_type"] != mime:
                    raise ConflictError("photo_id_conflict", "Фото уже загружено с другим содержимым.")
                return {"photo_id": photo_id}
            # Store in the existing private story directory, not a public static asset tree.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            _durable_write(path, data)
            os.chmod(path, 0o600)
            db.execute("INSERT INTO story_photos(story_id,photo_id,position,path,mime_type,sha256) "
                       "VALUES(?,?,NULL,?,?,?)", (story_id, photo_id, str(path), mime, sha))
            logging.getLogger(__name__).info("street_story_photo_saved story_id=%s photo_id=%s bytes=%s",
                                             story_id, photo_id, len(data))
        return {"photo_id": photo_id}

    def order_story_photos(self, story_id, key, body):
        ids = body.get("photo_ids") if isinstance(body, dict) else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(not isinstance(item, str) for item in ids)
                or len(set(ids)) != len(ids)):
            raise ConflictError("photo_order_invalid", "Выберите от одного до восьми разных загруженных фото.")
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            previous = [item["photo_id"] for item in selected_photos(db, story_id)]
            replay = self._idem(db, key, "story_photo_order",
                                digest({"story_id": story_id, "photo_ids": ids}), "story", story_id)
            if replay or previous == ids:
                return self._story_repr(db, row)
            available = {r["photo_id"] for r in db.execute(
                "SELECT photo_id FROM story_photos WHERE story_id=?", (story_id,))}
            if not set(ids).issubset(available):
                raise ConflictError("photo_order_unknown", "Один из выбранных снимков ещё не загружен.")
            # NULL all positions first to honour the existing UNIQUE constraint on reorders.
            db.execute("UPDATE story_photos SET position=NULL WHERE story_id=?", (story_id,))
            for position, photo_id in enumerate(ids):
                db.execute("UPDATE story_photos SET position=? WHERE story_id=? AND photo_id=?",
                           (position, story_id, photo_id))
            context = json.loads(row["visual_context_json"] or "{}")
            if context:
                context["stale"] = True
                context["stale_reason"] = "photos_changed"
            new_state = ("needs_review" if row["state"] in {"ready_to_publish", "visual_processing"}
                         else row["state"])
            db.execute("UPDATE stories SET visual_context_json=?,processed_image_url=NULL,"
                       "vibepublish_asset_ref=NULL,state=?,revision=revision+1,updated_at=? WHERE id=?",
                       (canonical(context), new_state, self.store.now(), story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    def _comic_photo_bytes(self, story_id, photo_id):
        if photo_id == "source":
            with self.store.connection() as db:
                row = self._story_row(db, story_id)
            return self._source_photo_for_job(story_id), row["photo_mime_type"]
        with self.store.connection() as db:
            entry = db.execute("SELECT path,mime_type FROM story_photos WHERE story_id=? AND photo_id=?",
                               (story_id, photo_id)).fetchone()
        if not entry:
            raise ConflictError("photo_not_found", "Фото не найдено в истории.")
        expected = self.settings.data_dir / "stories" / story_id / "photos" / f"{photo_id}.original"
        path = Path(entry["path"])
        if path != expected or path.is_symlink() or expected.parent.resolve() != expected.parent.absolute():
            raise ConflictError("source_unavailable", "Оригинал снимка недоступен.")
        try:
            return path.read_bytes(), entry["mime_type"]
        except OSError as exc:
            from .providers import RetryableProviderError
            raise RetryableProviderError("source_unavailable", retry_at=self.store.now() + 300) from exc

    def _cover_photo_bytes(self, story_id):
        with self.store.connection() as db:
            photos = selected_photos(db, story_id)
        return self._comic_photo_bytes(story_id, photos[0]['photo_id'])[0] if photos else self._source_photo_bytes(story_id)

    def photo_thumbnail(self, story_id, photo_id):
        from .reference_image_codec import topic_thumbnail
        with self.store.connection() as db:
            self._story_row(db, story_id)
            if not db.execute("SELECT 1 FROM story_photos WHERE story_id=? AND photo_id=?",
                              (story_id, photo_id)).fetchone():
                raise ConflictError("photo_not_found", "Фото не найдено.")
        data, _ = self._comic_photo_bytes(story_id, photo_id)
        try:
            return topic_thumbnail(data)
        except ValueError as exc:
            raise ConflictError("thumbnail_unavailable", "Не удалось показать миниатюру.") from exc

    async def comic_card(self, story_id, position):
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            context = json.loads(row["visual_context_json"] or "{}")
            comic = context.get("comic") or {}
            cards = comic.get("cards") or []
            if (context.get("stale") or not 0 <= position < len(cards)
                    or len(cards) != len(selected_photos(db, story_id))
                    or row["state"] not in {"ready_to_publish", "scheduling", "scheduled", "published"}):
                raise ConflictError("visual_not_ready", "Карточка истории ещё не готова.")
            asset = cards[position].get("asset_ref")
        if not asset:
            raise ConflictError("visual_not_ready", "Карточка истории ещё не готова.")
        return await self.providers.vibepublish.read_asset(asset)

    def _story_repr(self, db, row, *, research=None):
        result = super()._story_repr(db, row, research=research)
        photos = selected_photos(db, row["id"])
        context = json.loads(row["visual_context_json"] or "{}")
        comic = context.get("comic") or {}
        cards = comic.get("cards") or []
        research_photo_format = (research or json.loads(row['research_json'] or '{}')).get('visual_photo_format') or {}
        current_comic_format = not comic or context.get('photo_format') == research_photo_format
        result["photos"] = [
            {"photo_id": photo["photo_id"], "position": index,
             "thumbnail_url": f"/v1/stories/{row['id']}/photos/{photo['photo_id']}/thumbnail"}
            for index, photo in enumerate(photos)]
        # The caption writer owns card lettering; it is visible before visual generation.
        letterings = (research or json.loads(row["research_json"] or "{}")).get("caption_image_notes") or {}
        current_ids = [photo["photo_id"] for photo in photos]
        valid_letters = (isinstance(letterings, dict)
                         and letterings.get("photo_ids") == current_ids
                         and isinstance(letterings.get("cards"), list)
                         and len(letterings["cards"]) == len(photos))
        result["card_notes"] = [
            {"position": i, "notes": list(card["notes"]), "arrows": list(card["arrows"])}
            for i, card in enumerate(letterings["cards"])
        ] if valid_letters else []
        if (not context.get("stale") and current_comic_format and len(cards) == len(photos)
                and all(card.get("asset_ref") for card in cards)
                and row["state"] in {"ready_to_publish", "scheduling", "scheduled", "published"}):
            result["cards"] = [{"position": i, "url": f"/v1/stories/{row['id']}/cards/{i}"}
                               for i in range(len(cards))]
        elif (not context.get("stale") and len(photos) == 1 and row["vibepublish_asset_ref"]
              and row["processed_image_url"]):
            result["cards"] = [{"position": 0, "url": row["processed_image_url"]}]
        else:
            result["cards"] = []
            if context.get("stale") or not current_comic_format:
                result["processed_image_url"] = None
        # For a multi-network slot the owner sees both destinations, not just the
        # latest VK intent. Occupancy and native delivery remain in publish_intents.
        last = db.execute("SELECT request_json FROM publish_intents WHERE story_id=? "
                          "ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],)).fetchone()
        if last and isinstance(result.get("publication"), dict):
            group = json.loads(last["request_json"] or "{}").get("publication_group")
            if group:
                grouped = []
                for intent in db.execute("SELECT request_json FROM publish_intents WHERE story_id=?",
                                         (row["id"],)):
                    request = json.loads(intent["request_json"] or "{}")
                    if request.get("publication_group") == group:
                        grouped.append(request)
                result["publication"]["destinations"] = list(dict.fromkeys(
                    target for request in grouped for target in request.get("destinations", [])))
                result["publication"]["slot_at"] = group and grouped[0].get("slot_at")
        return result

    def mutate_publish(self, story_id, key, body):
        if not isinstance(body, dict):
            raise ConflictError("publish_input_invalid", "Неверная команда публикации.")
        if not body:
            body = {"destinations": list(DEFAULT_DESTINATIONS), "slot_intent": {}}
        elif not body.get("destinations") and body.get("slot_intent") is not None:
            body = {**body, "destinations": list(DEFAULT_DESTINATIONS)}
        with self.store.connection() as db:
            row = self._story_row(db, story_id)
            visual = json.loads(row["visual_context_json"] or "{}")
            photos = selected_photos(db, story_id)
            current_format = (json.loads(row['research_json'] or '{}').get('visual_photo_format') or {})
            if (visual.get("stale") or visual.get('comic') and visual.get('photo_format') != current_format
                    or not row["vibepublish_asset_ref"]):
                raise InvalidStateError("visual_not_ready", "Сначала создайте актуальные карточки.")
            cards = (visual.get("comic") or {}).get("cards") or []
            if cards and len(cards) != len(photos):
                raise InvalidStateError("visual_not_ready", "Набор карточек изменился.")
        return super().mutate_publish(story_id, key, body)

    def _comic_current(self, db, story_id, revision, photo_ids):
        row = self._story_row(db, story_id)
        context = json.loads(row["visual_context_json"] or "{}")
        return (context.get("content_revision") == revision and not context.get("stale")
                and context.get('photo_format') == (json.loads(row['research_json'] or '{}').get('visual_photo_format') or {})
                and [p["photo_id"] for p in selected_photos(db, story_id)] == photo_ids)

    def _comic_patch(self, story_id, revision, photo_ids, index=None, **updates):
        """Update one card's receipt within the original visual job's SQLite ledger."""
        with self.store.tx() as db:
            if not self._comic_current(db, story_id, revision, photo_ids):
                return None
            row = self._story_row(db, story_id)
            context = json.loads(row["visual_context_json"] or "{}")
            comic = context["comic"]
            if index is None:
                comic.update(updates)
            else:
                comic["cards"][index].update(updates)
            db.execute("UPDATE stories SET visual_context_json=?,updated_at=? WHERE id=?",
                       (canonical(context), self.store.now(), story_id))
            return context

    async def request_visual(self, story_id, key, body):
        with self.store.connection() as db:
            photos = selected_photos(db, story_id)
            row = self._story_row(db, story_id)
            letterings = (json.loads(row["research_json"] or "{}").get("caption_image_notes") or {})
        if len(photos) == 1 and photos[0]['photo_id'] == 'source':
            return await super().request_visual(story_id, key, body)
        if (not isinstance(body, dict) or body.get("preserve_source_frame")
                or body.get("reuse_generated_art") or body.get("observe_existing_visual")):
            raise ConflictError("visual_multi_input_invalid", "Для альбома используйте обычную генерацию карточек.")
        from .fact_ledger import eligibility_issues_for_ids, fact_revision_bundle
        fact_ids = body.get("selected_fact_ids", [])
        if (not isinstance(fact_ids, list) or len(set(fact_ids)) != len(fact_ids)
                or any(not isinstance(fid, str) for fid in fact_ids)):
            raise ConflictError("visual_fact_selection_invalid", "Укажите проверенные факты.")
        photo_ids = [p["photo_id"] for p in photos]
        if (not isinstance(letterings, dict) or letterings.get("photo_ids") != photo_ids
                or not isinstance(letterings.get("cards"), list)
                or len(letterings["cards"]) != len(photo_ids)
                or set(letterings.get("fact_ids") or []) != set(fact_ids)):
            return self.mutate_refinement(story_id, "comic-notes:" + key, {"instruction":
                "Сохрани содержание существующего поста, подготовь недостающие надписи card_notes "
                "для всех выбранных фотографий в их текущем порядке по проверенным выбранным фактам."})
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            if self._idem(db, key, "visual", digest({"story_id": story_id, **body}), "story", story_id):
                return self._story_repr(db, row)
            if not fact_ids:
                raise InvalidStateError('fact_review_required', 'Для скетч-комикса выберите подтверждённый факт.')
            if eligibility_issues_for_ids(db, story_id, fact_ids):
                raise InvalidStateError("fact_review_required", "Проверьте выбранные факты перед генерацией.")
            revisions = fact_revision_bundle(db, story_id, fact_ids)
            facts = []
            for fid in fact_ids:
                source = db.execute("SELECT text,evidence_supported FROM facts WHERE story_id=? AND fact_id=?",
                                    (story_id, fid)).fetchone()
                if not source or not source["evidence_supported"] or fid not in revisions:
                    raise ConflictError("visual_fact_unsupported", "Нужны подтверждённые факты.")
                facts.append({"fact_id": fid, "text": source["text"], "revision_digest": revisions[fid]})
            research = json.loads(row["research_json"] or "{}")
            photo_ids = [item["photo_id"] for item in photos]
            inputs = {
                "photo_ids": photo_ids, "photo_digests": [p["sha256"] for p in photos],
                "selected_facts": facts, "fact_revision_bundle": revisions,
                "source_photo_sha256": row["photo_sha256"],
                "identity_generation": int(research.get("identity_generation") or 0),
                "publication_concept": str(research.get("publication_concept") or "")[:1200],
                "visual_instruction": str(body.get("visual_instruction") or "")[:600],
                "photo_format": research.get("visual_photo_format") or {},
                "place_name": str(row["place_name"] or ""),
                "place_selection": research.get("place_selection"),
                "strongest_fact": (research.get("strongest_fact") or {}).get("fact_id"),
            }
            content_revision = digest(inputs)
            comic = {"photo_ids": photo_ids,
                     "cards": [{"photo_id": photo_id} for photo_id in photo_ids],
                     "plan": {"cards": letterings["cards"]}}
            context = {**inputs, "content_revision": content_revision, "comic": comic,
                       "prompt_version": self.PROMPT_VERSION, "stale": False}
            self._enqueue_job(db, story_id, "visual", f"visual:{key}",
                              {"content_revision": content_revision, "selected_fact_ids": fact_ids})
            db.execute("UPDATE stories SET state='visual_processing',visual_context_json=?,"
                       "vibepublish_asset_ref=NULL,processed_image_url=NULL,"
                       "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                       (canonical(context), self.store.now(), story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    def _comic_brief(self, story, context, position, photo, plan):
        from .visual_source import photo_transform
        from .photo_format import choice
        research = json.loads(story["research_json"] or "{}")
        selected_format = choice(research)
        # Legacy crop belongs to source; a new cover may choose its own format.
        crop_allowed = (position == 0 and
                        selected_format.get("photo_id", "source") == photo["photo_id"])
        mode = selected_format["mode"] if crop_allowed else "original"
        crop = selected_format.get("crop_rect") if crop_allowed else None
        data, mime = self._comic_photo_bytes(story["id"], photo["photo_id"])
        data, mime, transform = photo_transform(data, mime, mode, crop)
        notes, arrows = plan["notes"], plan["arrows"]
        if position == len(context["comic"]["photo_ids"]) - 1:
            selection = context.get("place_selection") or {}
            selection = selection if isinstance(selection, dict) else {}
            anchor = ((selection.get("candidate") or {}).get("search_anchor") or {})
            city = str(anchor.get("city") or selection.get("city") or "").strip()
            address = str(selection.get("address") or selection.get("display_address") or "").strip()
            if not address:
                address = ", ".join(str(anchor.get(k) or "").strip() for k in
                                    ("city", "street", "house_number") if str(anchor.get(k) or "").strip())
            if not city and address:
                pieces = [part.strip() for part in address.split(",") if part.strip()]
                city = pieces[1] if len(pieces) > 1 and "област" in pieces[0].casefold() else pieces[0]
                city = city.removeprefix("г. ").removeprefix("город ")
            place = context.get("place_name") or "Выбранный объект"
            city_label, address_label = city or place, address or place
            city_in_address = address_label.casefold().startswith((city_label.casefold() + ",",
                                                                    city_label.casefold() + " "))
            notes = list(dict.fromkeys(["История за поворотом Калининград",
                                        *([] if city_in_address else [city_label]), address_label]))
            arrows = [""] * len(notes)
        brief_context = {
            "selected_facts": context["selected_facts"] if position != len(context["comic"]["photo_ids"]) - 1 else [],
            "caption_image_notes": notes, "caption_image_note_arrows": arrows,
            "publication_concept": context.get("publication_concept"),
            "visual_instruction": context.get("visual_instruction"),
            "place_name": context.get("place_name"),
            "source_image_transform": transform,
        }
        suffix = ("\n\nЭто ОБЛОЖКА комикса: название, зачин и «листай →» обязательны."
                  if position == 0 else
                  "\n\nЭто ФИНАЛ комикса: точно передай фирменную строку и доступное местоположение. "
                  "Если город или адрес не указаны, не выдумывай их; используй название места."
                  if position == len(context["comic"]["photo_ids"]) - 1 else
                  "\n\nЭто ПРОМЕЖУТОЧНЫЙ кадр комикса. Не повторяй другие карточки.")
        brief = self._visual_brief(dict(story), brief_context, reserve_chars=len(suffix)) + suffix
        return brief, data, mime, transform

    async def _run_visual(self, job):
        story_id = job["story_id"]
        with self.store.connection() as db:
            story = dict(self._story_row(db, story_id))
            context = json.loads(story["visual_context_json"] or "{}")
        if not context.get("comic"):
            return await super()._run_visual(job)
        from .product import _receipt, ORIGINAL_ART_FRAME_CONTRACT, POST_4_5_FRAME_CONTRACT
        from .providers import PermanentProviderError, RetryableProviderError
        revision = json.loads(job["payload_json"] or "{}").get("content_revision")
        photo_ids = context["comic"]["photo_ids"]
        with self.store.connection() as db:
            if not self._comic_current(db, story_id, revision, photo_ids):
                return
        plans = context["comic"]["plan"]["cards"]
        await self.providers.vibepublish.bootstrap()
        if not all(card.get("asset_ref") for card in context["comic"]["cards"]):
            # Original image preparation/ingress remains per photo; only the
            # durable imagegen request is batched. Nothing is generated twice.
            for position, photo_id in enumerate(photo_ids):
                with self.store.connection() as db:
                    if not self._comic_current(db, story_id, revision, photo_ids):
                        return
                    story = dict(self._story_row(db, story_id))
                    context = json.loads(story["visual_context_json"] or "{}")
                card = context["comic"]["cards"][position]
                if (card.get("brief") and card.get("transform")
                        and card.get("source_asset_ref") and card.get("source_sha256")):
                    continue
                if not card.get("brief"):
                    brief, data, mime, transform = self._comic_brief(
                        story, context, position, {"photo_id": photo_id}, plans[position])
                    context = self._comic_patch(story_id, revision, photo_ids, position,
                                                brief=brief, transform=transform)
                    if context is None:
                        return
                    card = context["comic"]["cards"][position]
                else:
                    data, mime = self._comic_photo_bytes(story_id, photo_id)
                    from .visual_source import photo_transform
                    transform = card["transform"]
                    data, mime, current = photo_transform(data, mime, transform["mode"],
                        transform.get("crop_rect") if transform["mode"] == "crop_4_5" else None,
                        contract=transform["contract"])
                    if current != transform:
                        raise PermanentProviderError("visual_source_transform_changed")
                if not card.get("source_asset_ref") or not card.get("source_sha256"):
                    ingress_key = "ss-vp-asset-" + digest([story_id, photo_id, card["transform"]])[:48]
                    ingress = await self.providers.vibepublish.ingress_asset(data, mime, ingress_key)
                    source_asset = str(ingress.get("asset_id") or "")
                    source_sha = str(ingress.get("sha256") or "")
                    if not source_asset or len(source_sha) != 64 or any(c not in '0123456789abcdef' for c in source_sha):
                        raise RetryableProviderError("VibePublish ingress lacks asset ID or verified SHA")
                    context = self._comic_patch(story_id, revision, photo_ids, position,
                        source_asset_ref=source_asset, source_sha256=source_sha)
                    if context is None:
                        return
            with self.store.connection() as db:
                if not self._comic_current(db, story_id, revision, photo_ids):
                    return
                context = json.loads(self._story_row(db, story_id)["visual_context_json"] or "{}")
            cards = context["comic"]["cards"]
            if any(card.get("operation_id") for card in cards) and not context["comic"].get("batch_operation_id"):
                # In-flight legacy individual jobs must not be regenerated.
                raise PermanentProviderError("visual_legacy_card_operation_requires_review")
            operation = context["comic"].get("batch_operation_id")
            formats = ["original" if card["transform"]["mode"] == "original" else "post_4_5"
                       for card in cards]
            ordered = [[photo_id, card["source_asset_ref"], card["source_sha256"],
                        card["brief"], card["transform"]]
                       for photo_id, card in zip(photo_ids, cards)]
            if not operation:
                # All comic briefs share the owner-approved versioned prompt
                # template. Include it once, retaining every per-card factual
                # instruction, caption, frame rule and source-specific constraint.
                template, _ = self._prompt_template()
                prefix, suffix = template.split(self.PROMPT_TOKEN)
                card_sections = []
                for i, card in enumerate(cards):
                    brief = card["brief"]
                    if not brief.startswith(prefix) or (suffix and suffix not in brief[len(prefix):]):
                        raise PermanentProviderError("visual_prompt_template_mismatch")
                    dynamic = brief[len(prefix):]
                    if suffix:
                        dynamic = dynamic.replace(suffix, "", 1)
                    card_sections.append(
                        f"[CARD {i+1}/{len(cards)}] source-{i}, format={formats[i]}\n{dynamic}")
                batch_brief = (
                    "Единый стиль скетч-комикса для всех кадров. Каждый раздел относится "
                    "только к одноимённому исходному фото. Генерируй отдельную "
                    "картинку на каждое source-N в заданном порядке.\n\n"
                    + prefix + suffix + "\n\n" + "\n\n".join(card_sections))
                if len(batch_brief) > 32000:
                    raise PermanentProviderError("visual_prompt_too_long")
                visual_key = "ss-vp-visual-" + digest([story_id, revision, ordered])[:48]
                command = {"command": {"kind": "tune",
                    "sources": [{"source": {"kind": "asset", "id": card["source_asset_ref"]}}
                                for card in cards],
                    "brief": batch_brief, "candidates": len(cards), "formats": formats,
                    "selection": "human", "copy": {}}}
                response = await self.providers.vibepublish.visual(command, visual_key)
                operation = str(response.get("operation_id") or "")
                if not operation:
                    raise RetryableProviderError("VibePublish batch accepted no recoverable operation_id")
                context = self._comic_patch(story_id, revision, photo_ids,
                                            batch_operation_id=operation)
                if context is None:
                    return
            receipt = _receipt(await self.providers.vibepublish.status(operation), operation)
            state = receipt.get("state")
            if state in {"accepted", "running", "queued", "processing"}:
                raise RetryableProviderError("VibePublish comic batch is processing")
            if state in {"blocked", "failed", "outcome_unknown", "cancelled"}:
                raise PermanentProviderError("VibePublish comic batch ended in " + str(state))
            if state not in {"needs_selection", "verified"}:
                raise RetryableProviderError("VibePublish comic batch receipt pending")
            choices = receipt.get("candidates")
            if not isinstance(choices, list) or len(choices) != len(cards):
                raise PermanentProviderError("Comic batch has incomplete candidate receipt")
            seen_assets, seen_hashes, seen_ids = set(), set(), set()
            for position, candidate in enumerate(choices):
                card = cards[position]
                sha = candidate.get("sha256") if isinstance(candidate, dict) else None
                asset_ref = candidate.get("asset_ref") if isinstance(candidate, dict) else None
                candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
                if (not isinstance(candidate, dict) or type(candidate.get("source_index")) is not int
                        or candidate["source_index"] != position
                        or candidate.get("source_asset_ref") != card["source_asset_ref"]
                        or candidate.get("source_sha256") != card["source_sha256"]
                        or candidate.get("format") != formats[position]
                        or not isinstance(asset_ref, str) or not asset_ref
                        or not isinstance(candidate_id, str) or not candidate_id
                        or not isinstance(sha, str) or len(sha) != 64
                        or any(c not in "0123456789abcdef" for c in sha)
                        or asset_ref in seen_assets or sha in seen_hashes or candidate_id in seen_ids
                        or type(candidate.get("width")) is not int
                        or type(candidate.get("height")) is not int
                        or not self._frame_dimensions_match({
                            "output_frame_contract": (POST_4_5_FRAME_CONTRACT if card["transform"]["mode"] == "crop_4_5"
                                                      else ORIGINAL_ART_FRAME_CONTRACT),
                            "source_image_transform": card["transform"]
                        }, candidate["width"], candidate["height"])):
                    raise PermanentProviderError("visual_batch_source_or_format_unverified")
                if card.get("asset_ref") and card["asset_ref"] != asset_ref:
                    raise PermanentProviderError("visual_batch_candidate_changed")
                seen_assets.add(asset_ref)
                seen_hashes.add(sha)
                seen_ids.add(candidate_id)
            # Candidate bytes were already verified by VibePublish. This does NOT
            # call legacy single-candidate select and pretend it selected N;
            # the complete comic still requires Street Story's human review.
            for position, candidate in enumerate(choices):
                if not cards[position].get("asset_ref"):
                    context = self._comic_patch(story_id, revision, photo_ids, position,
                        asset_ref=candidate["asset_ref"], candidate=candidate,
                        selected_sha256=candidate["sha256"])
                    if context is None:
                        return
        with self.store.tx() as db:
            if not self._comic_current(db, story_id, revision, photo_ids):
                return
            row = self._story_row(db, story_id)
            context = json.loads(row["visual_context_json"] or "{}")
            refs = [card.get("asset_ref") for card in context["comic"]["cards"]]
            if not all(refs):
                raise RetryableProviderError("Comic card asset is missing")
            new_state = (row["state"] if row["state"] in {"scheduling", "scheduled", "published"}
                         else "ready_to_publish")
            db.execute("UPDATE stories SET state=?,processed_image_url=?,vibepublish_asset_ref=?,"
                       "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                       (new_state, f"/v1/stories/{story_id}/cards/0", refs[0],
                        self.store.now(), story_id))
            self._queue_publication_edits(db, story_id, text=str(row["draft_text"] or ""),
                asset_ref=refs[0], change_id="comic:" + revision)
