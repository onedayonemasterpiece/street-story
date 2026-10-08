"""Persistent, generation-bound photo identity shared by jobs and conversational tools."""
from __future__ import annotations

import asyncio
import json
import math
import time
import weakref
from typing import Any

from .identity_telemetry import record_identity_event
from .identity_candidate_policy import candidate_identity_eligible
from .camera_hints import read_camera_hints, metadata_summary, annotate_camera_alignment
from .photo_metadata import inspect_gps
from .identity_proof import geometry_result_valid, architectural_text_result_valid, non_reference_result_valid
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
    if isinstance(result.get('confidence'), bool):
        return 0.0
    try:
        value = float(result.get('confidence') or 0)
        return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0
    except (ValueError, TypeError):
        return 0.0


def visual_match(result: dict[str, Any], candidates: list[dict[str, Any]],
                 full_shortlist: list[dict[str, Any]] | None = None, *,
                 poi_aliases: dict[str, str] | None = None) -> bool:
    from .identity_subject_binding import article_candidate, physical_alternative_id, reference_binding_valid, subject_aliases
    catalog = [*(full_shortlist or []), *candidates]
    by_id = {item.get('candidate_id'): item for item in catalog}
    selected = result.get('candidate_id')
    if not isinstance(selected, str) or not selected:
        return False
    selected_candidate = by_id.get(selected) or {}
    aliases = subject_aliases(catalog, poi_aliases=poi_aliases).get(selected, {selected})
    alternatives = result.get('alternative_candidate_ids', [])
    if not isinstance(alternatives, list) or any(not isinstance(item, str) for item in alternatives):
        return False
    observations = result.get('observations')
    if result.get('_observable_geometry_required') is True:
        correspondences = result.get('observable_correspondences')
        if (result.get('shared_distinctive_geometry') is not True
                or not isinstance(correspondences, list) or not correspondences
                or any(not isinstance(item, dict) or any(not isinstance(item.get(key), str)
                       or not item[key].strip() for key in ('source_detail', 'reference_detail'))
                       for item in correspondences)):
            return False
    references_sent = result.get('_references_sent')
    if (not isinstance(observations, list) or not observations
            or any(not isinstance(item, str) or not item.strip() for item in observations)
            or not isinstance(references_sent, list)
            or any(not isinstance(item, str) for item in references_sent)):
        return False
    physical_alternatives = [physical_alternative_id(item, by_id) for item in alternatives if item]
    disagreement = [item for item in physical_alternatives if item and item not in aliases]
    sent = selected in {item.get('candidate_id') for item in candidates} and selected in references_sent
    return (result.get('status') == 'match' and selected in by_id
            and candidate_identity_eligible(selected_candidate)
            and not article_candidate(selected_candidate)
            and confidence(result) >= 0.90
            and (sent or reference_binding_valid(result, catalog))
            and not disagreement)


