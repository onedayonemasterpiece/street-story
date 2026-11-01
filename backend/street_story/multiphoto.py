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
                     "cards": [{"photo_id": photo_id} for photo_id in photo_ids]}
            context = {**inputs, "content_revision": content_revision, "comic": comic,
                       "prompt_version": self.PROMPT_VERSION, "stale": False}
            self._enqueue_job(db, story_id, "visual", f"visual:{key}",
                              {"content_revision": content_revision, "selected_fact_ids": fact_ids})
            db.execute("UPDATE stories SET state='visual_processing',visual_context_json=?,"
                       "vibepublish_asset_ref=NULL,processed_image_url=NULL,"
                       "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                       (canonical(context), self.store.now(), story_id))
            return self._story_repr(db, self._story_row(db, story_id))

    async def _plan_comic(self, story_id, context):
        """One model response plans every card; only visual rendering repeats per photo."""
        from google.genai import types
        photo_ids = context["comic"]["photo_ids"]
        facts = [{"id": f["fact_id"], "text": f["text"]} for f in context["selected_facts"]]
        place = context.get("place_name") or "Выбранный объект"
        selection = context.get("place_selection") or {}
        selection = selection if isinstance(selection, dict) else {}
        anchor = ((selection.get('candidate') or {}).get('search_anchor') or {})
        city = str(anchor.get('city') or selection.get('city') or '').strip()
        address = str(selection.get('address') or selection.get('display_address') or '').strip()
        if not address:
            address = ', '.join(str(anchor.get(key) or '').strip() for key in ('city', 'street', 'house_number')
                                if str(anchor.get(key) or '').strip())
        if not city and address:
            parts = [part.strip() for part in address.split(',') if part.strip()]
            city = (parts[1] if len(parts) > 1 and 'област' in parts[0].casefold() else parts[0])
            city = city.removeprefix('г. ').removeprefix('город ')
        # Missing map address must not prevent an otherwise supported comic.
        # The cover's place label is the available fallback, not an invented city.
        city_label, address_label = city or place, address or place
        rules = (
            "Ты писатель скетч-комикса о выбранном объекте Калининградской области. "
            "Один ответ — полный план всех N карточек в порядке присланных N фотографий. "
            "Для каждого кадра верни короткие русские надписи (notes), цели стрелок (arrows), "
            "использованные ID фактов (fact_ids), а для первой карточки сильный конкретный зачин (hook). "
            "События, годы, люди и причинные связи только из переданных проверенных фактов. "
            "Каждая надпись пересказывает ТОЛЬКО факты из fact_ids именно этой карточки: "
            "никаких новых деталей, эпитетов, оценок, статусов, причин или выводов "
            "(например, «главной», «пережила всё», «не барьер»), которых нет в факте. "
            "Не используй иностранных слов, кроме имён собственных из самих фактов. "
            "Факты распределяй между карточками без повторения; самый сильный факт — сцена на обложке. "
            "Несколько заметок на одном фото допускаются, 2–3 кратких подписи по возможности. "
            "Каждая надпись — короткая заметка своими словами в 3–8 слов (не более 70 символов), "
            "а зачин обложки — до 12 слов. «листай →» — отдельная подпись ТОЛЬКО обложки; "
            "на остальных карточках её быть не должно. "
            "Не переписывай длинные предложения из фактов в надписи дословно. "
            "Прошлое — только в прошедшем времени с собственной датой или периодом. "
            "Утраченное, невидимое и то, что происходило внутри, нельзя дорисовывать; стрелки "
            "только на отчётливо видимую сейчас часть дома, остальные arrows — пустая строка. "
            "Заметки только для приложенных фактов, без дат источников, заглушек, оценок вне замысла. "
            "Первый кадр — обложка: назови объект, начни со сцены по сильнейшему факту "
            "(strongest_fact_id желательно поставить первым) и добавь «листай →». "
            "Не заменяй собственные надписи шаблонными. "
            "Последний — завершение: фирменную строку, город и адрес добавит сервер, "
            "поэтому у финала fact_ids оставь пустым списком. "
            "Верни ровно N записей; по 1–3 notes на кадр, arrows той же длины, fact_ids без дублей. "
            "Верни поле text с коротким служебным названием плана (не надпись на карточке)."
        )
        schema = {"type": "object", "properties": {"text": {"type": "string"}, "cards": {"type": "array", "items": {
            "type": "object", "properties": {
                "notes": {"type": "array", "items": {"type": "string"}},
                "arrows": {"type": "array", "items": {"type": "string"}},
                "fact_ids": {"type": "array", "items": {"type": "string"}},
                "hook": {"type": "string"}},
            "required": ["notes", "fact_ids"]}}},
            "required": ["text", "cards"]}
        payload = {"photo_count": len(photo_ids), "photo_ids": photo_ids, "place": place,
                   "address": address or None, "city": city or None, "strongest_fact_id": context.get("strongest_fact"),
                   "strongest_fact_text": next((f['text'] for f in facts if f['id'] == context.get('strongest_fact')), None),
                   "concept": context.get("publication_concept"), "facts": facts}
        contents = [rules + "\n\n" + json.dumps(payload, ensure_ascii=False)]
        for photo_id in photo_ids:
            data, mime = self._comic_photo_bytes(story_id, photo_id)
            from .visual_source import photo_transform
            data, mime, _ = photo_transform(data, mime, "original")
            contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        config = types.GenerateContentConfig(response_mime_type="application/json",
                                             response_json_schema=schema, max_output_tokens=4096)
        allowed_ids = {item["id"] for item in facts}

        def parse(raw, model):
            allowed_punct = " .,;:!?…-—–«»„“”\"'()[]№%/+&→_#\n"
            def valid_letters(note):
                return all(ch in allowed_punct or (ch.isascii() and ch.isalnum())
                           or (ch.isalpha() and unicodedata.name(ch, '').split(' ', 1)[0]
                               in ('CYRILLIC', 'LATIN')) for ch in note)

            answer = json.loads(raw or "{}")
            cards = answer.get("cards")
            if not isinstance(cards, list) or len(cards) != len(photo_ids):
                raise ValueError("Comic writer returned an incomplete card plan")
            used = set()
            result = []
            for position, item in enumerate(cards):
                if not isinstance(item, dict):
                    raise ValueError("Invalid card plan")
                notes = item.get("notes")
                fids = item.get("fact_ids")
                if (not isinstance(notes, list) or not 1 <= len(notes) <= 3
                        or any(not isinstance(v, str) or not v.strip() or len(v.strip()) > 70
                               or not valid_letters(v) for v in notes)
                        or not isinstance(fids, list)
                        or any(not isinstance(fid, str) or fid not in allowed_ids
                               or fid in used for fid in fids)
                        or len(set(fids)) != len(fids)):
                    raise ValueError("Comic card plan does not match the selected evidence")
                # Arrow placement is editorial and may be absent; absence means no arrow.
                raw_arrows = item.get('arrows')
                raw_arrows = raw_arrows if isinstance(raw_arrows, list) else []
                arrows = [a if isinstance(a, str) else '' for a in raw_arrows[:len(notes)]]
                arrows += [''] * (len(notes) - len(arrows))
                notes = [note.strip() for note in notes]
                if position > 0:
                    # A swipe CTA is only allowed on the cover, including embedded CTA lines.
                    retained = [(re.sub(r'(?i)листай\s*→', '', note).strip(), arrow)
                                for note, arrow in zip(notes, arrows)]
                    retained = [(note, arrow) for note, arrow in retained if note]
                    if not retained and position != len(cards) - 1:
                        raise ValueError('Comic inner card contains no fact note after swipe removal')
                    notes, arrows = ([note for note, _ in retained],
                                     [arrow for _, arrow in retained])
                if position == 0:
                    # Add missing product labels without rejecting the writer's scene.
                    if not any(place.casefold() in note.casefold() for note in notes):
                        if len(notes) < 3:
                            notes.insert(0, place)
                            arrows.insert(0, '')
                        else:
                            notes[0] = place + '\n' + notes[0]
                            arrows[0] = ''
                    if not any('листай →' in note.casefold() for note in notes):
                        if len(notes) < 3:
                            notes.append('листай →')
                            arrows.append('')
                        else:
                            notes[-1] += '\nлистай →'
                            arrows[-1] = ''
                elif position == len(cards) - 1:
                    # Brand/footer text is a fixed product label, not writer prose.
                    address_start = address_label.casefold().removeprefix('г. ').strip()
                    city_start = city_label.casefold()
                    city_in_address = (address_start == city_start or
                                       address_start.startswith((city_start + ',', city_start + ' ')))
                    notes = list(dict.fromkeys(['История за поворотом Калининград',
                                                *([] if city_in_address else [city_label]), address_label]))
                    arrows = [''] * len(notes)
                    fids = []
                used.update(item['fact_ids'])
                result.append({'notes': notes, 'arrows': arrows, 'fact_ids': fids})
            return {"cards": result, "model": str(model)}
        # Same owner-approved GLM -> Sol -> Gemini ladder as the caption writer.
        # The lite route remains its existing fallback, not a new semantic writer.
        if getattr(self.settings, 'external_writers', False):
            from . import external_writers
            try:
                result, model = await external_writers.write(contents[0], None, answer_instruction=(
                    '\n\nОтветь одним JSON-объектом: text (краткое название плана) и cards '
                    '(ровно N объектов с полями notes, arrows, fact_ids, hook). Никаких пояснений.'),
                    validate=lambda answer: parse(json.dumps(answer, ensure_ascii=False), 'external'))
                return parse(json.dumps(result, ensure_ascii=False), external_writers.DISPLAY.get(model, model))
            except (external_writers.ExternalWriterUnavailable, ValueError, KeyError, TypeError):
                logging.getLogger(__name__).warning('street_story_comic_writer_fallback story_id=%s', story_id)
        return await self.providers.gemini._lite_chain(contents, config, parse)

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
        included = set(plan["fact_ids"])
        brief_context = {
            "selected_facts": [item for item in context["selected_facts"] if item["fact_id"] in included],
            "caption_image_notes": plan["notes"], "caption_image_note_arrows": plan["arrows"],
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
        if not context["comic"].get("plan"):
            planned = await self._plan_comic(story_id, context)
            context = self._comic_patch(story_id, revision, photo_ids, plan=planned)
            if context is None:
                return
        plans = context["comic"]["plan"]["cards"]
        await self.providers.vibepublish.bootstrap()
        inflight = 0
        for position, photo_id in enumerate(photo_ids):
            with self.store.connection() as db:
                if not self._comic_current(db, story_id, revision, photo_ids):
                    return
                story = dict(self._story_row(db, story_id))
                context = json.loads(story["visual_context_json"] or "{}")
            card = context["comic"]["cards"][position]
            if card.get("asset_ref"):
                continue
            if inflight >= 3:
                break
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
            source_asset = card.get("source_asset_ref")
            if not source_asset:
                ingress_key = "ss-vp-asset-" + digest([story_id, photo_id, card["transform"]])[:48]
                ingress = await self.providers.vibepublish.ingress_asset(data, mime, ingress_key)
                source_asset = str(ingress.get("asset_id") or "")
                if not source_asset:
                    raise RetryableProviderError("VibePublish asset ingress returned no asset ID")
                context = self._comic_patch(story_id, revision, photo_ids, position,
                                            source_asset_ref=source_asset)
                if context is None:
                    return
                card = context["comic"]["cards"][position]
            visual_key = "ss-vp-visual-" + digest([story_id, revision, photo_id, source_asset])[:48]
            operation = card.get("operation_id")
            if not operation:
                cmd = {"command": {"kind": "tune",
                       "source": {"source": {"kind": "asset", "id": source_asset}},
                       "brief": card["brief"], "candidates": 1,
                       "formats": ["original" if card["transform"]["mode"] == "original" else "post_4_5"],
                       "selection": "human", "copy": {}}}
                response = await self.providers.vibepublish.visual(cmd, visual_key)
                operation = str(response.get("operation_id") or "")
                if not operation:
                    raise RetryableProviderError("VibePublish visual accepted no recoverable operation_id")
                context = self._comic_patch(story_id, revision, photo_ids, position, operation_id=operation)
                if context is None:
                    return
                card = context["comic"]["cards"][position]
            inflight += 1
        # Poll the launched batch; VibePublish handles its concurrent imagegen jobs.
        pending = False
        for position, photo_id in enumerate(photo_ids):
            with self.store.connection() as db:
                if not self._comic_current(db, story_id, revision, photo_ids):
                    return
                context = json.loads(self._story_row(db, story_id)['visual_context_json'] or '{}')
            card = context['comic']['cards'][position]
            if card.get('asset_ref'):
                continue
            operation = card.get('operation_id')
            if not operation:
                pending = True  # Remaining members launch in the next batch.
                continue
            receipt = _receipt(await self.providers.vibepublish.status(operation), operation)
            state = receipt.get("state")
            if state in {"accepted", "running", "queued", "processing"}:
                pending = True
                continue
            if state in {"blocked", "failed", "outcome_unknown", "cancelled"}:
                raise PermanentProviderError("VibePublish comic card ended in " + str(state))
            candidate = card.get("candidate") or {}
            if state == "needs_selection":
                choices = receipt.get("candidates")
                if not isinstance(choices, list) or len(choices) != 1:
                    raise PermanentProviderError("Comic card requires exactly one VibePublish candidate")
                candidate = choices[0]
                visual_job_id = receipt.get("visual_job_id")
                provider_revision = receipt.get("visual_revision")
                token = candidate.get("selection_token")
                if (not isinstance(visual_job_id, str) or not isinstance(provider_revision, int)
                        or not isinstance(token, str) or not candidate.get("id")):
                    raise PermanentProviderError("VibePublish candidate selection receipt is incomplete")
                select_key = "ss-vp-select-" + digest([operation, candidate["id"], provider_revision])[:48]
                context = self._comic_patch(story_id, revision, photo_ids, position,
                    candidate=candidate, select_request_key=select_key)
                if context is None:
                    return
                await self.providers.vibepublish.visual({"command": {
                    "kind": "select", "job_id": visual_job_id, "candidate_id": candidate["id"],
                    "expected_revision": provider_revision, "token": token}}, select_key)
                # Provider select reply may omit immutable candidate identity;
                # use the same authoritative status receipt as the single-image path.
                receipt = _receipt(await self.providers.vibepublish.status(operation), operation)
            if receipt.get("state") != "verified":
                raise RetryableProviderError("VibePublish comic card selection pending")
            asset_ref = str(receipt.get("selected_asset_ref") or "")
            if not asset_ref:
                raise PermanentProviderError("VibePublish comic card lacks verified asset")
            # The provider's immutable candidate dimension proof is the same as for one photo.
            evidence = candidate or (receipt.get("candidates") or [{}])[0]
            if (not isinstance(evidence, dict) or evidence.get("asset_ref") != asset_ref
                    or not isinstance(evidence.get("width"), int)
                    or not isinstance(evidence.get("height"), int)
                    or not self._frame_dimensions_match({
                        "output_frame_contract": (POST_4_5_FRAME_CONTRACT if card["transform"]["mode"] == "crop_4_5"
                                                  else ORIGINAL_ART_FRAME_CONTRACT),
                        "source_image_transform": card["transform"]
                    }, evidence["width"], evidence["height"])):
                raise PermanentProviderError("visual_format_unverified")
            context = self._comic_patch(story_id, revision, photo_ids, position, asset_ref=asset_ref,
                candidate=candidate or evidence, selected_sha256=receipt.get("selected_sha256"))
            if context is None:
                return
        if pending:
            raise RetryableProviderError('VibePublish comic cards are processing')
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
