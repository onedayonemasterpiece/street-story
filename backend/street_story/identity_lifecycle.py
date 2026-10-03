"""Persistent, generation-bound photo identity shared by jobs and conversational tools."""
from __future__ import annotations

import asyncio
import json
import math
import time
import weakref
from pathlib import Path
from typing import Any

from .identity_telemetry import record_identity_event
from .identity_candidate_policy import candidate_identity_eligible
from .camera_hints import read_camera_hints, metadata_summary, annotate_camera_alignment
from .photo_metadata import inspect_gps, pixel_digest
from .service import ConflictError, canonical, digest

ACCEPTED = {'match', 'owner_confirmed'}
PROTECTED = {'scheduling', 'scheduled', 'published'}
POLICY = 'nearest_visual_batches_discovery_v3'


def distance(candidate: dict[str, Any]) -> float:
    try:
        value = float(candidate.get('distance_m'))
        return value if math.isfinite(value) and value >= 0 else float('inf')
    except (ValueError, TypeError):
        return float('inf')


def confidence(result: dict[str, Any]) -> float:
    try:
        value = float(result.get('confidence') or 0)
        return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0
    except (ValueError, TypeError):
        return 0.0


def visual_match(result: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    by_id = {item.get('candidate_id'): item for item in candidates}
    ids = set(by_id)
    selected = result.get('candidate_id')
    selected_candidate = by_id.get(selected) or {}
    return (result.get('status') == 'match' and selected in ids
            and candidate_identity_eligible(selected_candidate)
            and confidence(result) >= 0.90
            and selected in result.get('_references_sent', [])
            and bool(result.get('observations'))
            and not [item for item in result.get('alternative_candidate_ids', []) if item in ids and item != selected])


class IdentityLifecycleMixin:
    def _identity_snapshot(self, story_id):
        with self.store.connection() as db:
            story = dict(self._story_row(db, story_id))
        return story, json.loads(story.get('research_json') or '{}')

    def ensure_identity(self, story_id: str) -> dict[str, Any]:
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            research = json.loads(row['research_json'] or '{}')
            identity = research.get('visual_identity') or {}
            generation = int(research.get('identity_generation') or 0)
            if identity.get('status') in ACCEPTED or row['state'] in PROTECTED:
                return self._story_repr(db, row)
            # A finished uncertain result is not a reason to resubmit on every
            # foreground refresh. Recovery or explicit correction creates a generation.
            if research.get('identity_attempted_generation') == generation:
                return self._story_repr(db, row)
            semantic = 'identity:' + digest({'story_id': story_id, 'photo_sha256': row['photo_sha256'],
                                            'generation': generation, 'lat': row['latitude'], 'lon': row['longitude']})
            job_id = self._enqueue_job(db, story_id, 'identity', semantic, {'identity_generation': generation})
            job = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if job['state'] in {'ready', 'running', 'retry'} and row['state'] in {'photo_ready', 'voice_ready', 'needs_review'}:
                db.execute("UPDATE stories SET state='identifying',error_code=NULL,error_message=NULL,"
                           'revision=revision+1,updated_at=? WHERE id=?', (self.store.now(), story_id))
            result = self._story_repr(db, self._story_row(db, story_id))
        record_identity_event(self, story_id, 'identity_requested', {'generation': generation, 'job_id': job_id})
        return result

    async def _run_identity(self, job: dict[str, Any]) -> None:
        generation = json.loads(job.get('payload_json') or '{}').get('identity_generation', 0)
        await self.resolve_identity(job['story_id'], expected_generation=generation, job_id=job['id'])

    async def resolve_identity(self, story_id: str, transcript: str = '', *, expected_generation=None, job_id=None):
        if not hasattr(self, '_identity_locks'):
            self._identity_locks = weakref.WeakValueDictionary()
        lock = self._identity_locks.setdefault(story_id, asyncio.Lock())
        async with lock:
            story, prior = self._identity_snapshot(story_id)
            generation = int(prior.get('identity_generation') or 0)
            previous = prior.get('visual_identity') or {}
            if expected_generation is not None and expected_generation != generation:
                record_identity_event(self, story_id, 'identity_stale_job', {'generation': generation, 'job_generation': expected_generation})
                return self.story(story_id)
            if previous.get('status') in ACCEPTED or prior.get('identity_attempted_generation') == generation:
                record_identity_event(self, story_id, 'identity_reused', {'generation': generation, 'status': previous.get('status')})
                return self.story(story_id)
            started = time.monotonic()
            record_identity_event(self, story_id, 'identity_started', {'generation': generation, 'job_id': job_id, 'policy': POLICY})
            lat, lon = story.get('latitude'), story.get('longitude')
            metadata = inspect_gps(Path(story['photo_path']))
            binding = prior.get('photo_camera_hints') or {}
            recovered_hints = (binding.get('photo_sha256') == story['photo_sha256']
                and binding.get('source') == 'selected_original_exif'
                and (prior.get('location_provenance') or {}).get('kind') == 'selected_original_exif'
                and (prior.get('location_provenance') or {}).get('same_pixels_verified') is True)
            hints = binding['metadata'] if recovered_hints else read_camera_hints(Path(story['photo_path']))
            if not recovered_hints:
                binding = {'photo_sha256': story['photo_sha256'], 'source': 'source_photo_exif', 'metadata': hints}
            story['_camera_hints'] = hints
            story['_identity_generation'] = generation
            if lat is None or lon is None:
                lat, lon = metadata['latitude'], metadata['longitude']
            valid = lat is not None and lon is not None and math.isfinite(float(lat)) and math.isfinite(float(lon)) and -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
            record_identity_event(self, story_id, 'identity_location', {
                'generation': generation, 'client_coordinates_present': story.get('latitude') is not None and story.get('longitude') is not None,
                'photo_gps_status': metadata['status'], 'coordinates_usable': bool(valid),
                'source': 'client_photo_metadata' if story.get('latitude') is not None else 'server_photo_exif',
            })
            position_verified = bool(valid and (recovered_hints or (
                metadata['status'] == 'gps_present'
                and abs(float(lat) - metadata['latitude']) <= 0.00001
                and abs(float(lon) - metadata['longitude']) <= 0.00001)))
            record_identity_event(self, story_id, 'identity_camera_metadata', {
                'generation': generation, 'position_verified': position_verified, **metadata_summary(hints)})
            osm, wikipedia, candidates = {}, [], []
            if not valid:
                raw = {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                       'observations': ['В доступной приложению копии фото нет читаемых геометок. Выберите оригинал с разрешением на метаданные.']}
                error = 'identity_location_missing'
            else:
                try:
                    osm_error = None
                    osm = prior.get('osm')
                    if not isinstance(osm, dict) or not osm:
                        try:
                            osm = await self.providers.osm.lookup(float(lat), float(lon))
                        except Exception as exc:
                            from .providers import RetryableProviderError
                            if not isinstance(exc, RetryableProviderError):
                                raise
                            osm_error = exc
                            osm = {}
                            record_identity_event(self, story_id, 'identity_osm_unavailable', {'generation': generation, 'error_type': type(exc).__name__})
                    record_identity_event(self, story_id, 'identity_osm', {'generation': generation,
                        'candidate_pool_counts': osm.get('candidate_pool_counts', {}), 'retained_count': len(osm.get('nearby') or []), 'available': bool(osm),
                        'duration_ms': round((time.monotonic() - started) * 1000)})
                    wikipedia = prior.get('wikipedia')
                    if not isinstance(wikipedia, list):
                        try:
                            wikipedia = await self.providers.wikipedia.nearby(float(lat), float(lon))
                        except Exception as exc:
                            # Wikimedia may be unavailable. Never invent visual proof;
                            # keep OSM candidates and require author confirmation then.
                            wikipedia = []
                            record_identity_event(self, story_id, 'identity_wikipedia_unavailable', {'generation': generation, 'error_type': type(exc).__name__})
                    if osm_error is not None and not wikipedia:
                        raise osm_error
                    record_identity_event(self, story_id, 'identity_wikipedia', {'generation': generation, 'count': len(wikipedia)})
                    excluded = set(prior.get('identity_rejected_ids') or [])
                    candidates = self._candidate_catalog(osm, wikipedia, excluded_ids=excluded)
                    candidates.sort(key=lambda item: (distance(item), str(item.get('candidate_id'))))
                    candidates = annotate_camera_alignment(candidates, osm, wikipedia, lat, lon, hints,
                                                           position_verified=position_verified)
                    record_identity_event(self, story_id, 'identity_shortlist', {'generation': generation,
                        'candidate_count': len(candidates), 'candidate_ids': [item.get('candidate_id') for item in candidates],
                        'distances_m': [round(distance(item), 1) if math.isfinite(distance(item)) else None for item in candidates],
                        'excluded_count': len(excluded)})
                    raw = await self._identify_photo(story, transcript, candidates) if candidates else {
                        'status': 'uncertain', 'candidate_id': '', 'confidence': 0, 'observations': ['Подходящих кандидатов не найдено.']}
                except Exception as exc:
                    record_identity_event(self, story_id, 'identity_failed', {'generation': generation, 'error_type': type(exc).__name__,
                        'duration_ms': round((time.monotonic() - started) * 1000)})
                    raise
                error = 'visual_identity_uncertain'
            if not visual_match(raw, candidates):
                from .identity_discovery import recover
                rejected = set(json.loads(story.get('research_json') or '{}').get('identity_rejected_ids') or [])
                recovery = await recover(self, {**story, 'latitude': lat if valid else None,
                    'longitude': lon if valid else None}, transcript, candidates, rejected)
                if recovery:
                    recovered_raw, discovered = recovery
                    recovered_has_candidate = recovered_raw.get('candidate_id') in {
                        item.get('candidate_id') for item in discovered
                    }
                    recovery_is_better = (
                        visual_match(recovered_raw, discovered)
                        or (raw.get('status') == 'mismatch' and recovered_has_candidate
                            and recovered_raw.get('status') != 'mismatch')
                        or (not raw.get('candidate_id') and recovered_has_candidate)
                        or (recovered_has_candidate and recovered_raw.get('status') in {'match', 'uncertain'}
                            and confidence(recovered_raw) >= confidence(raw)
                            and recovered_raw.get('_references_sent'))
                    )
                    if recovery_is_better:
                        raw = recovered_raw
                        ids = {item['candidate_id'] for item in discovered}
                        candidates = (discovered + [item for item in candidates if item['candidate_id'] not in ids])[:16]
                        for item in discovered:
                            if item['candidate_id'].startswith('wiki:'):
                                wikipedia.append({'pageid': int(item['candidate_id'].split(':')[1]),
                                    'title': item['name'], 'url': item['url'], 'extract': item.get('extract', '')})
            catalog = {item['candidate_id']: item for item in candidates}
            selected = catalog.get(str(raw.get('candidate_id') or '')) if raw.get('status') != 'mismatch' else None
            matched = selected is not None and visual_match(raw, candidates)
            identity = {'status': 'match' if matched else 'uncertain',
                'candidate_id': selected['candidate_id'] if selected else None,
                'candidate_name': selected['name'] if selected else None,
                'candidate_url': selected.get('url') if selected else None,
                'source_links': (
                    list(dict.fromkeys(
                        selected.get('source_urls') or ([selected['url']] if selected.get('url') else [])
                    ))[:6] if selected else []
                ),
                'reference_evidence': [item for item in raw.get('_reference_evidence', [])[:6]
                    if item.get('candidate_id') == raw.get('candidate_id')],
                'photo_sha256': story['photo_sha256'], 'generation': generation, 'policy': POLICY,
                'confidence': confidence(raw), 'observations': [str(x)[:300] for x in raw.get('observations', [])[:6]],
                'alternative_candidate_ids': [x for x in raw.get('alternative_candidate_ids', [])[:6] if x in catalog],
                'candidates': candidates, 'visual_reference_verified': matched, 'resolved_at': self.store.now()}
            with self.store.tx() as db:
                current = self._story_row(db, story_id)
                latest = json.loads(current['research_json'] or '{}')
                if (int(latest.get('identity_generation') or 0) != generation or current['photo_sha256'] != story['photo_sha256']
                    or (latest.get('visual_identity') or {}).get('status') == 'owner_confirmed'):
                    return self._story_repr(db, current)
                latest.update({'visual_identity': identity, 'identity_attempted_generation': generation, 'osm': osm, 'wikipedia': wikipedia, 'photo_camera_hints': binding})
                if matched:
                    from .poi_memory import ensure_poi_identity, hydrate_story_facts
                    now = self.store.now()
                    poi_id = ensure_poi_identity(
                        db,
                        identity,
                        latitude=float(lat) if valid else None,
                        longitude=float(lon) if valid else None,
                        now=now,
                    )
                    reused = hydrate_story_facts(db, identity, story_id)
                    latest['poi_id'] = poi_id
                    latest['poi_reused_fact_count'] = reused
                db.execute('UPDATE stories SET latitude=COALESCE(latitude,?),longitude=COALESCE(longitude,?),'
                    'state=?,place_name=?,research_json=?,error_code=?,error_message=?,revision=revision+1,updated_at=? WHERE id=?',
                    (float(lat) if valid else None, float(lon) if valid else None, 'identity_ready' if matched else 'needs_review',
                     identity['candidate_name'] if matched else None, canonical(latest), None if matched else error,
                     None if matched else 'Пока недостаточно доказательств: варианты и основания доступны в теме.', self.store.now(), story_id))
                result = self._story_repr(db, self._story_row(db, story_id))
            record_identity_event(self, story_id, 'identity_finished', {'generation': generation, 'status': identity['status'],
                'candidate_id': identity['candidate_id'], 'candidate_url': identity['candidate_url'], 'confidence': identity['confidence'],
                'reference_verified': matched, 'duration_ms': round((time.monotonic() - started) * 1000)})
            return result

    def reject_identity(self, story_id: str, candidate_id: str, reason: str = '') -> dict[str, Any]:
        from .live import ensure_live_schema
        ensure_live_schema(self)
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            if story['state'] in PROTECTED:
                raise ConflictError('identity_publication_active', 'Сначала отмените отложенную публикацию; опубликованная история не меняется задним числом.')
            prior = json.loads(story['research_json'] or '{}')
            current = prior.get('visual_identity') or {}
            rejected = list(prior.get('identity_rejected_ids') or [])
            if candidate_id in rejected:
                return self._story_repr(db, story)
            if not candidate_id or current.get('candidate_id') != candidate_id:
                raise ConflictError('identity_candidate_changed', 'Объект уже изменился; перечитайте текущую тему.')
            rejected.append(candidate_id)
            history = list(prior.get('identity_history') or [])[-7:]
            history.append({'candidate_id': candidate_id, 'candidate_name': current.get('candidate_name'),
                'candidate_url': current.get('candidate_url'), 'reason': reason[:500], 'at': self.store.now()})
            generation = int(prior.get('identity_generation') or 0) + 1
            prior.update({'identity_generation': generation, 'identity_history': history,
                          'identity_rejected_ids': rejected[-64:], 'visual_identity': {'status': 'rejected', 'candidate_id': None,
                          'photo_sha256': story['photo_sha256'], 'generation': generation, 'candidates': []}})
            visual = json.loads(story['visual_context_json'] or '{}')
            visual.update({'stale': True, 'stale_reason': 'identity_changed', 'content_revision': f'identity-rejected:{generation}'})
            prior['content_identity_changed'] = bool(story['draft_text'])
            db.execute("UPDATE jobs SET state='cancelled',last_error='identity_changed',updated_at=? WHERE story_id=? AND kind IN ('research','visual') AND state IN ('ready','retry')", (self.store.now(), story_id))
            db.execute('UPDATE facts SET selected=0,evidence_supported=0 WHERE story_id=?', (story_id,))
            db.execute("UPDATE live_publication_confirmations SET state='invalidated',updated_at=? WHERE story_id=? AND state='pending'", (self.store.now(), story_id))
            db.execute("UPDATE stories SET place_name=NULL,state='needs_review',research_json=?,visual_context_json=?,"
                       'vibepublish_asset_ref=NULL,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?',
                       (canonical(prior), canonical(visual), self.store.now(), story_id))
        record_identity_event(self, story_id, 'identity_rejected', {'candidate_id': candidate_id, 'generation': generation})
        return self.ensure_identity(story_id)

    def recover_photo_location(self, story_id: str, expected_photo_sha256: str, original: bytes) -> dict[str, Any]:
        story, _ = self._identity_snapshot(story_id)
        if story['photo_sha256'] != expected_photo_sha256:
            raise ConflictError('photo_identity_changed', 'Фото темы уже изменилось.')
        gps = inspect_gps(original)
        if gps['status'] != 'gps_present':
            record_identity_event(self, story_id, 'photo_location_recovery_failed', {'gps_status': gps['status']})
            raise ConflictError('photo_original_gps_unavailable', 'В выбранной копии GPS недоступен. Выберите исходный файл и разрешите чтение геометок.')
        try:
            same = pixel_digest(Path(story['photo_path'])) == pixel_digest(original)
        except Exception:
            same = False
        if not same:
            raise ConflictError('photo_recovery_mismatch', 'Выбрано другое изображение. Для этой темы нужен оригинал того же фото.')
        camera_binding = {'photo_sha256': expected_photo_sha256, 'source': 'selected_original_exif',
                          'metadata': read_camera_hints(original)}
        with self.store.tx() as db:
            row = self._story_row(db, story_id)
            prior = json.loads(row['research_json'] or '{}')
            if row['photo_sha256'] != expected_photo_sha256:
                raise ConflictError('photo_identity_changed', 'Фото темы уже изменилось.')
            if row['state'] in PROTECTED:
                raise ConflictError('identity_publication_active', 'Нельзя менять идентификацию подготовленной публикации.')
            if row['latitude'] is not None and row['longitude'] is not None:
                if abs(row['latitude'] - gps['latitude']) > 0.00001 or abs(row['longitude'] - gps['longitude']) > 0.00001:
                    raise ConflictError('photo_location_conflict', 'Геометки отличаются от уже сохранённых.')
                # Successful replay may enrich optional metadata, never restart identity.
                prior['photo_camera_hints'] = camera_binding
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(prior), story_id))
                return self._story_repr(db, self._story_row(db, story_id))
            generation = int(prior.get('identity_generation') or 0) + 1
            prior.update({'identity_generation': generation, 'photo_camera_hints': camera_binding, 'location_provenance': {'kind': 'selected_original_exif', 'same_pixels_verified': True}})
            prior.pop('visual_identity', None)
            prior.pop('osm', None)
            prior.pop('wikipedia', None)
            db.execute("UPDATE stories SET latitude=?,longitude=?,research_json=?,state='photo_ready',error_code=NULL,error_message=NULL,"
                       'revision=revision+1,updated_at=? WHERE id=?', (gps['latitude'], gps['longitude'], canonical(prior), self.store.now(), story_id))
        record_identity_event(self, story_id, 'photo_location_recovered', {'generation': generation, 'same_pixels_verified': True})
        return self.ensure_identity(story_id)