class IdentityLifecycleMixin:
    @staticmethod
    def _identity_attempt_finished(research, photo_sha256, generation):
        terminal = research.get('automatic_research_outcome') or {}
        return bool(terminal.get('outcome') and terminal.get('photo_sha256') == photo_sha256
                    and terminal.get('identity_generation') == generation)

    def _recover_transient_identity(self, db) -> int:
        """Unseal old uncertain attempts only with retained outage evidence.

        Reuse their original identity job and generation. Visual operation
        receipts, leases, owner Stop and publication jobs remain authoritative.
        """
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='live_diagnostics'").fetchone():
            return 0
        from .research_control import research_stopped
        changed = 0
        for row in db.execute("SELECT * FROM stories WHERE state='needs_review' AND error_code='visual_identity_uncertain'").fetchall():
            research = json.loads(row['research_json'] or '{}')
            generation = int(research.get('identity_generation') or 0)
            if self._identity_attempt_finished(research, row['photo_sha256'], generation):
                continue
            if (research.get('visual_identity') or {}).get('status') != 'uncertain' or research_stopped(
                    research, 'identity', photo_sha256=row['photo_sha256'], identity_generation=generation):
                continue
            started = db.execute("SELECT max(created_at) FROM live_diagnostics WHERE story_id=? AND source='identity' "
                "AND event_type='identity_started' AND json_extract(payload_json,'$.generation')=?", (row['id'], generation)).fetchone()[0]
            if started is None or not db.execute("SELECT 1 FROM live_diagnostics WHERE story_id=? AND source='identity' "
                "AND created_at>=? AND event_type IN ('identity_osm_unavailable','identity_wikipedia_unavailable','identity_discovery_unavailable') LIMIT 1",
                (row['id'], started)).fetchone():
                continue
            resumed = db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error='identity_sources_waiting',updated_at=? "
                "WHERE story_id=? AND kind='identity' AND state='done' AND json_extract(payload_json,'$.identity_generation')=?",
                (self.store.now(), self.store.now(), row['id'], generation)).rowcount
            if not resumed:
                continue
            research.pop('identity_attempted_generation', None)
            research['identity_sources_waiting'] = True
            # A failed Wikipedia fetch was previously serialized as an empty
            # successful result. Fetch it again; keep all usable OSM candidates.
            research.pop('wikipedia', None)
            db.execute("UPDATE stories SET state='identifying',research_json=?,error_code='identity_sources_waiting',"
                "error_message='Источники не ответили; поиск продолжится автоматически.',revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.store.now(), row['id']))
            import logging
            logging.getLogger('uvicorn.error').info('street_story_identity_recovery %s', canonical({
                'story_id': row['id'], 'generation': generation, 'reason': 'retained_provider_outage', 'jobs_resumed': resumed}))
            changed += resumed
        return changed

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
            if self._identity_attempt_finished(research, row['photo_sha256'], generation):
                return self._story_repr(db, row)
            if identity.get('status') in ACCEPTED or row['state'] in PROTECTED:
                return self._story_repr(db, row)
            # A finished uncertain result is not a reason to resubmit on every
            # foreground refresh. Recovery or explicit correction creates a generation.
            if research.get('identity_attempted_generation') == generation:
                return self._story_repr(db, row)
            semantic = 'identity:' + digest({'story_id': story_id, 'photo_sha256': row['photo_sha256'],
                                            'generation': generation, 'lat': row['latitude'], 'lon': row['longitude']})
            job_id = self._enqueue_job(db, story_id, 'identity', semantic,
                {'identity_generation': generation, 'queue_priority': 'interactive'})
            job = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if job['state'] in {'ready', 'running', 'retry'} and row['state'] in {'photo_ready', 'voice_ready', 'needs_review'}:
                db.execute("UPDATE stories SET state='identifying',error_code=NULL,error_message=NULL,"
                           'revision=revision+1,updated_at=? WHERE id=?', (self.store.now(), story_id))
            result = self._story_repr(db, self._story_row(db, story_id))
        record_identity_event(self, story_id, 'identity_requested', {'generation': generation, 'job_id': job_id})
        return result

    async def _run_identity(self, job: dict[str, Any]) -> None:
        generation = json.loads(job.get('payload_json') or '{}').get('identity_generation', 0)
        await self.resolve_identity(job['story_id'], expected_generation=generation, job_id=job['id'], job_attempt=job['attempts'])

    async def resolve_identity(self, story_id: str, transcript: str = '', *, expected_generation=None, job_id=None, job_attempt=None, owner_hint=''):
        if not hasattr(self, '_identity_locks'):
            self._identity_locks = weakref.WeakValueDictionary()
        lock = self._identity_locks.setdefault(story_id, asyncio.Lock())
        from .identity_wikipedia_metadata import metadata_scope
        async with lock, metadata_scope() as metadata_tasks:
            story, prior = self._identity_snapshot(story_id)
            generation = int(prior.get('identity_generation') or 0)
            from .research_control import research_stopped
            control_revision = int(((prior.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)
            if research_stopped(prior, 'identity', photo_sha256=story['photo_sha256'], identity_generation=generation):
                return self.story(story_id)
            previous = prior.get('visual_identity') or {}
            if (story.get('error_code') == 'visual_identity_conflict'
                    and int(previous.get('generation') or 0) == generation):
                return self.story(story_id)  # Existing explicit correction creates the next generation.
            if expected_generation is not None and expected_generation != generation:
                record_identity_event(self, story_id, 'identity_stale_job', {'generation': generation, 'job_generation': expected_generation})
                return self.story(story_id)
            if previous.get('status') in ACCEPTED or (prior.get('identity_attempted_generation') == generation and not owner_hint.strip()):
                record_identity_event(self, story_id, 'identity_reused', {'generation': generation, 'status': previous.get('status')})
                return self.story(story_id)
            started = time.monotonic()
            record_identity_event(self, story_id, 'identity_started', {'generation': generation, 'job_id': job_id, 'policy': POLICY})
            lat, lon = story.get('latitude'), story.get('longitude')
            source = self._source_photo_for_job(story_id)
            import hashlib
            story['_identity_original_source_sha256'] = hashlib.sha256(source).hexdigest()
            metadata = inspect_gps(source)
            binding = prior.get('photo_camera_hints') or {}
            recovered_hints = (binding.get('photo_sha256') == story['photo_sha256']
                and binding.get('source') == 'selected_original_exif'
                and (prior.get('location_provenance') or {}).get('kind') == 'selected_original_exif')
            hints = binding['metadata'] if recovered_hints else read_camera_hints(source)
            if not recovered_hints:
                binding = {'photo_sha256': story['photo_sha256'], 'source': 'source_photo_exif', 'metadata': hints}
            story['_camera_hints'] = hints
            story['_identity_generation'] = generation
            story['_identity_research_control_revision'] = control_revision
            story['_research_job_id'] = job_id
            story['_research_job_attempt'] = job_attempt
            if lat is None or lon is None:
                lat, lon = metadata['latitude'], metadata['longitude']
            valid = lat is not None and lon is not None and math.isfinite(float(lat)) and math.isfinite(float(lon)) and -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
            owner_approx_camera = (prior.get('location_provenance') or {}).get('kind') == 'owner_approx_camera'
            record_identity_event(self, story_id, 'identity_location', {
                'generation': generation, 'client_coordinates_present': story.get('latitude') is not None and story.get('longitude') is not None,
                'photo_gps_status': metadata['status'], 'coordinates_usable': bool(valid),
                'source': 'owner_approx_camera' if owner_approx_camera else 'client_photo_metadata' if story.get('latitude') is not None else 'server_photo_exif',
            })
            position_verified = bool(valid and not owner_approx_camera and (recovered_hints or (
                metadata['status'] == 'gps_present'
                and abs(float(lat) - metadata['latitude']) <= 0.00001
                and abs(float(lon) - metadata['longitude']) <= 0.00001)))
            record_identity_event(self, story_id, 'identity_camera_metadata', {
                'generation': generation, 'position_verified': position_verified, **metadata_summary(hints)})
            story['_camera_position_verified'] = position_verified
            story['latitude'], story['longitude'] = (lat, lon) if valid else (None, None)
            if owner_hint.strip():
                story['_identity_owner_hint'] = {'text': owner_hint.strip()[:500],
                    'provenance': 'same_conversation_owner_hint', 'scope': 'search_context_only'}
            osm, wikipedia, candidates, observed_candidates = {}, [], [], []
            rejected = set(prior.get('identity_rejected_ids') or [])
            source_waits = []
            if not valid:
                raw = {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                       'observations': ['Геометки снимка отсутствуют; проверяю видимые признаки и доступный авторский контекст.']}
                error = 'identity_location_missing'
            else:
                nearby_task = None
                try:
                    osm = prior.get('osm')
                    map_policy = getattr(self.providers.osm, 'LOOKUP_POLICY_VERSION', None)
                    map_policy_changed = (isinstance(osm, dict) and map_policy is not None
                        and osm.get('lookup_policy_version') != map_policy)
                    wikipedia = prior.get('wikipedia')
                    need_metadata = not isinstance(wikipedia, list) or prior.get('wikipedia_metadata_complete') is False or map_policy_changed
                    if need_metadata:
                        # Read-only metadata acquisition overlaps reverse/map HTTP.
                        nearby_task = asyncio.create_task(self.providers.wikipedia.nearby(float(lat), float(lon)))
                        metadata_tasks.add(nearby_task)
                    if (not isinstance(osm, dict) or not osm or osm.get('partial')
                            or map_policy is not None and osm.get('lookup_policy_version') != map_policy):
                        try:
                            osm = await self.providers.osm.lookup(float(lat), float(lon))
                        except Exception as exc:
                            from .providers import RetryableProviderError
                            if not isinstance(exc, RetryableProviderError):
                                raise
                            if not isinstance(osm, dict):
                                osm = {}
                            source_waits.append(exc)
                            record_identity_event(self, story_id, 'identity_osm_unavailable', {'generation': generation, 'error_type': type(exc).__name__})
                    if osm.get('partial'):
                        from .errors import RetryableProviderError
                        source_waits.append(RetryableProviderError('identity_osm_partial'))
                    record_identity_event(self, story_id, 'identity_osm', {'generation': generation,
                        'candidate_pool_counts': osm.get('candidate_pool_counts', {}), 'retained_count': len(osm.get('nearby') or []), 'available': bool(osm),
                        'observed_count': len(osm.get('observed_pool', osm.get('nearby', []))),
                        'partial': bool(osm.get('partial')), 'unavailable_buckets': osm.get('unavailable_buckets', []),
                        'duration_ms': round((time.monotonic() - started) * 1000)})
                    story['_identity_map_snapshot'] = osm
                    if need_metadata:
                        # The map is durable before the bounded metadata join.
                        # This checkpoint claims no identity and schedules no REF.
                        geometry = self._candidate_catalog(osm, [], observed_pool=True)
                        with self.store.tx() as db:
                            current = self._story_row(db, story_id)
                            latest = json.loads(current['research_json'] or '{}')
                            if (current['photo_sha256'] != story['photo_sha256']
                                    or int(latest.get('identity_generation') or 0) != generation
                                    or int(((latest.get('research_controls') or {}).get('identity') or {}).get('revision') or 0) != control_revision
                                    or research_stopped(latest, 'identity', photo_sha256=current['photo_sha256'], identity_generation=generation)
                                    or (latest.get('visual_identity') or {}).get('status') in ACCEPTED
                                    or (current['error_code'] == 'visual_identity_conflict'
                                        and int((latest.get('visual_identity') or {}).get('generation') or 0) == generation)
                                    or job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?", (job_id, job_attempt)).fetchone()):
                                return self._story_repr(db, current)
                            latest['osm'] = osm
                            latest['visual_identity'] = {**(latest.get('visual_identity') or {}),
                                'status': 'uncertain', 'generation': generation, 'photo_sha256': story['photo_sha256'],
                                'observed_candidates': geometry, 'camera_hints': hints,
                                'camera_position_verified': position_verified}
                            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(latest), story_id))
                        from .identity_wikipedia_metadata import ready_metadata
                        wikipedia, pending_metadata, metadata_failures = await ready_metadata(
                            self.providers.wikipedia, osm, nearby_task, owned_tasks=metadata_tasks)
                        story['_wikipedia_metadata_complete'] = not pending_metadata and not metadata_failures
                        if pending_metadata or metadata_failures:
                            from .errors import RetryableProviderError
                            source_waits.append(RetryableProviderError('identity_wikipedia_waiting',
                                retry_at=min((getattr(exc, 'retry_at', None) or self.store.now()+15 for exc in metadata_failures), default=self.store.now()+15)))
                            record_identity_event(self, story_id, 'identity_wikipedia_unavailable', {
                                'generation': generation, 'pending_routes': pending_metadata,
                                'error_types': [type(exc).__name__ for exc in metadata_failures], 'ready_count': len(wikipedia)})
                    record_identity_event(self, story_id, 'identity_wikipedia', {'generation': generation, 'count': len(wikipedia)})
                    excluded = set(prior.get('identity_rejected_ids') or [])
                    candidates = self._candidate_catalog(osm, wikipedia, excluded_ids=excluded)
                    observed_candidates = self._candidate_catalog(osm, wikipedia, excluded_ids=excluded, observed_pool=True)
                    observed_candidates = annotate_camera_alignment(observed_candidates,
                        {**osm, 'nearby': osm.get('observed_pool', osm.get('nearby', []))}, wikipedia, lat, lon, hints,
                                                                    position_verified=position_verified)
                    story['_identity_observed_candidates'] = observed_candidates
                    candidates.sort(key=lambda item: (distance(item), str(item.get('candidate_id'))))
                    candidates = annotate_camera_alignment(candidates, osm, wikipedia, lat, lon, hints,
                                                           position_verified=position_verified)
                    story['_identity_wikipedia_metadata'] = wikipedia
                    from .identity_map_context import map_entry_context
                    story['_identity_search_context'] = {
                        'radius_m': osm.get('radius_m'),
                        'reverse_address': (osm.get('reverse') or {}).get('address') or {},
                        'reverse_map_context': map_entry_context(osm.get('reverse') or {}),
                        'nearby': [{**map_entry_context(item), 'distance_m': item.get('distance_m')}
                            for item in sorted(osm.get('nearby') or [], key=distance)[:20]]}
                    google = self.providers.gemini
                    can_plan = callable(getattr(getattr(self.providers, 'research', None), 'plan_identity_search', None)) or (
                        callable(getattr(google, '_generate', None)) and callable(getattr(getattr(google, 'executor', None), 'execute', None)))
                    if can_plan and (wikipedia or observed_candidates):
                        from .identity_discovery import prepare_search_plan
                        await prepare_search_plan(self, story, transcript, candidates)
                    record_identity_event(self, story_id, 'identity_shortlist', {'generation': generation,
                        'candidate_count': len(candidates), 'candidate_ids': [item.get('candidate_id') for item in candidates],
                        'observed_candidate_count': len(observed_candidates),
                        'distances_m': [round(distance(item), 1) if math.isfinite(distance(item)) else None for item in candidates],
                        'excluded_count': len(excluded)})
                    raw = story.get('_identity_accepted_result') or story.get('_identity_geometry_result') or (await self._identify_photo(story, transcript, candidates) if candidates else {
                        'status': 'uncertain', 'candidate_id': '', 'confidence': 0, 'observations': ['Подходящих кандидатов не найдено.']})
                except Exception as exc:
                    record_identity_event(self, story_id, 'identity_failed', {'generation': generation, 'error_type': type(exc).__name__,
                        'duration_ms': round((time.monotonic() - started) * 1000)})
                    raise
                error = 'visual_identity_uncertain'
            if raw.get('_comparison_deferred') and candidates:
                # Publish ready references before independent article discovery waits.
                # The dedicated visual worker keeps the same queue/operation fences.
                with self.store.tx() as db:
                    current = self._story_row(db, story_id)
                    latest = json.loads(current['research_json'] or '{}')
                    if (int(latest.get('identity_generation') or 0) != generation
                            or current['photo_sha256'] != story['photo_sha256']
                            or (latest.get('visual_identity') or {}).get('status') in ACCEPTED
                            or (current['error_code'] == 'visual_identity_conflict'
                                and int((latest.get('visual_identity') or {}).get('generation') or 0) == generation)
                            or research_stopped(latest, 'identity', photo_sha256=current['photo_sha256'], identity_generation=generation)
                            or int(((latest.get('research_controls') or {}).get('identity') or {}).get('revision') or 0) != control_revision):
                        return self._story_repr(db, current)
                    if job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?", (job_id, job_attempt)).fetchone():
                        return self._story_repr(db, current)
                    latest['visual_identity'] = {'status': 'uncertain', 'candidates': candidates,
                        'observed_candidates': observed_candidates, 'camera_hints': hints,
                        'camera_position_verified': position_verified,
                        'generation': generation, 'photo_sha256': story['photo_sha256']}
                    latest.update(osm=osm, wikipedia=wikipedia, photo_camera_hints=binding)
                    latest['wikipedia_metadata_complete'] = story.get('_wikipedia_metadata_complete', True)
                    db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(latest), story_id))
                self._schedule_identity_visual()
                record_identity_event(self, story_id, 'identity_shortlist_ready', {'generation': generation,
                    'candidate_count': len(candidates), 'discovery_may_continue': True})
            if not (visual_match(raw, candidates) or non_reference_result_valid(raw, candidates, story)):
                from .identity_discovery import recover
                rejected = set(json.loads(story.get('research_json') or '{}').get('identity_rejected_ids') or [])
                known_sources = []
                if raw.get('_comparison_deferred') and candidates:
                    from .poi_memory import candidate_article_sources, candidate_reference_images
                    with self.store.connection() as db:
                        known_sources = [*candidate_reference_images(db, candidates),
                                         *candidate_article_sources(db, candidates)]
                if known_sources and not owner_hint.strip():
                    # New photos still require a new comparison. Deliver the
                    # existing queue first; searching is not a prerequisite for
                    # addressing an already accumulated physical POI reference.
                    record_identity_event(self, story_id, 'identity_memory_acquisition_ready', {
                        'generation': generation, 'source_count': len(known_sources), 'identity_proof_reused': False})
                    recovery = None
                else:
                    from .errors import RetryableProviderError
                    try:
                        from .identity_map_context import map_entry_context
                        nearest = sorted(osm.get('nearby') or [], key=distance)[:20]
                        search_context = {
                            'radius_m': osm.get('radius_m'),
                            'reverse_address': (osm.get('reverse') or {}).get('address') or {},
                            'reverse_map_context': map_entry_context(osm.get('reverse') or {}),
                            'nearby': [{**map_entry_context(item), 'distance_m': item.get('distance_m'),
                                'tags': {key: value for key, value in (item.get('tags') or {}).items()
                                    if key in {'name', 'addr:street', 'addr:housenumber', 'building', 'historic', 'highway'}}}
                                for item in nearest]}
                        recovery = await recover(self, {**story, 'latitude': lat if valid else None,
                            'longitude': lon if valid else None, '_identity_search_context': search_context},
                            transcript, candidates, rejected)
                    except RetryableProviderError as exc:
                        source_waits.append(exc)
                        recovery = None
                if recovery:
                    recovered_raw, discovered = recovery
                    recovered_has_candidate = recovered_raw.get('candidate_id') in {
                        item.get('candidate_id') for item in discovered
                    }
                    recovery_is_better = (
                        visual_match(recovered_raw, discovered, [*candidates, *discovered])
                        or non_reference_result_valid(recovered_raw, [*candidates, *discovered], story)
                        or (raw.get('status') == 'mismatch' and recovered_has_candidate
                            and recovered_raw.get('status') != 'mismatch')
                        or (not raw.get('candidate_id') and recovered_has_candidate)
                        or (recovered_has_candidate and recovered_raw.get('status') in {'match', 'uncertain'}
                            and confidence(recovered_raw) >= confidence(raw)
                            and recovered_raw.get('_references_sent'))
                    )
                    if (recovered_raw.get('_article_media_pending')
                            or recovered_raw.get('_comparison_deferred')):
                        # The shared visual queue must receive expanded physical
                        # hypotheses even when no model has selected one yet.
                        # Retaining references does not replace the current verdict.
                        ids = {item['candidate_id'] for item in discovered}
                        candidates = ([item for item in candidates if item['candidate_id'] not in ids] + discovered)[:36]
                    if recovery_is_better:
                        raw = recovered_raw
                        ids = {item['candidate_id'] for item in discovered}
                        candidates = (discovered + [item for item in candidates if item['candidate_id'] not in ids])[:16]
                        for item in discovered:
                            if item['candidate_id'].startswith('wiki:'):
                                wikipedia.append({'pageid': int(item['candidate_id'].split(':')[1]),
                                    'title': item['name'], 'url': item['url'], 'extract': item.get('extract', '')})
            if metadata_tasks and story.get('_wikipedia_metadata_complete') is False:
                from .identity_wikipedia_metadata import merge_metadata, late_linked_page_ids, selected_candidates
                completed_batches = [task.result() for task in metadata_tasks
                    if task.done() and not task.cancelled() and task.exception() is None]
                late_pages = merge_metadata(*completed_batches)
                if late_pages:
                    wikipedia = merge_metadata(wikipedia, late_pages)
                    current_story, latest = self._identity_snapshot(story_id)
                    history = latest.get('identity_article_discovery') or {}
                    saved = history.get('search_plan') or {}
                    payload = saved.get('payload') or {}
                    if (saved.get('photo_sha256') == story['photo_sha256'] and saved.get('generation') == generation
                            and saved.get('control_revision') == control_revision and payload.get('first_wave_contract')):
                        late_ids = late_linked_page_ids(late_pages, payload)
                        if late_ids:
                            from .identity_discovery import _retain_article_discovery
                            payload = {**payload, 'selected_wikipedia_page_ids': payload.get('selected_wikipedia_page_ids') or [],
                                'late_mapped_wikipedia_page_ids': late_ids,
                                'late_reference_provenance': 'exact_osm_link_to_model_selected_physical_hypothesis'}
                            _retain_article_discovery(self, story, [], search_plan={**saved, 'payload': payload})
                        full = self._candidate_catalog(osm, wikipedia, excluded_ids=rejected, observed_pool=True)
                        observed_candidates = annotate_camera_alignment(full,
                            {**osm, 'nearby': osm.get('observed_pool', osm.get('nearby', []))}, wikipedia,
                            lat, lon, hints, position_verified=position_verified)
                        candidates = selected_candidates(candidates, observed_candidates, wikipedia,
                            {**payload, 'selected_wikipedia_page_ids': payload.get('selected_wikipedia_page_ids') or []})
                    story['_wikipedia_metadata_complete'] = all(task.done() and not task.cancelled()
                        and task.exception() is None for task in metadata_tasks)
                    if story['_wikipedia_metadata_complete']:
                        source_waits = [exc for exc in source_waits if not str(exc).startswith('identity_wikipedia')]
            catalog = {item['candidate_id']: item for item in candidates}
            selected = catalog.get(str(raw.get('candidate_id') or '')) if raw.get('status') != 'mismatch' else None
            reference_verified = selected is not None and visual_match(raw, candidates)
            geometry_verified = selected is not None and geometry_result_valid(raw, candidates, story)
            text_verified = selected is not None and architectural_text_result_valid(raw, candidates, story)
            matched = reference_verified or geometry_verified or text_verified
            if not matched and (raw.get('_references_rate_limited') or raw.get('_references_unavailable_ids')):
                from .errors import RetryableProviderError
                source_waits.append(RetryableProviderError('identity_references_waiting'))
            waiting = bool(source_waits) and not matched and not (error == 'identity_location_missing' and not candidates)
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
                'confidence': None if (geometry_verified or text_verified) and not reference_verified else confidence(raw), 'observations': [str(x)[:300] for x in raw.get('observations', [])[:6]],
                'alternative_candidate_ids': [x for x in raw.get('alternative_candidate_ids', [])[:6] if x in catalog],
                'candidates': candidates, 'observed_candidates': observed_candidates, 'camera_hints': hints,
                'camera_position_verified': position_verified,
                'visual_reference_verified': reference_verified, 'identity_verified': matched, 'resolved_at': self.store.now(),
                'control_revision': control_revision,
                'proof_kind': 'combined' if sum([reference_verified, geometry_verified, text_verified]) > 1
                    else 'geometry' if geometry_verified else 'architectural_text' if text_verified
                    else 'visual_reference' if reference_verified else None,
                **({'geometry_proof': raw['geometry_proof']} if geometry_verified else {}),
                **({'architectural_text_proof': raw['architectural_text_proof']} if text_verified else {})}
            with self.store.tx() as db:
                current = self._story_row(db, story_id)
                latest = json.loads(current['research_json'] or '{}')
                if (int(latest.get('identity_generation') or 0) != generation or current['photo_sha256'] != story['photo_sha256']
                    or research_stopped(latest, 'identity', photo_sha256=current['photo_sha256'], identity_generation=generation)
                    or int(((latest.get('research_controls') or {}).get('identity') or {}).get('revision') or 0) != control_revision
                    or (latest.get('visual_identity') or {}).get('status') in ACCEPTED
                    or (current['error_code'] == 'visual_identity_conflict'
                        and int((latest.get('visual_identity') or {}).get('generation') or 0) == generation)):
                    return self._story_repr(db, current)
                if job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?", (job_id, job_attempt)).fetchone():
                    return self._story_repr(db, current)
                latest.update({'visual_identity': identity, 'osm': osm, 'photo_camera_hints': binding})
                latest['wikipedia_metadata_complete'] = story.get('_wikipedia_metadata_complete', True)
                question = ((latest.get('identity_article_discovery') or {}).get('search_plan') or {}).get('payload', {}).get('clarification_question')
                if not valid and isinstance(question, str) and question.strip():
                    latest['identity_clarification'] = {'question': question.strip()[:200],
                        'reason': 'geographic_context_missing', 'photo_sha256': story['photo_sha256'],
                        'generation': generation, 'control_revision': control_revision}
                # Keep successful discovery available to the SAME visual queue,
                # but do not seal a generation while providers are unavailable.
                if waiting:
                    latest.pop('identity_attempted_generation', None)
                    latest['identity_sources_waiting'] = True
                    if any(str(exc).startswith('identity_wikipedia') for exc in source_waits):
                        latest.pop('wikipedia', None)
                    else:
                        latest['wikipedia'] = wikipedia
                else:
                    latest.update(identity_attempted_generation=generation, wikipedia=wikipedia)
                    latest.pop('identity_sources_waiting', None)
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
                    (float(lat) if valid else None, float(lon) if valid else None, 'identity_ready' if matched else ('identifying' if waiting else 'needs_review'),
                     identity['candidate_name'] if matched else None, canonical(latest), None if matched else ('identity_sources_waiting' if waiting else error),
                     None if matched else ('Источники не ответили; поиск продолжится автоматически.' if waiting else
                         'Пока недостаточно доказательств: варианты и основания доступны в теме.'), self.store.now(), story_id))
                if owner_hint.strip() and not matched:
                    db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=NULL,updated_at=? "
                               "WHERE story_id=? AND kind='identity_visual' AND state='done' "
                               "AND json_extract(payload_json,'$.identity_generation')=?",
                               (self.store.now(), self.store.now(), story_id, generation))
                result = self._story_repr(db, self._story_row(db, story_id))
            if waiting:
                # Revisit independent work, not the slowest provider's cooldown.
                # Each provider keeps its own admission/cooldown; this cannot
                # resend an unknown visual operation or bypass quota controls.
                retry_at = self.store.now() + 60
                record_identity_event(self, story_id, 'identity_sources_waiting', {'generation': generation,
                    'candidate_count': len(candidates), 'retry_at': retry_at})
                from .errors import RetryableProviderError
                raise RetryableProviderError('identity_sources_waiting', retry_at=retry_at)
            record_identity_event(self, story_id, 'identity_finished', {'generation': generation, 'status': identity['status'],
                'candidate_id': identity['candidate_id'], 'candidate_url': identity['candidate_url'], 'confidence': identity['confidence'],
                'reference_verified': reference_verified, 'proof_kind': identity['proof_kind'],
                'physical_identity_verified': matched, 'duration_ms': round((time.monotonic() - started) * 1000)})
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
            db.execute(
                "UPDATE fact_assertions SET owner_selected=0,review_status='withheld',eligibility='withheld',updated_at=? "
                "WHERE story_id=?",
                (self.store.now(), story_id),
            )
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
                self._restore_source_photo(db, story_id, original)
                # Successful replay may enrich optional metadata, never restart identity.
                prior['photo_camera_hints'] = camera_binding
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(prior), story_id))
                return self._story_repr(db, self._story_row(db, story_id))
            self._restore_source_photo(db, story_id, original)
            generation = int(prior.get('identity_generation') or 0) + 1
            prior.update({'identity_generation': generation, 'photo_camera_hints': camera_binding, 'location_provenance': {'kind': 'selected_original_exif', 'selected_original_metadata': True}})
            prior.pop('visual_identity', None)
            prior.pop('osm', None)
            prior.pop('wikipedia', None)
            db.execute("UPDATE stories SET latitude=?,longitude=?,research_json=?,state='photo_ready',error_code=NULL,error_message=NULL,"
                       'revision=revision+1,updated_at=? WHERE id=?', (gps['latitude'], gps['longitude'], canonical(prior), self.store.now(), story_id))
        record_identity_event(self, story_id, 'photo_location_recovered', {'generation': generation, 'selected_original_metadata': True})
        return self.ensure_identity(story_id)
