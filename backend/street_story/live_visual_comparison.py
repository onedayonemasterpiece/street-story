"""Article images compared in multimodal tools by the current Live conversation.

No provider connection/key/transport is owned here. SOURCE and REF are separate
vision inputs; the Live tool records the model verdict.
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from live_interaction import with_live_tool_parts

from .identity_lifecycle import PROTECTED, confidence, distance, visual_match
from .identity_telemetry import record_identity_event
from .service import ConflictError, canonical, digest


def _article_acquisition_rank(source):
    """Order reader leads by declared/page role; this never excludes a source."""
    parsed = urlsplit(str(source.get('url') or ''))
    segments = {part.casefold() for part in parsed.path.split('/') if part}
    role = str(source.get('kind') or source.get('source_kind') or source.get('type') or '').casefold()
    if (role in {'article', 'news', 'photo_gallery'}
            or segments.intersection({'article', 'articles', 'news', 'story', 'stories', 'post', 'posts', 'blog'})):
        return 0
    context = ' '.join(str(source.get(key) or '') for key in ('title', 'snippet')).casefold()
    if (role in {'map', 'directory', 'search', 'homepage', 'collection'}
            or segments.intersection({'map', 'maps', 'geo', 'firm', 'search', 'directory', 'catalog', 'adresa', 'addresses'})
            or any(marker in context for marker in ('на карте', 'номерами домов', 'справочник', 'каталог'))):
        return 2
    # An index endpoint can still identify a detail page through its query.
    detail_query = set(parse_qs(parsed.query)).intersection({'id', 'sid', 'article', 'story', 'post'})
    if not detail_query and parsed.path.rstrip('/').casefold() in {'', '/index.php', '/index.html'}:
        return 2
    return 1


class LiveVisualComparisonMixin:
    @staticmethod
    def _visual_control_revision(research, photo_sha256, generation):
        control = (research.get('research_controls') or {}).get('identity') or {}
        if (control.get('photo_sha256') == photo_sha256
                and control.get('identity_generation') == generation):
            return int(control.get('revision') or 0)
        return 0

    def _assert_visual_current(self, row, research, state, *, session=None):
        from .research_control import research_stopped
        generation = int(research.get('identity_generation') or 0)
        revision = self._visual_control_revision(research, row['photo_sha256'], generation)
        if (row['photo_sha256'] != state['photo_sha256'] or generation != state['generation']
                or int(state.get('control_revision') or 0) != revision
                or research_stopped(research, 'identity', photo_sha256=row['photo_sha256'],
                                    identity_generation=generation)):
            raise ConflictError('visual_comparison_changed', 'Фото или управление исследованием изменилось.')
        saved = research.get('visual_search_operation') or {}
        if (saved.get('photo_sha256') == state['photo_sha256'] and saved.get('generation') == state['generation']
                and int(saved.get('control_revision') or 0) != int(state.get('control_revision') or 0)):
            raise ConflictError('visual_comparison_changed', 'Управление исследованием изменилось.')
        if session is not None and saved.get('lease_owner') != session.id:
            raise ConflictError('visual_comparison_changed', 'Исполнитель сравнения уже изменился.')

    def _save_visual_queue(self, session, state, *, db=None, research=None):
        """Store queue addresses and the same pending operation, never image bytes."""
        durable = {k: v for k, v in state.items() if k not in {'pending', 'pending_descriptor', 'seen_images', 'parallel_pair_payloads'}}
        pending = state.get('pending') or {}
        if pending:
            durable['pending_descriptor'] = {key: pending[key] for key in ('id', 'candidates', 'evidence', 'reply')}
        def save(connection, current):
            row = self.service._story_row(connection, session.resource_id)
            self._assert_visual_current(row, current, state, session=session)
            current['visual_search_operation'] = durable
            connection.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(current), row['id']))
        if db is not None:
            save(db, research)
        else:
            with self.service.store.tx() as connection:
                row = self.service._story_row(connection, session.resource_id)
                save(connection, json.loads(row['research_json'] or '{}'))

    @staticmethod
    def _image_entries(candidate):
        from .article_media import public_url
        from .identity_references import unsupported_reference_url
        for supplied in candidate.get('reference_image_urls', []):
            url = public_url(supplied)
            if not url or unsupported_reference_url(url):
                continue
            reference_id = 'ref_' + uuid.uuid5(uuid.NAMESPACE_URL, str(candidate['candidate_id']) + '\n' + url).hex
            allowed = ('image_url', 'article_url', 'kind', 'alt', 'figcaption', 'section_heading', 'context_text', 'article_title')
            media = [{key: m[key] for key in allowed if key in m} for m in candidate.get('article_media', []) if m.get('image_url') == supplied]
            copied = {key: value for key, value in candidate.items() if key not in
                {'reference_evidence', 'model_image_sha256', 'image_sha256', 'source_sha256'}}
            if copied.get('reference_reuse'):
                copied['reference_reuse'] = {key: value for key, value in copied['reference_reuse'].items()
                    if key in {'story_id', 'subject_candidate_id'}}
            yield {**copied, 'reference_id': reference_id, 'reference_image_urls': [url],
                   'reference_batch': True, 'article_media': media}

    async def _find_place_articles(self, session, args):
        from .identity_discovery import web_image_sources
        query = str(args.get('query') or '').strip()[:180]
        if not query:
            raise ConflictError('visual_query_required', 'Нужен поисковый запрос.')
        attempted = session.state.setdefault('identity_search_queries', {})
        story, research = self.service._identity_snapshot(session.resource_id)
        scope = {'photo_sha256': story['photo_sha256'], 'generation': int(research.get('identity_generation') or 0),
                 'control_revision': self._visual_control_revision(research, story['photo_sha256'],
                                                                  int(research.get('identity_generation') or 0))}
        self._assert_visual_current(story, research, scope)
        saved = research.get('identity_article_discovery') or {}
        if saved.get('photo_sha256') == story['photo_sha256'] and saved.get('generation') == int(research.get('identity_generation') or 0):
            attempted.update(saved.get('queries') or {})
        previous = attempted.get(query) or {}
        now = self.service.store.now()
        if previous.get('status') in {'in_progress', 'unknown', 'submitted'}:
            from .identity_discovery import _resume_article_query
            return await _resume_article_query(self.service, story, query, previous)
        if previous.get('status') == 'completed':
            return previous
        if previous.get('retry_at', 0) > now:
            return previous
        from .identity_discovery import _claim_article_query
        claim_id, previous = _claim_article_query(self.service, story, query)
        if not claim_id:
            if previous.get('status') in {'in_progress', 'unknown', 'submitted'}:
                from .identity_discovery import _resume_article_query
                return await _resume_article_query(self.service, story, query, previous)
            return previous
        try:
            visual = session.state.get('visual_comparison') or research.get('visual_search_operation') or {}
            history = visual.get('verdict_history') or []
            latest_verdict = history[-1] if history else {}
            alternative_ids = set(latest_verdict.get('alternative_candidate_ids') or [])
            query_context = {'query_seed': visual.get('query_seed'), 'requested_query': query,
                'last_verdict': latest_verdict, 'recent_verdicts': history[-6:],
                'alternative_candidates': [candidate for candidate in (research.get('visual_identity') or {}).get('candidates', [])
                    if candidate.get('candidate_id') in alternative_ids],
                'search_history': {key: {'status': value.get('status'), 'source_count': len(value.get('sources') or [])}
                    for key, value in (visual.get('searches') or {}).items()},
                'unfinished_sources': [{'url': url, 'status': page.get('status'),
                    'gallery_cursor': page.get('source', {}).get('gallery_cursor'),
                    'gallery_slide_cursor': page.get('source', {}).get('gallery_slide_cursor')}
                    for url, page in (visual.get('sources') or {}).items()
                    if page.get('status') not in {'completed', 'excluded'}]}
            sources = await web_image_sources(self.service, query, '', story={**story,
                '_identity_generation': int(research.get('identity_generation') or 0),
                '_identity_query_context': query_context, '_identity_search_query': query}, first_ready=True)
            result = {'sources': sources, 'status': 'completed', 'search_unavailable': False,
                'instruction': 'Fetch article illustrations with compare_place_images; titles are hypotheses only.'}
        except Exception as exc:
            code = getattr(exc, 'code', None) or type(exc).__name__
            result = {'sources': [], 'status': 'temporary_failure', 'search_unavailable': True,
                'code': code, 'retry_at': max(now + 3, float(getattr(exc, 'retry_at', None) or now + 15)),
                'provider_receipt': getattr(self.service.providers.gemini, 'last_article_discovery_failure', None),
                'instruction': 'Search failed, not a visual mismatch. Retain progress and retry after retry_at or with another query.'}
            record_identity_event(self.service, session.resource_id, 'identity_web_search_unavailable', {'code': code})
            if (session.state.get('visual_comparison') or {}).get('queue') or any(
                    c.get('discovery') == 'web_article_media' and c.get('reference_image_urls')
                    for c in (research.get('visual_identity') or {}).get('candidates', [])):
                result['instruction'] = 'Search failed, but saved illustrations remain. Call compare_place_images to process them before another search.'
        result['claim_id'] = claim_id
        attempted[query] = result
        sources = result['sources']
        if sources:
            old = session.state.get('identity_article_sources', [])
            session.state['identity_article_sources'] = list({x['url']: x for x in [*old, *sources]}.values())
        with self.service.store.tx() as db:
            row = self.service._story_row(db, session.resource_id)
            current = json.loads(row['research_json'] or '{}')
            self._assert_visual_current(row, current, scope)
            history = current.get('identity_article_discovery') or {}
            if history.get('photo_sha256') != row['photo_sha256'] or history.get('generation') != scope['generation']:
                history = {}
            current['identity_article_discovery'] = {**history, 'generation': scope['generation'],
                'photo_sha256': row['photo_sha256'], 'queries': {**history.get('queries', {}), query: result},
                'sources': list({x['url']: x for x in [*history.get('sources', []), *sources]}.values())}
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(current), row['id']))
        return result

    def _continue_identity(self, session):
        if getattr(session, 'closed', False):
            return
        _story, research = self.service._identity_snapshot(session.resource_id)
        from .research_control import research_stopped
        if research_stopped(research, 'identity', photo_sha256=_story['photo_sha256'],
                            identity_generation=int(research.get('identity_generation') or 0)):
            return
        state = session.state.get('visual_comparison') or research.get('visual_search_operation') or {}
        if state.get('pending'):
            return
        identity = research.get('visual_identity') or {}
        if (_story.get('error_code') == 'visual_identity_conflict'
                and int(identity.get('generation') or 0) == int(research.get('identity_generation') or 0)):
            return
        if identity.get('status') in {'match', 'owner_confirmed'}:
            return
        now = self.service.store.now()
        pages = state.get('sources') or {}
        unread_candidates = [c for c in identity.get('candidates', [])
            if c.get('reference_image_urls') and c.get('url') not in pages]
        discovery = research.get('identity_article_discovery') or {}
        unread_sources = [source.get('url') for source in discovery.get('sources', [])
                          if source.get('url') and source.get('url') not in pages]
        searches = {**discovery.get('queries', {}), **state.get('searches', {})}
        from .identity_discovery import next_visual_query
        continuation = next_visual_query(identity, state.get('query_seed') or state.get('query'), searches,
                                         discovery.get('planned_queries', []))
        available = bool(unread_candidates) or bool(unread_sources) or bool(state.get('queue')) or any(p['status'] == 'pending' or
            (p['status'] in {'partial', 'temporary_failure'} and p.get('retry_at', 0) <= now)
            for p in pages.values())
        if not available and continuation:
            available = searches.get(continuation, {}).get('retry_at', 0) <= now
        if not available:
            return
        token = canonical([len(state.get('reviewed_reference_ids', [])), len(state.get('queue', [])),
            [(url, p.get('status'), p.get('attempts')) for url, p in pages.items()],
            [c.get('candidate_id') or c.get('url') for c in unread_candidates],
            unread_sources,
            state.get('query'), continuation,
            [(query, result.get('status'), result.get('retry_at')) for query, result in
                (research.get('identity_article_discovery') or {}).get('queries', {}).items()]])
        if session.state.get('identity_continuation_token') == token:
            return
        session.state['identity_continuation_token'] = token
        self.write(session, {'type': 'text', 'text': 'Continue the pending visual operation: saved illustrations remain. '
            'Call compare_place_images and record_place_comparison in this same conversation. '
            'Let compare_place_images advance the saved article query plan; do not repeat completed searches; stop on proved match.'})

    def _cancel_identity_waiter(self, session):
        task = session.state.pop('identity_wait_task', None)
        if task is not None:
            task.cancel()

    def _watch_identity_ready(self, session):
        """Wake the existing conversation after the existing photo job finishes."""
        self._continue_identity(session)
        task = session.state.get('identity_wait_task')
        if task is not None and not task.done():
            return
        story, research = self.service._identity_snapshot(session.resource_id)
        if story['state'] != 'identifying':
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        binding = (story['photo_sha256'], int(research.get('identity_generation') or 0))
        async def wait():
            started = time.monotonic()
            def observed(status):
                from .live import record_live_diagnostic
                record_live_diagnostic(self.service, session.resource_id, session.id, 'backend',
                    'identity_ready_continuation', {'generation': binding[1], 'status': status,
                        'wait_ms': round((time.monotonic() - started) * 1000)})
            while time.monotonic() - started < 90 and not getattr(session, 'closed', False):
                await asyncio.sleep(0.5)
                current, current_research = self.service._identity_snapshot(session.resource_id)
                if (current['photo_sha256'], int(current_research.get('identity_generation') or 0)) != binding:
                    observed('superseded')
                    return
                if current['state'] != 'identifying':
                    self._continue_identity(session)
                    observed('ready')
                    return
            observed('closed' if getattr(session, 'closed', False) else 'timeout')
        session.state['identity_wait_task'] = loop.create_task(wait())

    async def _comparison_result(self, pending, *, direct_provider=False):
        if direct_provider:
            return dict(pending['reply'])
        pending['live_images_delivered'] = False
        # Shared Live receives a separate SOURCE and REF, rotated and fitted
        # in RAM only to respect its transport and image budget.
        if len(pending['image_parts']) != 2:
            return {**pending['reply'], 'direct_provider_required': True,
                    'instruction': 'Direct background vision owns this addressed group; wait for its outcome.'}
        parts = []
        from .article_media import fetch_public, MAX_DOWNLOAD_BYTES
        from .reference_image_codec import normalize_reference
        import httpx
        try:
            for index, part in enumerate(pending['image_parts']):
                if part.get('url'):
                    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                        _target, _mime, data = await fetch_public(client, part['url'], MAX_DOWNLOAD_BYTES)
                else:
                    data = base64.b64decode(part['data'], validate=True)
                mime, prepared = await asyncio.to_thread(normalize_reference, data)
                encoded = base64.b64encode(prepared).decode('ascii')
                parts.append({'inlineData': {'mimeType': mime,
                    'displayName': 'SOURCE' if index == 0 else 'REF_1', 'data': encoded}})
            pending['live_images_delivered'] = True
            return with_live_tool_parts(pending['reply'], parts)
        except (httpx.HTTPError, ValueError, OSError):
            return {**pending['reply'], 'direct_provider_required': True,
                    'image_delivery_status': 'preparation_failed',
                    'instruction': 'Image preparation for this comparison failed; SOURCE may still be available. '
                        'This is not a visual verdict. Do not record_place_comparison or claim images were reviewed. '
                        'The background queue owns retry/fallback; describe this as a comparison preparation failure, '
                        'not a lost original photo.'}

    async def _next_visual_result(self, session, result):
        """Deliver the next bounded frame while the model owns its verdict."""
        _story, research = self.service._identity_snapshot(session.resource_id)
        identity = research.get('visual_identity') or {}
        if identity.get('status') not in {'uncertain', 'mismatch'}:
            return result
        try:
            comparison = await self._compare_place_images(session, {})
        except Exception as exc:
            # A completed verdict remains acknowledged if fetching the next
            # frame fails. The durable cursor and count survive the failure.
            record_identity_event(self.service, session.resource_id, 'identity_next_frame_unavailable',
                {'code': getattr(exc, 'code', None) or type(exc).__name__})
            return {**result, 'visual_queue_partial': True,
                'instruction': 'Saved progress remains. Resume compare_place_images; no new verdict is available.'}
        merged = {**result, **dict(comparison)}
        if getattr(comparison, 'parts', None):
            return with_live_tool_parts(merged, comparison.parts)
        return merged

    def _send_pending_comparison(self, session):
        pending = (session.state.get('visual_comparison') or {}).get('pending') or {}
        if pending.get('image_parts'):
            self.write(session, {'type': 'text', 'text':
                'A visual comparison is pending. Call compare_place_images to retrieve its SOURCE/REF image '
                'as a multimodal tool result before recording a verdict. Article titles are not visual evidence.'})

    def _visual_lease(self, session, *, expected=None):
        with self.service.store.tx() as db:
            row = self.service._story_row(db, session.resource_id)
            research = json.loads(row['research_json'] or '{}')
            state = research.get('visual_search_operation') or {}
            generation = int(research.get('identity_generation') or 0)
            scope = {'photo_sha256': row['photo_sha256'], 'generation': generation,
                     'control_revision': self._visual_control_revision(research, row['photo_sha256'], generation)}
            self._assert_visual_current(row, research, expected or scope)
            now = self.service.store.now()
            if state.get('lease_until', 0) > now and state.get('lease_owner') not in {None, session.id}:
                from .errors import RetryableProviderError
                raise RetryableProviderError('visual_unit_busy', retry_at=state['lease_until'])
            state.update(lease_owner=session.id, lease_until=now+180, control_revision=scope['control_revision'])
            research['visual_search_operation'] = state
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), row['id']))
        return state

    @staticmethod
    def _visual_reply(comparison_id, candidates, identity, remaining):
        return {'comparison_id': comparison_id, 'snapshot_kind': 'source_and_references',
            'references': [{'label': f'REF {i}', 'candidate_id': c['candidate_id'], 'name': c['name'],
                'source_url': c['reference_image_urls'][0],
                **({'reference_id': c['reference_id'], 'article_url': c.get('url'),
                    'context': [{key: media[key] for key in ('alt','figcaption','section_heading','context_text') if key in media}
                                for media in c.get('article_media') or []]})}
                for i, c in enumerate(candidates, 1)],
            'physical_candidates': [{'candidate_id': c['candidate_id'], 'name': c.get('name', ''),
                'url': c.get('url'), 'distance_m': c.get('distance_m'),
                **{key: c[key] for key in ('map_address', 'map_coordinates', 'road_name', 'map_object') if key in c},
                'alias_candidate_ids': c.get('alias_candidate_ids', [])}
                for c in identity.get('candidates', []) if c.get('identity_eligible') is not False
                and not str(c.get('candidate_id', '')).startswith('web:')][:32],
            'remaining_illustrations': remaining,
            'search_feedback_instruction': (
                'Верни search_feedback: тип REF (modern_exterior/interior/historical/diagram/unclear), '
                'следующее полезное действие (explore_alternative/find_external_view/another_view/verify_binding), '
                'его reason, буквальный next_query или пустую строку, candidate_ids из physical_candidates. '
                'Для интерьера не отвергай здание: запроси внешний вид либо другую страницу. '
                'Для действительно другого фасада переходи к альтернативе; для перспективного частичного вида '
                'запроси недостающий ракурс. Непригодность REF не делает его тему лучшей гипотезой: '
                'если SOURCE явно показывает другой физический тип здания, не продолжай искать ракурсы '
                'этого противоречащего кандидата только потому, что REF исторический. Выбери ещё не '
                'проверенную физическую гипотезу или реальный адрес, согласованный с наблюдениями SOURCE. '
                'Не выдумывай адрес и не повторяй сравнение ради положительного ответа.'),
            'shooting_distance_instruction': (
                'Оцени по SOURCE, перспективе, размеру объекта в кадре и доступным camera_hints '
                'правдоподобный диапазон дистанции съёмки и приблизительную верхнюю границу в метрах. '
                'Кратко запиши оценку, основания и неопределённость в observations. '
                'Если масштаб или зум неизвестны, не выдумывай верхнюю границу. '
                'Сопоставь оценку с distance_m кандидатов; не подтверждай дальний вариант '
                'за счёт придуманного зума или иной точки съёмки при близкой визуально подходящей '
                'альтернативе. При неразрешённом противоречии верни uncertain. '
                'Оценка не является точным измерением или самостоятельным доказательством identity.'),
            'instruction': 'Сравни SOURCE и REF по отличительным деталям; запиши вердикт через record_place_comparison. Для определения объекта используй современные фотографии; архивный исторический снимок не является подходящим REF и не даёт match. Для web REF candidate_id — показанный REF; reference_subject_candidate_id — доказанный физический кандидат из physical_candidates. Map_object описывает именно mapped_entry: парковка, вход, учреждение, улица и здание не становятся одним объектом от близости точек. Если SOURCE показывает целый дом, выбери физическое здание либо документированную адресную точку дома; ресторан, магазин и другое учреждение внутри дома — отдельные сущности. Название арендатора может помочь поиску, но не переименовывает дом и не доказывает связь с ним. Reverse display_name — контекст ближайшего объекта, а не имя здания на SOURCE. Связывай REF с подходящим типом объекта и реальным адресом; при неразрешённой привязке верни uncertain. Проверяй альтернативы всего shortlist. Расстояния — контекст съёмки, а не доказательство identity. Разделяй устойчивую геометрию и изменяемую отделку: цвет стен, вывески и цветочные ящики сами по себе не устанавливают ни match, ни mismatch. Положительный вывод требует видимых общих отличительных положений и пропорций окон, выступов, арок и карниза. Не объясняй различия геометрии или композиции предположениями о ремонте, реконструкции, переносе или добавлении элементов: если без этих недоказанных изменений match не получается, верни uncertain. Название статьи, реклама и другие объекты не доказательство.'}

    async def _compare_place_images(self, session, args, *, page_budget=4, expected_scope=None, search_budget=1, parallel_refill=False):
        story, research = self.service._identity_snapshot(session.resource_id)
        generation = int(research.get('identity_generation') or 0)
        identity = research.get('visual_identity') or {}
        catalog = {c.get('candidate_id'): c for c in identity.get('candidates', [])}
        collection_pages = {}
        def reference_eligible(candidate):
            from .article_media import collection_reference
            url = candidate.get('url')
            if url not in collection_pages:
                collection_pages[url] = collection_reference(candidate, self.service.store)
            if collection_pages[url]:
                return False
            if str(candidate.get('candidate_id') or '').startswith('web:'):
                return True  # Article REF still requires the existing eligible physical-subject gate.
            known = catalog.get(candidate.get('candidate_id'), candidate)
            if candidate.get('identity_eligible') is not False and known.get('identity_eligible') is not False:
                return True
            from .identity_subject_binding import documented_physical_subject
            return documented_physical_subject(known, catalog) is not None
        if (story.get('error_code') == 'visual_identity_conflict'
                and int(identity.get('generation') or 0) == generation):
            return {'identity_conflict': True, 'exhausted': True, 'visual_identity': identity}
        if identity.get('status') in {'match', 'owner_confirmed'}:
            return {'already_resolved': True, 'visual_identity': identity}
        try:
            source_bytes = self.service._source_photo_bytes(story['id'])
        except ConflictError as exc:
            if exc.code != 'source_unavailable':
                raise
            from .errors import RetryableProviderError
            raise RetryableProviderError('source_unavailable', retry_at=self.service.store.now()+300) from exc

        expected = {'photo_sha256': story['photo_sha256'], 'generation': generation,
                    'control_revision': self._visual_control_revision(research, story['photo_sha256'], generation)}
        lease = self._visual_lease(session, expected=expected_scope or expected)
        # An uncertain verdict intentionally has no selected candidate_name.
        # Retain a reference-bearing hypothesis for search, never as identity proof.
        query_hint = str(args.get('query') or identity.get('candidate_name') or next(
            (c['name'] for c in identity.get('candidates', [])
             if c.get('name') and c.get('reference_image_urls') and c.get('identity_eligible', True)), ''))[:180]
        state = session.state.get('visual_comparison')
        if state and (state.get('generation') != generation or state.get('photo_sha256') != story['photo_sha256']
                      or int(state.get('control_revision') or 0) != expected['control_revision']):
            state = None
        if not state:
            state = lease if (lease.get('generation') == generation and lease.get('photo_sha256') == story['photo_sha256']
                              and 'queue' in lease) else None
        if state is None:
            initial_selection = None
            selector = getattr(self.service.providers.gemini, 'select_identity_sources', None)
            observed = [{'url': c['url'], 'title': c.get('name', '')}
                        for c in identity.get('candidates', [])
                        if reference_eligible(c) and c.get('url') and c.get('reference_image_urls')
                        and c.get('discovery') != 'web_article_media']
            if observed and callable(selector):
                from .reference_image_codec import normalize_reference
                image = await asyncio.to_thread(normalize_reference, source_bytes)
                selected = await selector('Select initial reference pages useful for SOURCE; nearby article titles are hypotheses.',
                    observed, {**story, '_identity_selection_image': image})
                initial_selection = selected['source_selection']
                allowed_urls = {x['url'] for x in selected['sources']} & {x['url'] for x in observed}
                initial_selection = {**initial_selection, 'observed_sources': observed,
                                     'selected_urls': sorted(allowed_urls)}
            queue = []
            for candidate in identity.get('candidates', []):
                if (reference_eligible(candidate) and candidate.get('reference_image_urls')
                        and candidate.get('discovery') != 'web_article_media'
                        and (initial_selection is None or candidate.get('url') in allowed_urls)):
                    from .identity_references import original_reference
                    urls = list(dict.fromkeys(original_reference(url) or url for url in candidate['reference_image_urls']))
                    queue.extend(self._image_entries({**candidate, 'reference_image_urls': urls}))
            state = {'generation': generation, 'photo_sha256': story['photo_sha256'],
                     'control_revision': expected['control_revision'],
                     'queue': queue, 'web_searched': False, 'query': query_hint,
                     'query_seed': query_hint,
                     'reviewed_reference_ids': list((research.get('identity_progress') or {}).get('reviewed_reference_ids') or [])
                         if (research.get('identity_progress') or {}).get('generation', generation) == generation else [],
                     'browser_budget': {'remaining': 2}, 'sources': {}, 'searches': {}, 'fetch_failures': []}
            if initial_selection is not None:
                state['initial_reference_selection'] = initial_selection
                record_identity_event(self.service, story['id'], 'identity_initial_reference_sources_selected', {
                    'generation': generation, 'discovered_count': len(observed),
                    'selected_count': len(allowed_urls), 'reference_count': len(queue)})
            session.state['visual_comparison'] = state
        state.update(lease_owner=session.id, lease_until=lease['lease_until'],
                     control_revision=expected['control_revision'])
        session.state['visual_comparison'] = state
        state.setdefault('reviewed_reference_ids', [])
        progress = research.get('identity_progress') or {}
        if progress.get('generation', generation) == generation:
            # Background comparisons may finish after the first Live tool call.
            state['reviewed_reference_ids'] = list(dict.fromkeys([*state['reviewed_reference_ids'],
                *(progress.get('reviewed_reference_ids') or [])]))
        # Accept later native/API URLs even after the first discovery portion.
        urls = args.get('article_urls') or []
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise ConflictError('visual_article_urls_invalid', 'Некорректные ссылки статей.')
        discovery = research.get('identity_article_discovery') or {}
        if discovery.get('generation') != generation or discovery.get('photo_sha256') != story['photo_sha256']:
            discovery = {}
        sources = [{'url': url} for url in urls] + session.state.get('identity_article_sources', []) + discovery.get('sources', [])
        # Acquisition hints from previous stories stay useful before this
        # photo's identity is confirmed. Every image needs a fresh verdict.
        from .poi_memory import candidate_article_sources
        with self.service.store.connection() as db:
            memory_sources = candidate_article_sources(db, identity.get('candidates') or [])
        sources.extend(memory_sources)
        state.setdefault('sources', {})
        state.setdefault('searches', {})
        state['searches'].update(discovery.get('queries') or {})
        state['planned_queries'] = list(discovery.get('planned_queries') or state.get('planned_queries') or [])
        state.setdefault('units_since_planned_query', len(state['reviewed_reference_ids']))
        state.setdefault('fetch_failures', [])
        # Recovery prefetch may exceed the bounded physical-candidate catalog.
        # Hydrate its saved media once; subsequent passes use this same cursor.
        for url, saved_page in (discovery.get('pages') or {}).items():
            if url not in state['sources']:
                state['sources'][url] = {key: value for key, value in saved_page.items() if key != 'candidates'}
                for candidate in saved_page.get('candidates', []):
                    state['queue'].extend(self._image_entries(candidate))
        # Wiki lead bytes may already have been compared by the background
        # worker. Enumerate the actual article through the same durable HTTP /
        # quiet-browser reader before advancing to broad API discovery.
        from urllib.parse import urlsplit
        for candidate in identity.get('candidates', []):
            if not reference_eligible(candidate):
                continue
            url = str(candidate.get('url') or '')
            selection = state.get('initial_reference_selection')
            if selection is not None and url not in selection['selected_urls']:
                continue
            if candidate.get('reference_image_urls') and (urlsplit(url).hostname or '').endswith('.wikipedia.org'):
                state['sources'].setdefault(url, {'source': {'url': url, 'candidate_id': candidate['candidate_id']},
                    'status': 'pending', 'attempts': 0})
        memory_added = 0
        for source in sources:
            if source.get('discovery_provider') == 'poi_memory' and source['url'] not in state['sources']:
                memory_added += 1
            state['sources'].setdefault(source['url'], {'source': source, 'status': 'pending', 'attempts': 0})
        if memory_added:
            record_identity_event(self.service, story['id'], 'identity_poi_sources_reused', {
                'generation': generation, 'source_count': memory_added, 'identity_proof_reused': False})
        state.pop('seen_images', None)
        if not state.get('pending') and state.get('pending_descriptor'):
            restored = dict(state['pending_descriptor'])
            restored['image_parts'] = [{'label': 'SOURCE', 'mime_type': story.get('photo_mime_type') or 'image/jpeg',
                'data': base64.b64encode(source_bytes).decode('ascii')}] + [
                {'label': f'REF {i}', 'mime_type': 'image/jpeg', 'url': c['reference_image_urls'][0]}
                for i, c in enumerate(restored['candidates'], 1)]
            state['pending'] = restored
        if state.get('pending'):
            from .identity_references import unsupported_reference_url
            pending = state['pending']
            unusable = [c for c in pending['candidates']
                        if any(unsupported_reference_url(url) for url in c.get('reference_image_urls') or [])]
            if unusable:
                # A legacy descriptor can predate transport validation. It may
                # be retired only when no inference outcome is unresolved.
                blocked = False
                with self.service.store.connection() as db:
                    for attempt in db.execute("SELECT receipt_json FROM research_provider_attempts "
                                              "WHERE story_id=? AND role LIKE 'vision%'", (story['id'],)):
                        receipt = json.loads(attempt['receipt_json'])
                        binding = receipt.get('binding') or {}
                        if receipt.get('generation', binding.get('generation', generation)) != generation:
                            continue
                        if (receipt.get('phase') not in {'failed', 'aborted', 'completed'}
                                or receipt.get('provider_send_state') == 'possibly_sent'
                                or (receipt.get('phase') == 'completed'
                                    and receipt.get('comparison_id') == pending['id'])):
                            blocked = True
                            break
                if not blocked:
                    rejected_ids = {c['reference_id'] for c in unusable}
                    state.setdefault('skipped_reference_ids', []).extend(
                        ref for ref in rejected_ids if ref not in state.get('skipped_reference_ids', []))
                    state['queue'] = [c for c in pending['candidates'] if c not in unusable] + state['queue']
                    state['pending'] = None
                    self._save_visual_queue(session, state)
                    record_identity_event(self.service, story['id'], 'identity_reference_skipped', {
                        'generation': generation, 'reason': 'unsupported_reference_format',
                        'reference_ids': sorted(rejected_ids), 'comparison_id': pending['id']})
        if state.get('pending'):
            # A pending verdict must not swallow URLs supplied by a later tool.
            self._save_visual_queue(session, state)
            return await self._comparison_result(state['pending'], direct_provider=session.id.startswith('headless:'))
        # Address accepted references by the current shortlist, not an archive
        # scan or old SOURCE verdict. Unknown sends retain their original pair.
        with self.service.store.connection() as db:
            unsettled = False
            for row in db.execute("SELECT logical_id,role,receipt_json FROM research_provider_attempts WHERE story_id=? AND role LIKE 'vision%'", (story['id'],)):
                receipt = json.loads(row['receipt_json'])
                binding = receipt.get('binding') or {}
                if parallel_refill and session.id.startswith('headless:'):
                    from .visual_attachments import visual_operation_unit
                    # Only exact saved children of this owned queue may run
                    # independently. A legacy/group/unrelated UNKNOWN still
                    # fences new dispatch, even with a similar comparison ID.
                    addressed = {**story, '_identity_generation': generation}
                    owned = any(row['logical_id'] == digest([story['id'], generation, row['role'],
                        canonical(visual_operation_unit({**addressed,
                            '_visual_reference_mapping': pair['reply']['references']}, pair['reply']))])
                        for pair in state.get('parallel_pairs', [])
                        if pair.get('phase') not in {'completed', 'failed', 'skipped'})
                    if owned:
                        continue
                # Local admission refusals stay CREATED but never sent a
                # comparison. They must not suppress other article/query work.
                # Contradictory send markers still fence the original operation.
                send_marked = (receipt.get('provider_send_state') not in {None, 'not_sent'}
                    or any(container.get(key) for container in (receipt, binding)
                           for key in ('message_id', 'messageID', 'turn_id', 'turnId', 'possibly_sent')))
                created_unsent = receipt.get('phase') == 'created' and not send_marked
                if (receipt.get('phase') not in {'completed', 'failed', 'aborted'}
                        and not created_unsent
                        and (binding.get('visual_scope') is True
                             or receipt.get('photo_sha256', binding.get('photo_sha256')) == story['photo_sha256'])
                        and receipt.get('generation', binding.get('generation', generation)) == generation):
                    unsettled = True
                    if receipt.get('phase') not in {'created', 'completed', 'failed', 'aborted'} or send_marked:
                        from .errors import RetryableProviderError
                        # Restarts must not turn a possibly-sent group into a
                        # brand new pair. Its exact outcome remains unresolved.
                        raise RetryableProviderError('research_visual_outcome_unknown',
                                                     retry_at=self.service.store.now()+300)
            if not state.get('accepted_references_loaded') and not unsettled:
                from .poi_memory import candidate_reference_images
                reused = candidate_reference_images(db, identity.get('candidates') or [])
                entries = [entry for candidate in reused for entry in self._image_entries(candidate)]
                existing = {(c['candidate_id'], tuple(c.get('reference_image_urls') or [])) for c in state['queue']}
                entries = [c for c in entries if (c['candidate_id'], tuple(c['reference_image_urls'])) not in existing]
                state['queue'] = entries + state['queue']
                state['accepted_references_loaded'] = True
                if entries:
                    record_identity_event(self.service, story['id'], 'identity_accepted_references_reused',
                        {'generation': generation, 'reference_count': len(entries), 'identity_proof_reused': False})
        read_pages = 0
        searches_performed = 0
        from .article_media import article_candidates
        physical = [c for c in identity.get('candidates', []) if c.get('identity_eligible') is not False
            and not str(c.get('candidate_id', '')).startswith('web:')]
        physical_ids = {value for c in physical for value in [c.get('candidate_id'), *(c.get('alias_candidate_ids') or [])] if value}
        article_urls = {str(url).rstrip('/') for c in physical for url in [c.get('url'), c.get('wikipedia_url')]
            if url and (urlsplit(str(url)).hostname or '').endswith('.wikipedia.org')}

        # A previous positive belongs to its old SOURCE. Use its explicit
        # subject only to schedule current hypotheses, never as match evidence.
        def reference_distance(candidate):
            subject = (candidate.get('reference_reuse') or {}).get('subject_candidate_id')
            ids = {candidate.get('candidate_id'), subject}
            url = str(candidate.get('url') or '').rstrip('/')
            return min((distance(c) for c in physical if ids.intersection(
                {c.get('candidate_id'), *(c.get('alias_candidate_ids') or [])})
                or (url and url in {str(c.get('url') or '').rstrip('/'),
                                   str(c.get('wikipedia_url') or '').rstrip('/')})), default=float('inf'))

        def page_distance(page):
            source = page.get('source') or {}
            ids = set(source.get('memory_candidate_ids') or [])
            ids.add(source.get('candidate_id'))
            url = str(source.get('url') or '').rstrip('/')
            return min((distance(c) for c in physical if ids.intersection(
                {c.get('candidate_id'), *(c.get('alias_candidate_ids') or [])})
                or (url and url in {str(c.get('url') or '').rstrip('/'),
                                   str(c.get('wikipedia_url') or '').rstrip('/')})), default=float('inf'))

        if state['queue'] and not unsettled:
            previous_head = state['queue'][0].get('reference_id')
            # Cover distinct source pages before repeatedly consuming one
            # gallery. Counts are scheduling evidence, never identity evidence.
            coverage = Counter(ref.get('url') for item in state.get('verdict_history', [])
                for ref in item.get('references', []))
            coverage.update(pair['candidates'][0].get('url')
                for pair in state.get('parallel_pairs', [])
                if pair.get('phase') not in {'completed', 'failed', 'skipped'})
            state['source_comparison_coverage'] = dict(coverage)
            state['queue'].sort(key=lambda c: (coverage[c.get('url')],
                reference_distance(c), 0 if c.get('reference_reuse') else 1))
            if state['queue'][0].get('reference_id') != previous_head:
                record_identity_event(self.service, story['id'], 'identity_reference_priority', {
                    'generation': generation, 'reason': 'current_shortlist_proximity',
                    'candidate_id': state['queue'][0]['candidate_id'],
                    'distance_m': (reference_distance(state['queue'][0])
                                   if reference_distance(state['queue'][0]) != float('inf') else None),
                    'reference_count': len(state['queue']), 'identity_proof_reused': False})

        def source_rank(page):
            source = page.get('source') or {}
            if source.get('accepted_reference'):
                return -1
            if str(source.get('url') or '').rstrip('/') in article_urls:
                return 0
            if physical_ids.intersection(source.get('memory_candidate_ids') or []):
                return 1
            return 2

        def acquisition_priority(page):
            # An acquired article is a cheap media lead, regardless of whether
            # any prior comparison was positive or negative. The normal reader
            # still verifies/refetches image bytes and the common gate decides.
            source_url = str((page.get('source') or {}).get('url') or '')
            saved = self.service.store.cache_get('public-article-acquisition-v1:' + hashlib.sha256(source_url.encode()).hexdigest())
            cached = False
            if saved and saved.get('final_url') == source_url:
                try:
                    body = base64.b64decode(saved['body'], validate=True)
                    cached = hashlib.sha256(body).hexdigest() == saved.get('sha256')
                except (KeyError, TypeError, ValueError):
                    pass
            # Ready references get their initial turn immediately. After that,
            # independently discovered articles must not wait for a whole Wiki
            # gallery. Provenance schedules acquisition; the model binds POI.
            independent = source_rank(page) == 2 and bool((page.get('source') or {}).get('discovery_provider'))
            article_turn = int(state.get('preferred_units') or 0) >= 2 and independent
            # Prefer detailed independent articles to map/directory navigation.
            # Attempts stay first so unread lower-ranked sources retain a turn.
            reader_rank = _article_acquisition_rank(page.get('source') or {}) if source_rank(page) == 2 else 0
            return (int(not article_turn), page.get('attempts', 0), reader_rank,
                    page_distance(page), source_rank(page), int(not cached))

        async def acquire_page(page):
            receipts = []
            articles = await article_candidates(self.service, {**story, '_identity_generation': generation},
                [page['source']], set(research.get('identity_rejected_ids') or []), receipts=receipts)
            page['attempts'] += 1
            page['status'] = receipts[0]['status'] if receipts else 'temporary_failure'
            if receipts:
                page['source']['gallery_cursor'] = receipts[0].get('gallery_cursor', 0)
                page['source']['gallery_slide_cursor'] = receipts[0].get('gallery_slide_cursor', 0)
                if 'static_media_delivered' in receipts[0]:
                    page['source']['static_media_delivered'] = receipts[0]['static_media_delivered']
            page['retry_at'] = self.service.store.now() + 15 if page['status'] != 'completed' else 0
            for candidate in articles:
                state['queue'].extend(self._image_entries(candidate))

        # Give the remaining model-authored address/view plan a bounded turn.
        # Pending/UNKNOWN operations were returned above, before any new search.
        from .identity_discovery import next_visual_query
        planned = next_visual_query({}, '', state['searches'], state['planned_queries'])
        untried = planned and not any(' '.join(query.split()).casefold() == ' '.join(planned.split()).casefold()
                                     for query in state['searches'])
        useful_due = any(page['status'] in {'pending', 'partial'}
                         and page.get('retry_at', 0) <= self.service.store.now()
                         for page in state['sources'].values())
        ready_source_urls = {entry.get('url') for entry in state['queue']}
        unread_due = any(page['status'] == 'pending' and not page.get('attempts', 0)
                        and page.get('source', {}).get('url') not in ready_source_urls
                        and page.get('retry_at', 0) <= self.service.store.now()
                        for page in state['sources'].values())
        planned_reference_ready = False
        first_page_due = unread_due and int(state.get('units_since_acquisition') or 0) >= 2
        if (untried and not unsettled and search_budget > 0 and not first_page_due
                and (int(state['units_since_planned_query']) >= 2 or (not state['queue'] and not useful_due))):
            session.state.setdefault('identity_search_queries', {}).update(state['searches'])
            result = await self._find_place_articles(session, {'query': planned})
            searches_performed += 1
            state['searches'][planned] = result
            state['units_since_planned_query'] = 0
            new_pages = []
            for source in result['sources']:
                if source['url'] not in state['sources']:
                    page = {'source': source, 'status': 'pending', 'attempts': 0}
                    state['sources'][source['url']] = page
                    new_pages.append(page)
            self._save_visual_queue(session, state)
            record_identity_event(self.service, story['id'], 'identity_planned_query_turn', {
                'generation': generation, 'query': planned, 'status': result['status'],
                'gallery_frames_retained': len(state['queue']), 'new_source_count': len(new_pages)})
            if new_pages and read_pages < page_budget:
                tail_count = len(state['queue'])
                read_pages += 1
                await acquire_page(min(new_pages, key=acquisition_priority))
                state['units_since_acquisition'] = 0
                planned_reference_ready = len(state['queue']) > tail_count
                state['queue'] = state['queue'][tail_count:] + state['queue'][:tail_count]
            self._save_visual_queue(session, state)

        # A broad gallery must not starve unread articles tied to the physical
        # shortlist. This is acquisition order only, never identity evidence.
        # Preserve the entire gallery and do not preempt an unknown dispatch.
        if (state['queue'] and not unsettled and not planned_reference_ready
                and int(state.get('preferred_units') or 0) < 2):
            head = state['queue'][0]
            preferred = [p for p in state['sources'].values() if source_rank(p) < 2
                and p['status'] not in {'completed', 'excluded'} and p.get('retry_at', 0) <= self.service.store.now()
                and str(p.get('source', {}).get('url') or '').rstrip('/') != str(head.get('url') or '').rstrip('/')
                and (page_distance(p) < reference_distance(head)
                     or (page_distance(p) == float('inf') == reference_distance(head)
                         and not head.get('reference_reuse')))]
            if not head.get('reference_reuse') and preferred:
                if not unsettled:
                    for page in sorted(preferred, key=acquisition_priority):
                        if read_pages >= page_budget:
                            break
                        read_pages += 1
                        tail_count = len(state['queue'])
                        await acquire_page(page)
                        if len(state['queue']) > tail_count:
                            state['queue'] = state['queue'][tail_count:] + state['queue'][:tail_count]
                            record_identity_event(self.service, story['id'], 'identity_source_priority', {
                                'generation': generation, 'reason': 'unread_shortlist_article',
                                'gallery_frames_retained': tail_count, 'new_reference_count': len(state['queue'])-tail_count})
                            break
                        state['units_since_acquisition'] = 0
        if state['queue'] and not unsettled:
            # Bounded alternation serves other saved hypotheses as well. The
            # whole gallery remains addressable; this is no source/image cap.
            def queued_rank(candidate):
                if candidate.get('reference_reuse'):
                    return -1
                return source_rank(state['sources'].get(candidate.get('url')) or {'source': {'url': candidate.get('url')}})
            prefer_broad = int(state.get('preferred_units') or 0) >= 2
            target = min((i for i, candidate in enumerate(state['queue'])
                if (queued_rank(candidate) == 2 if prefer_broad else queued_rank(candidate) < 2)),
                key=lambda i: (state.get('source_comparison_coverage', {}).get(state['queue'][i].get('url'), 0),
                               reference_distance(state['queue'][i])), default=None)
            if target is not None:
                state['queue'].insert(0, state['queue'].pop(target))
            if int(state.get('units_since_acquisition') or 0) >= 2 and read_pages < page_budget:
                due = [page for page in state['sources'].values()
                       if page['status'] not in {'completed', 'excluded'}
                       and page.get('retry_at', 0) <= self.service.store.now()
                       and page.get('source', {}).get('url') != state['queue'][0].get('url')]
                if due:
                    page = min(due, key=acquisition_priority)
                    tail_count = len(state['queue'])
                    read_pages += 1
                    await acquire_page(page)
                    state['units_since_acquisition'] = 0
                    if len(state['queue']) > tail_count:
                        state['queue'] = state['queue'][tail_count:] + state['queue'][:tail_count]
                        record_identity_event(self.service, story['id'], 'identity_source_priority', {
                            'generation': generation, 'reason': 'independent_article_turn',
                            'gallery_frames_retained': tail_count,
                            'new_reference_count': len(state['queue'])-tail_count})
        if not state['queue']:
            from .article_media import article_candidates
            query = str(args.get('query') or state['query'] or query_hint)[:180]
            if not query:
                from .identity_discovery import next_visual_query
                query = next_visual_query(identity, '', state['searches'], state['planned_queries'])
            state['query'] = query
            if not state.get('query_seed'):
                state['query_seed'] = query
            if not query and not state['sources']:
                return {'query_required': True, 'instruction': 'Use visible features or an object-name hypothesis as query; do not ask the owner to identify it.'}
            saved_articles = [c for c in identity.get('candidates', []) if c.get('discovery') == 'web_article_media']
            for candidate in saved_articles:
                if candidate['url'] not in state['sources']:
                    state['queue'].extend(self._image_entries(candidate))
                    state['sources'][candidate['url']] = {'status': candidate.get('enumeration_status', 'completed'),
                        'source': {'url': candidate['url'], **{key: value for key, value in
                            candidate.get('discovery_provenance', {}).items() if key in {'gallery_cursor', 'gallery_slide_cursor', 'static_media_delivered'}}}, 'attempts': 1}
            available = any(p['status'] in {'pending', 'partial', 'temporary_failure'}
                            for p in state['sources'].values())
            previous = state['searches'].get(query) or {}
            if not available and not state['queue'] and previous.get('status') == 'completed':
                from .identity_discovery import next_visual_query
                following = next_visual_query(identity, state['query_seed'], state['searches'], state['planned_queries'])
                if following:
                    query = state['query'] = following
                    previous = state['searches'].get(query) or {}
            if not available and not state['queue'] and query and previous.get('status') != 'completed' and search_budget > searches_performed:
                record_identity_event(self.service, story['id'], 'identity_web_media_started', {'generation': generation})
                session.state.setdefault('identity_search_queries', {}).update(state['searches'])
                result = await self._find_place_articles(session, {'query': query})
                searches_performed += 1
                state['searches'][query] = result
                for source in result['sources']:
                    state['sources'].setdefault(source['url'], {'source': source, 'status': 'pending', 'attempts': 0})
            # Read the first usable page, not all 20 before showing any image.
            # Give unread URLs a turn before retrying unavailable pages. A slow
            # scheduler must not revisit its first failed four forever.
            for page in sorted(state['sources'].values(), key=acquisition_priority):
                if state['queue'] or read_pages >= page_budget:
                    break
                if page['status'] in {'completed', 'excluded'} or page.get('retry_at', 0) > self.service.store.now():
                    continue
                read_pages += 1
                await acquire_page(page)
            state['web_searched'] = state['searches'].get(query, {}).get('status') == 'completed'
        self._save_visual_queue(session, state)
        references, evidence, candidates = [], [], []
        from .identity_references import unsupported_reference_url
        reference_limit = min(4, max(1, int(getattr(session, 'visual_reference_limit', 1))))
        while state['queue'] and len(references) < reference_limit:
            if references:
                other = next((i for i, item in enumerate(state['queue'])
                    if item.get('url') not in {c.get('url') for c in candidates}), None)
                if other is not None:
                    state['queue'].insert(0, state['queue'].pop(other))
            candidate = state['queue'].pop(0)
            if not reference_eligible(candidate):
                continue  # Context articles remain URL sources, never physical POI candidates.
            if any(unsupported_reference_url(url) for url in candidate.get('reference_image_urls') or []):
                continue
            reference_id = candidate.get('reference_id') or next(self._image_entries(candidate))['reference_id']
            if (reference_id in state['reviewed_reference_ids']
                    or reference_id in state.get('unavailable_reference_ids', [])
                    or reference_id in {e['reference_id'] for e in evidence}
                    or reference_id in {p['reference_id'] for p in state.get('parallel_pairs', [])}):
                continue
            receipts = []
            images = await self.service._candidate_reference_images([candidate], limit=1,
                story_id=story['id'], evidence=receipts)
            if not images or not receipts:
                continue
            receipts[0].update(reference_id=reference_id, label=f'REF {len(references)+1}')
            candidate['reference_id'] = reference_id
            references.extend(images)
            evidence.extend(receipts)
            candidates.append(candidate)
            state['units_since_planned_query'] = int(state.get('units_since_planned_query') or 0) + 1
            state['units_since_acquisition'] = int(state.get('units_since_acquisition') or 0) + 1
            state['preferred_units'] = int(state.get('preferred_units') or 0) + 1 if candidate.get('reference_reuse') or source_rank(
                state['sources'].get(candidate.get('url')) or {'source': {'url': candidate.get('url')}}) < 2 else 0
        if not references:
            # Retry failed media on a later turn; never manufacture a verdict.
            failed = state['fetch_failures']
            state['fetch_failures'] = []
            state['queue'].extend(failed)
            partial = bool(state['queue']) or any(p['status'] not in {'completed', 'excluded'} for p in state['sources'].values())
            unavailable = any(r.get('status') != 'completed' for r in state['searches'].values())
            self._save_visual_queue(session, state)
            remaining_pages = page_budget - read_pages
            if remaining_pages > 0 and not state['queue'] and any(p['status'] == 'pending' for p in state['sources'].values()):
                # Skip already-reviewed leads mechanically. Share one bounded
                # page allowance across refills, stopping at the first new frame.
                return await self._compare_place_images(session, args, page_budget=remaining_pages,
                    expected_scope=expected_scope, search_budget=search_budget-searches_performed,
                    parallel_refill=parallel_refill)
            if not state['web_searched'] and state['query'] and not partial and not unavailable:
                if search_budget > searches_performed:
                    return await self._compare_place_images(session, args, page_budget=remaining_pages,
                        expected_scope=expected_scope, search_budget=search_budget-searches_performed,
                    parallel_refill=parallel_refill)
                return {'partial': True, 'next_query': state['query'], 'autonomous_continuation': True,
                    'instruction': 'The next saved visual hypothesis awaits search admission; continue this same operation.'}
            if partial or unavailable:
                return {'partial': True, 'search_unavailable': unavailable, 'images_compared': 0,
                    'instruction': 'Queue retained. Some articles/images are unavailable or partial; continue later or supply new API-found article URLs/query. This is not exhausted or mismatch.'}
            from .identity_discovery import next_visual_query
            following = next_visual_query(identity, state.get('query_seed'), state['searches'], state['planned_queries'])
            if following:
                state.update(query=following, web_searched=False)
                self._save_visual_queue(session, state)
                return {'partial': True, 'next_query': following, 'autonomous_continuation': True,
                    'instruction': 'Current query completed; continue with the saved alternative/view query in this same operation. Identity remains unproved.'}
            record_identity_event(self.service, story['id'], 'identity_finished', {
                'generation': generation, 'status': 'uncertain', 'candidate_id': identity.get('candidate_id'), 'reference_verified': False})
            return {'exhausted': True, 'instruction': 'Available illustrations reviewed for this query. A new query or article URLs can continue the same queue.'}
        if not isinstance(source_bytes, bytes) or not source_bytes:
            from .errors import RetryableProviderError
            raise RetryableProviderError('source_photo_ram_unavailable', retry_at=self.service.store.now()+30)
        comparison_id = 'comparison_' + uuid.uuid4().hex
        reply = self._visual_reply(comparison_id, candidates, identity, len(state['queue']))
        from .camera_hints import model_camera_hints, read_camera_hints
        binding = research.get('photo_camera_hints') or {}
        hints = (binding.get('metadata') if binding.get('photo_sha256') == story['photo_sha256']
                 and isinstance(binding.get('metadata'), dict) else read_camera_hints(source_bytes))
        reply['camera_hints'] = model_camera_hints(hints)
        reply['camera_hints_instruction'] = (
            'focal_length_35mm уже является эквивалентным фокусным расстоянием. '
            'Не умножай его автоматически на digital_zoom_ratio: поля могут описывать один и тот же зум. '
            'distance_m — расстояние до координаты POI, которая может обозначать центр здания или территории, '
            'а не точную дистанцию до видимого фасада.')
        image_parts = [{'label': 'SOURCE', 'mime_type': story.get('photo_mime_type') or 'image/jpeg',
            'data': base64.b64encode(source_bytes).decode('ascii')}] + [
            {'label': f'REF {i}', 'mime_type': mime, 'url': url}
            for i, (_cid, mime, url) in enumerate(references, 1)]
        state['pending'] = {'id': comparison_id, 'candidates': candidates,
            'evidence': evidence, 'reply': reply, 'image_parts': image_parts}
        self._save_visual_queue(session, state)
        record_identity_event(self.service, story['id'], 'identity_live_comparison_sent', {
            'generation': generation, 'reference_count': len(references),
            'comparison_id': comparison_id, 'model': session.model, 'delivery': 'direct_source_and_refs'})
        return await self._comparison_result(state['pending'], direct_provider=session.id.startswith('headless:'))

    def _record_place_comparison(self, session, command_id, args):
        state = session.state.get('visual_comparison') or {}
        pending = state.get('pending') or {}
        if not pending or args.get('comparison_id') != pending['id']:
            raise ConflictError('visual_comparison_changed', 'Группа иллюстраций уже изменилась.')
        if pending.get('live_images_delivered') is False and not session.id.startswith('headless:'):
            raise ConflictError('visual_images_not_delivered',
                                'Изображения не переданы модели; фоновая проверка продолжит работу.')
        if len(pending['candidates']) > 1:
            return self._record_grouped_comparison(session, command_id, args)
        status = args.get('status')
        if (status not in {'match', 'uncertain', 'mismatch'} or not isinstance(args.get('observations'), list)
                or any(not isinstance(item, str) or not item.strip() for item in args['observations'])
                or not isinstance(args.get('alternative_candidate_ids', []), list)):
            raise ConflictError('visual_comparison_invalid', 'Некорректный результат сравнения.')
        raw = {**{k: v for k, v in args.items() if not k.startswith('_')},
            '_references_sent': [c['candidate_id'] for c in pending['candidates']],
            '_reference_ids_sent': [c['reference_id'] for c in pending['candidates']]}
        if (args.get('provider_receipt') or {}).get('semantic_visual_contract') == 'observable_geometry_v1':
            raw['_observable_geometry_required'] = True
        # A valid but low-confidence verdict is a completed comparison, not a
        # failed job. The common gate below records it as uncertain and moves
        # the same durable queue to its next reference without accepting a POI.
        if args.get('reference_id') and args['reference_id'] not in raw['_reference_ids_sent']:
            raise ConflictError('visual_comparison_unproved', 'Эталон не относится к текущему сравнению.')
        if status == 'match' and (not raw['observations'] or args.get('candidate_id') not in raw['_references_sent']):
            raise ConflictError('visual_comparison_unproved', 'Совпадение должно опираться на показанные эталоны и отличительные детали.')
        with self.service.store.tx() as db:
            row = self.service._story_row(db, session.resource_id)
            research = json.loads(row['research_json'] or '{}')
            self._assert_visual_current(row, research, state, session=session)
            pair = next((item for item in state.get('parallel_pairs', [])
                         if item['id'] == pending['id'] and item.get('phase') == 'result'), None)
            accepted_before = (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
            conflict_before = row['error_code'] == 'visual_identity_conflict'
            if (int(research.get('identity_generation') or 0) != state['generation']
                    or row['photo_sha256'] != state['photo_sha256']
                    or ((row['state'] in PROTECTED or accepted_before or conflict_before) and pair is None)):
                raise ConflictError('visual_comparison_changed', 'Фото или подтверждение объекта изменилось.')
            from .identity_subject_binding import bind_reference_subject
            shortlist = (research.get('visual_identity') or {}).get('candidates', [])
            bound = bind_reference_subject(raw, pending['candidates'], shortlist, pending['evidence'])
            raw = bound['result']
            aliases = {r['normalized_value']: r['poi_id'] for r in db.execute(
                "SELECT normalized_value,poi_id FROM poi_aliases WHERE namespace='street_story_candidate'")}
            proved = visual_match(raw, pending['candidates'], shortlist, poi_aliases=aliases)
            from .identity_subject_binding import subject_aliases
            accepted_id = (research.get('visual_identity') or {}).get('candidate_id')
            pair_aliases = subject_aliases(shortlist, poi_aliases=aliases).get(raw.get('candidate_id'), {raw.get('candidate_id')})
            conflicting_peer = pair is not None and accepted_before and proved and accepted_id not in pair_aliases
            matched = not accepted_before and not conflict_before and proved
            if (not proved or conflicting_peer) and raw.get('status') == 'match':
                raw = {**raw, 'status': 'uncertain'}
            status = raw.get('status')
            verdict_summary = {'comparison_id': pending['id'], 'query': state.get('query'),
                'status': status, 'model_status': args.get('status'), 'matched': matched,
                'candidate_id': raw.get('candidate_id'), 'reference_candidate_id': args.get('candidate_id'),
                'reference_subject_candidate_id': raw.get('reference_subject_candidate_id'),
                'observations': [str(value)[:300] for value in raw.get('observations', [])[:6]],
                'reference_subject_observations': [str(value)[:300] for value in raw.get('reference_subject_observations', [])[:6]],
                'alternative_candidate_ids': raw.get('alternative_candidate_ids') or [],
                'binding_status': bound.get('status'), 'binding_reason': bound.get('reason'),
                'shared_distinctive_geometry': raw.get('shared_distinctive_geometry'),
                'observable_correspondences': raw.get('observable_correspondences'),
                'uncertain_reason': ('parallel_peer_physical_conflict' if conflicting_peer or conflict_before else
                    bound.get('reason') or ('common_visual_acceptance_not_proved'
                    if args.get('status') == 'match' and not proved else None)),
                'references': [{'candidate_id': candidate['candidate_id'], 'url': candidate.get('url'),
                    'image_urls': candidate.get('reference_image_urls') or []} for candidate in pending['candidates']],
                'confidence': confidence(raw), 'model': session.model, 'photo_sha256': state['photo_sha256'],
                'generation': state['generation'], 'control_revision': state.get('control_revision', 0)}
            state['verdict_history'] = [item for item in state.get('verdict_history', [])
                if item.get('comparison_id') != pending['id']] + [verdict_summary]
            feedback = args.get('search_feedback')
            if feedback is not None:
                from jsonschema import Draft202012Validator
                from .headless_identity import SEARCH_FEEDBACK_SCHEMA
                if Draft202012Validator(SEARCH_FEEDBACK_SCHEMA).is_valid(feedback):
                    # The model owns the interpretation and next query. Code
                    # validates addresses and retains per-reference evidence.
                    physical_ids = {c['candidate_id'] for c in shortlist}
                    valid_ids = [cid for cid in feedback['candidate_ids'] if cid in physical_ids]
                    saved_feedback = {**feedback, 'candidate_ids': valid_ids,
                        'reason': feedback['reason'][:500], 'next_query': feedback['next_query'].strip()[:240]}
                    verdict_summary['search_feedback'] = saved_feedback
                    evaluated = [raw.get('reference_subject_candidate_id')] if raw.get('reference_subject_candidate_id') in physical_ids else [
                        c['candidate_id'] for c in pending['candidates'] if c['candidate_id'] in physical_ids]
                    for candidate_id in evaluated:
                        hypothesis = state.setdefault('hypotheses', {}).setdefault(candidate_id,
                            {'candidate_id': candidate_id, 'comparisons': []})
                        hypothesis['comparisons'].append({'comparison_id': pending['id'],
                            'status': status, 'reference_ids': [e['reference_id'] for e in pending['evidence']],
                            'reference_kind': saved_feedback['reference_kind'],
                            'observations': verdict_summary['observations']})
                        hypothesis['next_action'] = saved_feedback
                    query = saved_feedback['next_query']
                    if query and not matched and not accepted_before and not conflict_before:
                        discovery = research.get('identity_article_discovery') or {}
                        planned = list(dict.fromkeys([query, *state.get('planned_queries', []),
                            *discovery.get('planned_queries', [])]))
                        state['planned_queries'] = planned
                        discovery.update(generation=state['generation'], photo_sha256=state['photo_sha256'],
                                         planned_queries=planned)
                        research['identity_article_discovery'] = discovery
                        # A model-directed missing view gets the next ordinary
                        # bounded search turn; original UNKNOWNs still fence it.
                        state['units_since_planned_query'] = max(2, state.get('units_since_planned_query', 0))
            if conflicting_peer:
                previous_identity = research['visual_identity']
                research['visual_identity'] = {**previous_identity, 'status': 'uncertain',
                    'visual_reference_verified': False, 'confidence': None,
                    'conflicting_comparison_ids': [previous_identity.get('comparison_id'), pending['id']],
                    'observations': ['Независимые сравнения подтвердили разные физические объекты; identity требует разрешения противоречия.']}
                db.execute("UPDATE stories SET state='needs_review',research_json=?,error_code='visual_identity_conflict',"
                           "error_message=?,revision=revision+1,updated_at=? WHERE id=?",
                           (canonical(research), 'Сравнения дали противоречащие определения объекта. Публикация приостановлена.',
                            self.service.store.now(), row['id']))
                verdict_summary['conflicts_with_comparison_id'] = previous_identity.get('comparison_id')
            if matched:
                selected = bound['candidate']
                evidence = bound.get('reference_evidence') or pending['evidence']
                identity = {**(research.get('visual_identity') or {}), 'status': 'match',
                    'candidate_id': selected['candidate_id'], 'candidate_name': selected['name'][:180],
                    'candidate_url': selected.get('url'), 'source_links': selected.get('source_urls') or [selected.get('url')],
                    'visual_reference_verified': True, 'confidence': confidence(raw),
                    'observations': [str(v)[:300] for v in raw['observations'][:6]],
                    'reference_evidence': [e for e in evidence if e.get('subject_candidate_id', e['candidate_id']) == raw['candidate_id']],
                    'reference_subject_binding': bound.get('binding'),
                    'photo_sha256': row['photo_sha256'], 'generation': state['generation'],
                    'comparison_model': session.model, 'comparison_id': pending['id'],
                'provider_receipt': args.get('provider_receipt'),
                    'resolved_at': self.service.store.now(), 'policy': 'live_article_media_v1',
                    'candidates': list({c['candidate_id']: c for c in [selected, *(research.get('visual_identity') or {}).get('candidates', [])]}.values())[:32]}
                research['visual_identity'] = identity
                from .poi_memory import ensure_poi_identity, hydrate_story_facts
                research['poi_id'] = ensure_poi_identity(db, identity, latitude=row['latitude'],
                    longitude=row['longitude'], now=self.service.store.now())
                research['poi_reused_fact_count'] = hydrate_story_facts(db, identity, row['id'])
                db.execute("UPDATE stories SET state='identity_ready',place_name=?,research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (identity['candidate_name'], canonical(research), self.service.store.now(), row['id']))
            result = {'matched': matched, 'continue_comparison': not matched and not accepted_before and not conflict_before,
                      'identity_conflict': conflicting_peer,
                      'instruction': 'Объект подтверждён.' if matched else 'Совпадения пока нет: вызови compare_place_images для следующей группы; широкий поиск выполняется автоматически.'}
            state['reviewed_reference_ids'] = list(dict.fromkeys([*state['reviewed_reference_ids'], *[e['reference_id'] for e in pending['evidence']]]))
            from .identity_progress import advance
            research['identity_progress'] = advance(research.get('identity_progress') or {}, 'identity_images_reviewed',
                {'generation': state['generation'], 'reference_ids': [e['reference_id'] for e in pending['evidence']]}, self.service.store.now())
            if pair is not None:
                # The accepted identity and every submitted sibling address commit
                # together. Later peer results only drain this frozen operation.
                pair.update(phase='completed', matched=matched, receipt=args.get('provider_receipt'))
                pair.pop('result', None)
            state['pending'] = None
            if (not session.state.get('visual_refill_preparing')
                    and not any(item.get('phase') not in {'completed', 'failed', 'skipped'}
                                for item in state.get('parallel_pairs', []))):
                state.update(lease_owner=None, lease_until=0)
            self._save_visual_queue(session, state, db=db, research=research)
            self._store_command(db, row['id'], command_id, 'record_place_comparison', args, result)
        record_identity_event(self.service, session.resource_id, 'identity_images_reviewed', {
            'generation': state['generation'], 'image_count': len(pending['evidence']),
            'reference_ids': [e['reference_id'] for e in pending['evidence']],
            'comparison_model': session.model, 'comparison_id': pending['id'],
            'status': status, 'matched': matched, 'conflicting_peer': conflicting_peer, 'confidence': confidence(raw)})
        state['pending'] = None
        if matched or accepted_before or conflict_before:
            record_identity_event(self.service, session.resource_id, 'identity_finished', {
                'generation': state['generation'], 'status': 'uncertain' if conflicting_peer or conflict_before else 'match',
                'reference_verified': not (conflicting_peer or conflict_before)})
        return {**result, 'story': self.service.story(session.resource_id)}

    def _record_grouped_comparison(self, session, command_id, args):
        """Apply addressed results through the pair gate; unseen items stay queued."""
        from jsonschema import Draft202012Validator
        from .headless_identity import grouped_verdict_schema
        state = session.state['visual_comparison']
        pending = state['pending']
        catalog = {c['reference_id']: (c, e) for c, e in zip(pending['candidates'], pending['evidence'])}
        item_schema = grouped_verdict_schema(pending['reply'])[1]
        if (args.get('provider_receipt') or {}).get('semantic_visual_contract') == 'observable_geometry_v1':
            item_schema['properties'].update(shared_distinctive_geometry={'type':'boolean'},
                observable_correspondences={'type':'array','items':{'type':'object'}})
            item_schema['required'] += ['shared_distinctive_geometry','observable_correspondences']
        validator = Draft202012Validator(item_schema)
        items, duplicates = {}, set()
        returned = args.get('reference_verdicts')
        if not isinstance(returned, list):
            returned = [args] if args.get('reference_id') else []
        for item in returned:
            if not isinstance(item, dict):
                continue
            ref = item.get('reference_id')
            if ref in items:
                duplicates.add(ref)
            elif ref in catalog and validator.is_valid(item) and item.get('candidate_id') in {'', catalog[ref][0]['candidate_id']}:
                items[ref] = item
        for ref in duplicates:
            items.pop(ref, None)
        # Different physical hypotheses in one result need an actual
        # clarification; processing order cannot choose the object's identity.
        _row, research = self.service._identity_snapshot(session.resource_id)
        from .identity_subject_binding import subject_aliases
        aliases = subject_aliases((research.get('visual_identity') or {}).get('candidates') or [])
        matches = {item.get('reference_subject_candidate_id') or item['candidate_id']
                   for item in items.values() if item['status'] == 'match'}
        ambiguous = bool(matches) and not matches.issubset(aliases.get(next(iter(matches)), matches if len(matches) == 1 else set()))
        unseen = [c for c in pending['candidates'] if c['reference_id'] not in items]
        # Keep all not-yet-committed frames durable between item commits. A
        # crash or early match cannot lose the rest of a completed batch.
        state['queue'] = pending['candidates'] + state['queue']
        result = {'matched': False, 'continue_comparison': True}
        for ref, item in items.items():
            candidate, evidence = catalog[ref]
            lease = self._visual_lease(session, expected=state)
            state.update(lease_owner=session.id, lease_until=lease['lease_until'])
            state['queue'] = [c for c in state['queue'] if c.get('reference_id') != ref]
            pair_id = pending['id'] + ':' + ref
            state['pending'] = {**pending, 'id': pair_id, 'candidates': [candidate], 'evidence': [evidence]}
            result = self._record_place_comparison(session, command_id + ':' + ref,
                {**item, **({'status':'uncertain'} if ambiguous and item['status'] == 'match' else {}),
                 'comparison_id': pair_id, 'provider_receipt': args.get('provider_receipt')})
            if result['matched']:
                return result
        lease = self._visual_lease(session, expected=state)
        state.update(lease_owner=session.id, lease_until=lease['lease_until'], pending=None)
        self._save_visual_queue(session, state)
        return {**result, 'unreviewed_reference_count': len(unseen), 'ambiguous_subjects': ambiguous,
                'story': self.service.story(session.resource_id)}
