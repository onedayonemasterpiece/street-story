"""Article images compared in multimodal tools by the current Live conversation.

No provider connection/key/transport is owned here. A labelled contact sheet binds
the source and references to one image; the Live tool records the model verdict.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid

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
    async def _find_place_articles(self, session, args):
        from .identity_discovery import web_image_sources
        query = str(args.get('query') or '').strip()[:180]
        if not query:
            raise ConflictError('visual_query_required', 'Нужен поисковый запрос.')
        attempted = session.state.setdefault('identity_search_queries', {})
        if query not in attempted:
            attempted[query] = await web_image_sources(self.service, query, '')
        sources = attempted[query]
        if sources:
            session.state['identity_article_sources'] = sources
        return {'sources': sources, 'search_unavailable': not sources,
                'instruction': 'Fetch these article illustrations with compare_place_images. Search titles are hypotheses only.' if sources else
                    'Search returned no sources. Do not repeat this helper or claim visual mismatches without images. Use native search if available; otherwise explain the current search availability gap.'}

    def _comparison_result(self, pending):
        return with_live_tool_parts({**pending['reply'], 'image': {'$ref': 'comparison.jpg'}},
            [{'inlineData': {'mimeType': 'image/jpeg', 'displayName': 'comparison.jpg',
                'data': base64.b64encode(pending['snapshot']).decode('ascii')}}])

    def _send_pending_comparison(self, session):
        pending = (session.state.get('visual_comparison') or {}).get('pending') or {}
        if pending.get('snapshot'):
            self.write(session, {'type': 'text', 'text':
                'A visual comparison is pending. Call compare_place_images to retrieve its SOURCE/REF image '
                'as a multimodal tool result before recording a verdict. Article titles are not visual evidence.'})

    async def _compare_place_images(self, session, args):
        story, research = self.service._identity_snapshot(session.resource_id)
        generation = int(research.get('identity_generation') or 0)
        identity = research.get('visual_identity') or {}
        if identity.get('status') in {'match', 'owner_confirmed'}:
            return {'already_resolved': True, 'visual_identity': identity}
        state = session.state.get('visual_comparison')
        if state and (state['generation'] != generation or state['photo_sha256'] != story['photo_sha256']):
            state = None
        if state and state.get('pending'):
            return self._comparison_result(state['pending'])
        if state is None:
            queue = []
            for candidate in identity.get('candidates', []):
                if candidate.get('reference_image_urls') and candidate.get('discovery') != 'web_article_media':
                    from .identity_references import original_reference
                    from .article_media import wikipedia_article_references
                    gallery = await wikipedia_article_references(candidate)
                    urls = list(dict.fromkeys(original_reference(url) or url for url in [*candidate['reference_image_urls'], *gallery]))
                    queue.extend({**candidate, 'reference_image_urls': [url], 'reference_batch': True}
                                 for url in urls)
            state = {'generation': generation, 'photo_sha256': story['photo_sha256'],
                     'queue': queue, 'web_searched': False, 'query': str(args.get('query') or identity.get('candidate_name') or '')[:180],
                     'seen_images': list((research.get('identity_progress') or {}).get('reviewed_image_sha256s') or []),
                     'browser_budget': {'remaining': 2}}
            session.state['visual_comparison'] = state
        if not state['queue'] and not state['web_searched']:
            from .article_media import article_candidates
            record_identity_event(self.service, story['id'], 'identity_web_media_started', {'generation': generation})
            state['query'] = str(args.get('query') or state['query'])[:180]
            if not state['query']:
                return {'instruction': 'Передай query — гипотезу о названии либо видимые отличительные признаки объекта. Это не подтверждение автора.'}
            saved_articles = [c for c in identity.get('candidates', []) if c.get('discovery') == 'web_article_media']
            urls = args.get('article_urls') or []
            if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
                raise ConflictError('visual_article_urls_invalid', 'Некорректные ссылки статей.')
            sources = [{'url': url} for url in urls[:20]] or session.state.get('identity_article_sources', [])
            if not sources and not saved_articles and not state.get('search_unavailable'):
                sources = (await self._find_place_articles(session, {'query': state['query']}))['sources']
            if not sources and not saved_articles:
                state['search_unavailable'] = True
                record_identity_event(self.service, story['id'], 'identity_web_search_unavailable', {'generation': generation})
                return {'search_unavailable': True, 'images_compared': 0,
                    'instruction': 'Поисковый helper не вернул статей. Это НЕ визуальное несовпадение: новых эталонов не было. Используй свой native Google Search, затем передай реальные URL статей в compare_place_images.article_urls. Не повторяй helper без новых источников и не утверждай, что фотографии просмотрены.'}
            articles = saved_articles or await article_candidates(self.service,
                {**story, '_identity_generation': generation}, sources, set(research.get('identity_rejected_ids') or []))
            state['web_searched'] = True
            for candidate in articles:
                state['queue'].extend({**candidate, 'reference_image_urls': [url], 'reference_batch': True}
                                     for url in candidate['reference_image_urls'])
        references, evidence, candidates = [], [], []
        # One reference beside the source keeps detail readable and the current
        # Live context bounded. Every completed image advances the UI counter.
        while state['queue'] and len(references) < 1:
            candidate = state['queue'].pop(0)
            receipts = []
            images = await self.service._candidate_reference_images([{**candidate, '_browser_budget': state['browser_budget']}],
                limit=1, story_id=story['id'], evidence=receipts)
            if not images or not receipts or receipts[0]['model_image_sha256'] in state['seen_images']:
                continue
            state['seen_images'].append(receipts[0]['model_image_sha256'])
            references.extend(images)
            evidence.extend(receipts)
            candidates.append(candidate)
        if not references:
            if not state['web_searched']:
                return await self._compare_place_images(session, args)
            record_identity_event(self.service, story['id'], 'identity_finished', {
                'generation': generation, 'status': 'uncertain', 'candidate_id': identity.get('candidate_id'),
                'reference_verified': False})
            return {'exhausted': True, 'instruction': 'Доступные иллюстрации проверены; объясни конкретный пробел без выдуманного подтверждения.'}
        sheet = comparison_sheet(story['photo_path'], references)
        comparison_id = 'comparison_' + uuid.uuid4().hex
        reply = {'comparison_id': comparison_id, 'snapshot_kind': 'source_and_references',
            'references': [{'label': f'REF {i}', 'candidate_id': c['candidate_id'], 'name': c['name'],
                }
                for i, (c, e) in enumerate(zip(candidates, evidence), 1)],
            'remaining_illustrations': len(state['queue']),
            'instruction': 'Сравни SOURCE и REF по отличительным деталям; запиши вердикт через record_place_comparison. Нет match — продолжай compare_place_images. Реклама и другие объекты не доказательство.'}
        state['pending'] = {'id': comparison_id, 'candidates': candidates, 'evidence': evidence, 'reply': reply, 'snapshot': sheet}
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
        raw = {**args, '_references_sent': [c['candidate_id'] for c in pending['candidates']]}
        matched = visual_match(raw, pending['candidates'])
        if status == 'match' and not matched:
            raise ConflictError('visual_comparison_unproved', 'Совпадение должно опираться на показанные эталоны и отличительные детали.')
        with self.service.store.tx() as db:
            row = self.service._story_row(db, session.resource_id)
            research = json.loads(row['research_json'] or '{}')
            if (int(research.get('identity_generation') or 0) != state['generation']
                    or row['photo_sha256'] != state['photo_sha256'] or row['state'] in PROTECTED
                    or (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}):
                raise ConflictError('visual_comparison_changed', 'Фото или подтверждение объекта изменилось.')
            if matched:
                selected = next(c for c in pending['candidates'] if c['candidate_id'] == raw['candidate_id'])
                identity = {**(research.get('visual_identity') or {}), 'status': 'match',
                    'candidate_id': selected['candidate_id'], 'candidate_name': str(args.get('object_name') or selected['name'])[:180],
                    'candidate_url': selected.get('url'), 'source_links': selected.get('source_urls') or [selected.get('url')],
                    'visual_reference_verified': True, 'confidence': confidence(raw),
                    'observations': [str(v)[:300] for v in raw['observations'][:6]],
                    'reference_evidence': [e for e in pending['evidence'] if e['candidate_id'] == raw['candidate_id']],
                    'photo_sha256': row['photo_sha256'], 'generation': state['generation'],
                    'comparison_model': session.model, 'comparison_id': pending['id'],
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
