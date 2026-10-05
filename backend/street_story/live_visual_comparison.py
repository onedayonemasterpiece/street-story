"""Article images compared in multimodal tools by the current Live conversation.

No provider connection/key/transport is owned here. A labelled contact sheet binds
the source and references to one image; the Live tool records the model verdict.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time

from PIL import Image, ImageDraw, ImageOps
from live_interaction import with_live_tool_parts

from .identity_lifecycle import PROTECTED, confidence, visual_match
from .identity_telemetry import record_identity_event
from .service import ConflictError, canonical


def comparison_sheet(photo_path, references):
    """Source remains visible beside every reference; no image is cropped."""
    cells = [('SOURCE', Image.open(photo_path))]
    cells += [(f'REF {index}', Image.open(io.BytesIO(data)))
              for index, (_cid, _mime, data) in enumerate(references, 1)]
    canvas = Image.new('RGB', (1280, 640 * ((len(cells) + 1) // 2)), 'white')
    draw = ImageDraw.Draw(canvas)
    for index, (label, opened) in enumerate(cells):
        with opened:
            image = ImageOps.exif_transpose(opened).convert('RGB')
            image.thumbnail((632, 610), Image.Resampling.LANCZOS)
            x, y = (index % 2) * 640, (index // 2) * 640
            draw.text((x + 8, y + 4), label, fill='black', font_size=20)
            canvas.paste(image, (x + (640 - image.width) // 2, y + 28 + (610 - image.height) // 2))
    output = io.BytesIO()
    for quality in (82, 70, 58, 46):
        output.seek(0)
        output.truncate()
        canvas.save(output, format='JPEG', quality=quality, optimize=True)
        if output.tell() <= 480 * 1024:
            return output.getvalue()
    raise ValueError('comparison_sheet_size')


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
        """Persist cursors/receipts, never multimedia or session credentials.

        An unacknowledged comparison is offered again after reconnect. Only a
        completed verdict enters seen_images, so download/replay never counts.
        """
        durable = {k: v for k, v in state.items() if k != 'pending'}
        pending = state.get('pending') or {}
        durable['queue'] = [*pending.get('candidates', []), *state['queue']]
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
        for url in candidate.get('reference_image_urls', []):
            yield {**candidate, 'reference_image_urls': [url], 'reference_batch': True,
                   'article_media': [m for m in candidate.get('article_media', []) if m['image_url'] == url]}

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
        if previous.get('status') == 'completed':
            return previous
        if previous.get('retry_at', 0) > now:
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
                '_identity_query_context': query_context})
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
                'photo_sha256': row['photo_sha256'], 'queries': {**history.get('queries', {}), **attempted},
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
        continuation = next_visual_query(identity, state.get('query_seed') or state.get('query'), searches)
        available = bool(unread_candidates) or bool(unread_sources) or bool(state.get('queue')) or any(p['status'] == 'pending' or
            (p['status'] in {'partial', 'temporary_failure'} and p.get('retry_at', 0) <= now)
            for p in pages.values())
        if not available and continuation:
            available = searches.get(continuation, {}).get('retry_at', 0) <= now
        if not available:
            return
        token = canonical([len(state.get('seen_images', [])), len(state.get('queue', [])),
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
            'Do not repeat search while usable saved references remain; stop on proved match.'})

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

    def _comparison_result(self, pending):
        return with_live_tool_parts({**pending['reply'], 'image': {'$ref': 'comparison.jpg'}},
            [{'inlineData': {'mimeType': 'image/jpeg', 'displayName': 'comparison.jpg',
                'data': base64.b64encode(pending['snapshot']).decode('ascii')}}])

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
        if pending.get('snapshot'):
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

    async def _reconcile_completed_reference(self, session, story, research, identity, state):
        """Repair host provenance for an observed pair, never rerun its model.

        A completed native result rejected only for missing article provenance
        is reusable only when the original semantic unit can be reconstructed.
        Previously seen pixels remain seen, including when proof is unavailable.
        """
        from pathlib import Path
        from urllib.parse import urlsplit
        from .article_media import extract_media, public_url
        from .headless_identity import VERDICT_SCHEMA
        from .identity_references import original_reference
        from .research_adapter import semantic_visual_context
        from .errors import RetryableProviderError

        history = [entry for entry in state.get('verdict_history', [])
            if entry.get('model_status') == 'match' and entry.get('binding_reason') == 'reference_provenance_missing'
            and entry.get('photo_sha256') == story['photo_sha256']
            and entry.get('generation') == state['generation']
            and entry.get('control_revision', 0) == state['control_revision']]
        if not history:
            return None
        previous = history[-1]
        marker = state.get('completed_reconciliation') or {}
        now = self.service.store.now()
        if marker.get('comparison_id') == previous['comparison_id'] and marker.get('retry_at', 0) > now:
            raise RetryableProviderError('identity_completed_reference_waiting', retry_at=marker['retry_at'])

        def waiting(reason):
            state['completed_reconciliation'] = {'comparison_id': previous['comparison_id'],
                'status': 'waiting', 'reason': reason, 'retry_at': now + 300}
            self._save_visual_queue(session, state)
            record_identity_event(self.service, story['id'], 'identity_completed_reference_reconciliation', {
                'component': 'visual_queue', 'stage': 'provenance_reconciliation',
                'comparison_id': previous['comparison_id'], 'generation': state['generation'],
                'status': 'waiting', 'reason': reason})
            raise RetryableProviderError('identity_completed_reference_' + reason, retry_at=now + 300)

        with self.service.store.connection() as db:
            attempts = list(db.execute("SELECT * FROM research_provider_attempts WHERE story_id=? AND role='vision_native' ORDER BY created_at DESC",
                                       (story['id'],)))
        completed = []
        for attempt in attempts:
            receipt = json.loads(attempt['receipt_json'])
            binding, result = receipt.get('binding') or {}, receipt.get('result') or {}
            if (receipt.get('phase') == 'completed' and receipt.get('provider') == 'codex_native'
                    and receipt.get('transport') == 'native_codex_app_server'
                    and receipt.get('photo_sha256') == story['photo_sha256']
                    and receipt.get('generation') == state['generation']
                    and receipt.get('profile_verified') is True and receipt.get('thread_id') and receipt.get('turn_id')
                    and binding.get('story_id') == story['id'] and binding.get('photo_sha256') == story['photo_sha256']
                    and binding.get('generation') == state['generation']
                    and binding.get('control_revision', 0) == state['control_revision']
                    and binding.get('attempt_id') == attempt['attempt_id']
                    and result.get('status') == 'match'
                    and result.get('candidate_id') == previous.get('reference_candidate_id')
                    and result.get('reference_subject_candidate_id') == previous.get('reference_subject_candidate_id')):
                completed.append((attempt, receipt))
        if not completed:
            waiting('completed_receipt_missing')
        sources = [entry for entry in previous.get('references', [])
            if entry.get('candidate_id') == previous.get('reference_candidate_id')]
        if len(sources) != 1 or len(sources[0].get('image_urls') or []) != 1:
            waiting('original_reference_missing')
        source = sources[0]
        article_url = public_url(source.get('url') or '')
        original = source['image_urls'][0]
        if (not article_url or not (urlsplit(article_url).hostname or '').endswith('.wikipedia.org')
                or original_reference(original) != original
                or source['candidate_id'] != 'web:' + hashlib.sha256(article_url.encode()).hexdigest()[:16]):
            waiting('article_extraction_unproved')
        saved = self.service.store.cache_get('public-article-acquisition-v1:' + hashlib.sha256(article_url.encode()).hexdigest())
        try:
            body = base64.b64decode(saved['body'], validate=True)
            if saved['final_url'] != article_url or hashlib.sha256(body).hexdigest() != saved['sha256']:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            waiting('immutable_article_missing')
        title, media = extract_media(body, article_url)
        media = [{**item, 'image_url': original} for item in media if original_reference(item['image_url']) == original]
        if not media:
            waiting('article_extraction_unproved')
        candidate = {'candidate_id': source['candidate_id'], 'name': title, 'url': article_url,
            'source_urls': [article_url], 'reference_image_urls': [original], 'article_media': media,
            'discovery': 'wikipedia_article_media', 'reference_batch': True}
        evidence = []
        try:
            references = await self.service._candidate_reference_images([candidate], limit=1,
                story_id=story['id'], evidence=evidence)
        except (ValueError, OSError) as exc:
            waiting('reference_' + type(exc).__name__)
        if (len(references) != 1 or len(evidence) != 1
                or evidence[0].get('article_source_sha256') != saved['sha256']
                or evidence[0].get('model_image_sha256') != hashlib.sha256(references[0][2]).hexdigest()
                or references[0][0] != candidate['candidate_id']):
            waiting('immutable_reference_missing')
        try:
            actual_photo_sha = hashlib.sha256(Path(story['photo_path']).read_bytes()).hexdigest()
        except OSError:
            waiting('source_pixels_missing')
        if actual_photo_sha != story['photo_sha256']:
            waiting('source_pixels_changed')
        sheet = comparison_sheet(story['photo_path'], references)
        comparison_id = 'comparison_' + hashlib.sha256(canonical([story['photo_sha256'], state['generation'],
            state['control_revision'], [evidence[0]['model_image_sha256']]]).encode()).hexdigest()[:32]
        if comparison_id != previous['comparison_id']:
            waiting('reference_pixels_changed')
        reply = self._visual_reply(comparison_id, [candidate], identity, len(state['queue']))
        unit = hashlib.sha256(sheet).hexdigest() + hashlib.sha256(
            (semantic_visual_context(canonical(reply)) + canonical(VERDICT_SCHEMA)).encode()).hexdigest()
        logical = hashlib.sha256(canonical([story['id'], story['photo_sha256'], state['generation'], 'vision_native', unit]).encode()).hexdigest()
        selected = next(((attempt, receipt) for attempt, receipt in completed
            if attempt['logical_id'] == logical and (receipt.get('binding') or {}).get('request_id') == logical
            and receipt.get('model_image_sha256') == hashlib.sha256(sheet).hexdigest()), None)
        if selected is None:
            waiting('semantic_unit_changed')
        attempt, receipt = selected
        import re
        if not re.fullmatch(r'rattempt_[0-9a-f]{24}', attempt['attempt_id']):
            waiting('input_receipt_unproved')
        original_input = self.service.settings.data_dir / 'stories' / story['id'] / 'native-comparisons' / (attempt['attempt_id'] + '.jpg')
        try:
            if hashlib.sha256(original_input.read_bytes()).hexdigest() != receipt['model_image_sha256']:
                raise ValueError
        except (OSError, ValueError):
            waiting('immutable_input_missing')
        from jsonschema import ValidationError, validate
        try:
            validate(receipt['result'], VERDICT_SCHEMA)
        except ValidationError:
            waiting('completed_result_invalid')
        state['pending'] = {'id': comparison_id, 'candidates': [candidate], 'evidence': evidence,
            'reply': reply, 'snapshot': sheet}
        state['completed_reconciliation'] = {'comparison_id': comparison_id, 'attempt_id': attempt['attempt_id'],
            'status': 'reconciled', 'reason': 'reference_provenance_missing'}
        self._save_visual_queue(session, state)
        session.model = receipt.get('model') or session.model
        result = self._record_place_comparison(session, 'reconcile:' + comparison_id,
            {**receipt['result'], 'comparison_id': comparison_id, 'provider_receipt': receipt})
        record_identity_event(self.service, story['id'], 'identity_completed_reference_reconciliation', {
            'component': 'visual_queue', 'stage': 'provenance_reconciliation',
            'comparison_id': comparison_id, 'attempt_id': attempt['attempt_id'], 'generation': state['generation'],
            'status': 'reconciled', 'reason': 'reference_provenance_missing', 'matched': result['matched']})
        return {**result, 'reconciled_completed': True}

    @staticmethod
    def _visual_reply(comparison_id, candidates, identity, remaining):
        return {'comparison_id': comparison_id, 'snapshot_kind': 'source_and_references',
            'references': [{'label': f'REF {i}', 'candidate_id': c['candidate_id'], 'name': c['name']}
                for i, c in enumerate(candidates, 1)],
            'physical_candidates': [{'candidate_id': c['candidate_id'], 'name': c.get('name', ''),
                'url': c.get('url'), 'alias_candidate_ids': c.get('alias_candidate_ids', [])}
                for c in identity.get('candidates', []) if not str(c.get('candidate_id', '')).startswith('web:')][:32],
            'remaining_illustrations': remaining,
            'instruction': 'Сравни SOURCE и REF по отличительным деталям; запиши вердикт через record_place_comparison. Для web REF candidate_id — показанный REF; reference_subject_candidate_id — доказанный физический кандидат из physical_candidates. Проверяй альтернативы всего shortlist. Название статьи, реклама и другие объекты не доказательство.'}

    async def _compare_place_images(self, session, args, *, page_budget=4, expected_scope=None, search_budget=1):
        story, research = self.service._identity_snapshot(session.resource_id)
        generation = int(research.get('identity_generation') or 0)
        identity = research.get('visual_identity') or {}
        if identity.get('status') in {'match', 'owner_confirmed'}:
            return {'already_resolved': True, 'visual_identity': identity}
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
            queue = []
            for candidate in identity.get('candidates', []):
                if candidate.get('reference_image_urls') and candidate.get('discovery') != 'web_article_media':
                    from .identity_references import original_reference
                    urls = list(dict.fromkeys(original_reference(url) or url for url in candidate['reference_image_urls']))
                    queue.extend(self._image_entries({**candidate, 'reference_image_urls': urls}))
            state = {'generation': generation, 'photo_sha256': story['photo_sha256'],
                     'control_revision': expected['control_revision'],
                     'queue': queue, 'web_searched': False, 'query': query_hint,
                     'query_seed': query_hint,
                     'seen_images': list((research.get('identity_progress') or {}).get('reviewed_image_sha256s') or [])
                         if (research.get('identity_progress') or {}).get('generation', generation) == generation else [],
                     'browser_budget': {'remaining': 2}, 'sources': {}, 'searches': {}, 'fetch_failures': []}
            session.state['visual_comparison'] = state
        state.update(lease_owner=session.id, lease_until=lease['lease_until'],
                     control_revision=expected['control_revision'])
        session.state['visual_comparison'] = state
        progress = research.get('identity_progress') or {}
        if progress.get('generation', generation) == generation:
            # Background comparisons may finish after the first Live tool call.
            state['seen_images'] = list(dict.fromkeys([*state['seen_images'],
                *(progress.get('reviewed_image_sha256s') or [])]))
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
            url = str(candidate.get('url') or '')
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
        if state.get('pending'):
            # A pending verdict must not swallow URLs supplied by a later tool.
            self._save_visual_queue(session, state)
            return self._comparison_result(state['pending'])
        reconciled = await self._reconcile_completed_reference(session, story, research, identity, state)
        if reconciled is not None:
            return reconciled
        read_pages = 0
        searches_performed = 0
        if not state['queue']:
            from .article_media import article_candidates
            query = str(args.get('query') or state['query'] or query_hint)[:180]
            if not query:
                from .identity_discovery import next_visual_query
                query = next_visual_query(identity, '', state['searches'])
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
                            candidate.get('discovery_provenance', {}).items() if key in {'gallery_cursor', 'gallery_slide_cursor'}}}, 'attempts': 1}
            available = any(p['status'] in {'pending', 'partial', 'temporary_failure'}
                            for p in state['sources'].values())
            previous = state['searches'].get(query) or {}
            if not available and not state['queue'] and previous.get('status') == 'completed':
                from .identity_discovery import next_visual_query
                following = next_visual_query(identity, state['query_seed'], state['searches'])
                if following:
                    query = state['query'] = following
                    previous = state['searches'].get(query) or {}
            if not available and not state['queue'] and query and previous.get('status') != 'completed' and search_budget > 0:
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
            for page in sorted(state['sources'].values(), key=lambda item: item.get('attempts', 0)):
                if state['queue'] or read_pages >= page_budget:
                    break
                if page['status'] in {'completed', 'excluded'} or page.get('retry_at', 0) > self.service.store.now():
                    continue
                read_pages += 1
                receipts = []
                articles = await article_candidates(self.service, {**story, '_identity_generation': generation},
                    [page['source']], set(research.get('identity_rejected_ids') or []), receipts=receipts)
                page['attempts'] += 1
                page['status'] = receipts[0]['status'] if receipts else 'temporary_failure'
                if receipts:
                    page['source']['gallery_cursor'] = receipts[0].get('gallery_cursor', 0)
                    page['source']['gallery_slide_cursor'] = receipts[0].get('gallery_slide_cursor', 0)
                page['retry_at'] = self.service.store.now() + 15 if page['status'] != 'completed' else 0
                for candidate in articles:
                    state['queue'].extend(self._image_entries(candidate))
            state['web_searched'] = state['searches'].get(query, {}).get('status') == 'completed'
        self._save_visual_queue(session, state)
        references, evidence, candidates = [], [], []
        # One reference beside the source keeps detail readable and the current
        # Live context bounded. Every completed image advances the UI counter.
        fetch_attempts = 0
        while state['queue'] and len(references) < 1 and fetch_attempts < 3:
            candidate = state['queue'].pop(0)
            fetch_attempts += 1
            receipts = []
            images = await self.service._candidate_reference_images([{**candidate, '_browser_budget': state['browser_budget']}],
                limit=1, story_id=story['id'], evidence=receipts)
            if not images or not receipts:
                candidate['_fetch_attempts'] = candidate.get('_fetch_attempts', 0) + 1
                if candidate['_fetch_attempts'] < 2:
                    state['fetch_failures'].append(candidate)
                continue
            if receipts[0]['model_image_sha256'] in state['seen_images']:
                continue
            references.extend(images)
            evidence.extend(receipts)
            candidates.append(candidate)
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
                    expected_scope=expected_scope, search_budget=search_budget-searches_performed)
            if not state['web_searched'] and state['query'] and not partial and not unavailable:
                if search_budget > searches_performed:
                    return await self._compare_place_images(session, args, page_budget=remaining_pages,
                        expected_scope=expected_scope, search_budget=search_budget-searches_performed)
                return {'partial': True, 'next_query': state['query'], 'autonomous_continuation': True,
                    'instruction': 'The next saved visual hypothesis awaits search admission; continue this same operation.'}
            if partial or unavailable:
                return {'partial': True, 'search_unavailable': unavailable, 'images_compared': 0,
                    'instruction': 'Queue retained. Some articles/images are unavailable or partial; continue later or supply new API-found article URLs/query. This is not exhausted or mismatch.'}
            from .identity_discovery import next_visual_query
            following = next_visual_query(identity, state.get('query_seed'), state['searches'])
            if following:
                state.update(query=following, web_searched=False)
                self._save_visual_queue(session, state)
                return {'partial': True, 'next_query': following, 'autonomous_continuation': True,
                    'instruction': 'Current query completed; continue with the saved alternative/view query in this same operation. Identity remains unproved.'}
            record_identity_event(self.service, story['id'], 'identity_finished', {
                'generation': generation, 'status': 'uncertain', 'candidate_id': identity.get('candidate_id'), 'reference_verified': False})
            return {'exhausted': True, 'instruction': 'Available illustrations reviewed for this query. A new query or article URLs can continue the same queue.'}
        sheet = comparison_sheet(story['photo_path'], references)
        comparison_id = 'comparison_' + hashlib.sha256(canonical([story['photo_sha256'], generation,
            state['control_revision'], [e['model_image_sha256'] for e in evidence]]).encode()).hexdigest()[:32]
        reply = self._visual_reply(comparison_id, candidates, identity, len(state['queue']))
        state['pending'] = {'id': comparison_id, 'candidates': candidates, 'evidence': evidence, 'reply': reply, 'snapshot': sheet}
        self._save_visual_queue(session, state)
        record_identity_event(self.service, story['id'], 'identity_live_comparison_sent', {
            'generation': generation, 'image_count': len(references), 'sheet_sha256': hashlib.sha256(sheet).hexdigest(),
            'comparison_id': comparison_id, 'model': session.model})
        return self._comparison_result(state['pending'])

    def _record_place_comparison(self, session, command_id, args):
        state = session.state.get('visual_comparison') or {}
        pending = state.get('pending') or {}
        if not pending or args.get('comparison_id') != pending['id']:
            raise ConflictError('visual_comparison_changed', 'Группа иллюстраций уже изменилась.')
        status = args.get('status')
        if (status not in {'match', 'uncertain', 'mismatch'} or not isinstance(args.get('observations'), list)
                or any(not isinstance(item, str) or not item.strip() for item in args['observations'])
                or not isinstance(args.get('alternative_candidate_ids', []), list)):
            raise ConflictError('visual_comparison_invalid', 'Некорректный результат сравнения.')
        raw = {**{k: v for k, v in args.items() if not k.startswith('_')},
            '_references_sent': [c['candidate_id'] for c in pending['candidates']]}
        if status == 'match' and (confidence(raw) < .90 or not raw['observations'] or args.get('candidate_id') not in raw['_references_sent']):
            raise ConflictError('visual_comparison_unproved', 'Совпадение должно опираться на показанные эталоны и отличительные детали.')
        with self.service.store.tx() as db:
            row = self.service._story_row(db, session.resource_id)
            research = json.loads(row['research_json'] or '{}')
            self._assert_visual_current(row, research, state, session=session)
            if (int(research.get('identity_generation') or 0) != state['generation']
                    or row['photo_sha256'] != state['photo_sha256'] or row['state'] in PROTECTED
                    or (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}):
                raise ConflictError('visual_comparison_changed', 'Фото или подтверждение объекта изменилось.')
            from .identity_subject_binding import bind_reference_subject
            shortlist = (research.get('visual_identity') or {}).get('candidates', [])
            bound = bind_reference_subject(raw, pending['candidates'], shortlist, pending['evidence'])
            raw = bound['result']
            aliases = {r['normalized_value']: r['poi_id'] for r in db.execute(
                "SELECT normalized_value,poi_id FROM poi_aliases WHERE namespace='street_story_candidate'")}
            matched = visual_match(raw, pending['candidates'], shortlist, poi_aliases=aliases)
            if not matched and raw.get('status') == 'match':
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
                'uncertain_reason': bound.get('reason') or ('common_visual_acceptance_not_proved'
                    if args.get('status') == 'match' and not matched else None),
                'references': [{'candidate_id': candidate['candidate_id'], 'url': candidate.get('url'),
                    'image_urls': candidate.get('reference_image_urls') or []} for candidate in pending['candidates']],
                'confidence': confidence(raw), 'model': session.model, 'photo_sha256': state['photo_sha256'],
                'generation': state['generation'], 'control_revision': state.get('control_revision', 0)}
            state['verdict_history'] = [item for item in state.get('verdict_history', [])
                if item.get('comparison_id') != pending['id']] + [verdict_summary]
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
            result = {'matched': matched, 'continue_comparison': not matched,
                      'instruction': 'Объект подтверждён.' if matched else 'Совпадения пока нет: вызови compare_place_images для следующей группы; широкий поиск выполняется автоматически.'}
            state['seen_images'] = list(dict.fromkeys([*state['seen_images'], *[e['model_image_sha256'] for e in pending['evidence']]]))
            from .identity_progress import advance
            research['identity_progress'] = advance(research.get('identity_progress') or {}, 'identity_images_reviewed',
                {'generation': state['generation'], 'image_sha256s': [e['model_image_sha256'] for e in pending['evidence']]}, self.service.store.now())
            state['pending'] = None
            state.update(lease_owner=None, lease_until=0)
            self._save_visual_queue(session, state, db=db, research=research)
            self._store_command(db, row['id'], command_id, 'record_place_comparison', args, result)
        record_identity_event(self.service, session.resource_id, 'identity_images_reviewed', {
            'generation': state['generation'], 'image_count': len(pending['evidence']),
            'image_sha256s': [e['model_image_sha256'] for e in pending['evidence']],
            'comparison_model': session.model, 'comparison_id': pending['id'],
            'status': status, 'matched': matched, 'confidence': confidence(raw)})
        state['pending'] = None
        if matched:
            record_identity_event(self.service, session.resource_id, 'identity_finished', {
                'generation': state['generation'], 'status': 'match', 'reference_verified': True})
        return {**result, 'story': self.service.story(session.resource_id)}
