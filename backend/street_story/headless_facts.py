"""Bounded fact pages through the existing frozen Live research intake."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from types import SimpleNamespace

from . import review_packets
from .errors import MalformedProviderResponse, RetryableProviderError
from .identity_telemetry import record_identity_event
from .live import StreetStoryLiveAdapter, _search_source_ref
from .poi_memory import memory_keys, prior_facts, processed_sources
from .research_control import research_stopped
from .research_runs import (
    manifest_complete,
    register_discovered_source,
    run_manifest,
    set_run_state,
)
from .service import ConflictError, canonical

LOG = logging.getLogger(__name__)


class HeadlessFacts:
    """Up to three independent frozen cores; durable commits remain serial."""

    def __init__(self, service):
        self.service = service
        self.adapter = StreetStoryLiveAdapter(service, emit=lambda *_: None, write=lambda *_: None)

    def _snapshot(self, job, run_id, control_revision=None):
        with self.service.store.connection() as db:
            story = dict(self.service._story_row(db, job['story_id']))
            research = json.loads(story['research_json'] or '{}')
            run = db.execute('SELECT * FROM research_runs WHERE run_id=? AND story_id=?',
                             (run_id, story['id'])).fetchone()
            identity = research.get('visual_identity') or {}
            payload = json.loads(job.get('payload_json') or '{}')
            generation = int(research.get('identity_generation') or 0)
            epoch = int(((research.get('research_controls') or {}).get('facts') or {}).get('revision') or 0)
            job_owned = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?",
                                   (job['id'], job.get('attempts', 0))).fetchone()
            if (not job_owned or run is None or run['state'] in {'cancelled', 'failed'}
                    or research_stopped(research, 'facts', photo_sha256=story['photo_sha256'], identity_generation=generation)
                    or research.get('research_cancelled')
                    or (control_revision is not None and epoch != control_revision)
                    or int(run['identity_generation']) != generation
                    or payload.get('identity_generation', generation) != generation
                    or payload.get('photo_sha256', story['photo_sha256']) != story['photo_sha256']
                    or identity.get('status') not in {'match', 'owner_confirmed'}
                    or run['poi_key'] not in memory_keys(db, identity)):
                return None
            story.update(_identity_generation=generation, _research_run_id=run_id,
                         _research_job_id=job['id'], _research_job_attempt=job.get('attempts', 0),
                         _fact_research_control_revision=epoch)
            return story, research, dict(run)

    def _bind_discovery(self, job, run_id, goal, scope, sources, receipt, control_revision):
        """Save the same short refs/history used by the regular Live reader."""
        with self.service.store.tx() as db:
            snapshot = self._snapshot(job, run_id, control_revision)
            if snapshot is None:
                return False
            story, research, _ = snapshot
            stored = {row['url']: dict(row) for row in db.execute(
                'SELECT * FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url', (run_id,))}
            for source in sources:
                url = str(source.get('url') or '').rstrip('/')
                if not url.startswith('https://') or url in stored:
                    continue
                register_discovered_source(db, run_id=run_id, url=url, title=str(source.get('title') or url),
                                           status='discovered', now=self.service.store.now())
                stored[url] = source
            history = list(research.get('live_web_searches') or [])
            if not any(item.get('research_run_id') == run_id for item in history if isinstance(item, dict)):
                history.append({
                    'query': goal, 'coverage_goal': goal, 'extraction_scope': scope,
                    'research_run_id': run_id,
                    'save_batch_id': 'livebatch_' + hashlib.sha256(f'{run_id}:discovery-save'.encode()).hexdigest()[:24],
                    'source_urls': list(stored), 'source_refs': [_search_source_ref(url) for url in stored],
                    'discovery_only': True, 'search_provider': receipt.get('backend', 'unknown'),
                })
                research['live_web_searches'] = history[-12:]
            elif receipt.get('backend') and receipt['backend'] != 'poi_memory':
                for item in history:
                    if isinstance(item, dict) and item.get('research_run_id') == run_id:
                        item.update(search_provider=receipt['backend'], source_urls=list(stored),
                                    source_refs=[_search_source_ref(url) for url in stored])
            grounding = {str(item.get('url') or '').rstrip('/'): item
                         for item in research.get('grounding_sources') or [] if isinstance(item, dict)}
            for url, source in stored.items():
                grounding.setdefault(url, {'url': url, 'title': str(source.get('title') or url),
                                          'source_ref': _search_source_ref(url), 'type': 'web', 'supports': []})
            research['grounding_sources'] = list(grounding.values())
            db.execute('UPDATE stories SET research_json=?,updated_at=? WHERE id=?',
                       (canonical(research), self.service.store.now(), story['id']))
            return True

    def _partial(self, run_id, reason, *, retry_at=None):
        with self.service.store.tx() as db:
            run = db.execute('SELECT state FROM research_runs WHERE run_id=?', (run_id,)).fetchone()
            if run and run['state'] not in {'cancelled', 'failed'}:
                set_run_state(db, run_id, 'partial', detail=reason, now=self.service.store.now())
        delay = 300 if reason in {'research_fact_source_coverage_partial', 'research_fact_source_unreadable'} else 10
        due = self.service.store.now() + delay if retry_at is None else max(self.service.store.now() + 1, retry_at)
        raise RetryableProviderError(reason, retry_at=due)

    def _pending_retry_at(self, job, run_id):
        with self.service.store.connection() as db:
            chunks = {row[0] for row in db.execute("SELECT chunk_id FROM research_chunk_runs WHERE run_id=? "
                                                  "AND status NOT IN ('extracted','no_claims')", (run_id,))}
            states = [json.loads(row[0]) for row in db.execute("SELECT value_json FROM research_checkpoints "
                "WHERE job_id=? AND stage LIKE 'headless_fact_unit:%'", (job['id'],))]
        due = [state['retry_at'] for state in states if state.get('chunk_id') in chunks
               and isinstance(state.get('retry_at'), (int, float))
               and not isinstance(state['retry_at'], bool) and math.isfinite(state['retry_at'])
               and state['retry_at'] > self.service.store.now()]
        return min(due) if due else None

    async def _discover_requested_gap(self, job, run_id, goal, scope, provider, control_revision):
        payload = json.loads(job.get('payload_json') or '{}')
        query = str(payload.get('research_query') or '').strip()
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        story, _, _ = snapshot
        # Keep the accepted model discovery decision across a route failure.
        with self.service.store.tx() as db:
            if self._snapshot(job, run_id, control_revision) is None:
                return
            set_run_state(db, run_id, 'partial', detail='research_fact_discovery_pending', now=self.service.store.now())
        search = getattr(provider, 'search_fact_articles', None) or getattr(provider, 'search_articles', None)
        if not callable(search) or not query:
            raise RetryableProviderError('research_search_unavailable', retry_at=self.service.store.now()+60)
        found = await search(query, story)
        if self._snapshot(job, run_id, control_revision) is None:
            return
        added = [source for source in found.get('sources') or [] if isinstance(source, dict)]
        found_receipt = {**(found.get('receipt') or {}),
                         'backend': (found.get('receipt') or {}).get('backend', 'external_search')}
        if not self._bind_discovery(job, run_id, goal, scope, added, found_receipt, control_revision):
            return
        with self.service.store.connection() as db:
            unread = db.execute("SELECT 1 FROM research_run_sources WHERE run_id=? AND source_version_id IS NULL "
                                "AND status!='failed' LIMIT 1", (run_id,)).fetchone()
        if unread:
            self._partial(run_id, 'research_fact_next_page')
        with self.service.store.tx() as db:
            if self._snapshot(job, run_id, control_revision) is None:
                return
            set_run_state(db, run_id, 'completed', now=self.service.store.now(), completed=True)

    def _queue_model_continuation(self, job, run_id, goal, scope, result, control_revision):
        """Join an explicit model-owned new aspect after the current run finishes.

        Page continuation describes unread text; research sufficiency describes
        the publication goal. Neither is inferred from a server fact count.
        """
        if (result.get('research_sufficient') is not False or result.get('source_matches_poi') is not True
                or result.get('source_content_valid') is not True):
            return False
        query = result.get('next_research_query')
        next_goal = result.get('next_research_goal')
        if not isinstance(query, str) or not isinstance(next_goal, str):
            return False
        query, next_goal = query.strip(), next_goal.strip()
        if not query or not next_goal or len(query) > 1000 or len(next_goal) > 1600:
            return False
        def normalized(value):
            return ' '.join(value.split()).casefold()
        with self.service.store.tx() as db:
            snapshot = self._snapshot(job, run_id, control_revision)
            if snapshot is None:
                return False
            story, research, _ = snapshot
            identity = research['visual_identity']
            old_payload = json.loads(job.get('payload_json') or '{}')
            current_query = str(old_payload.get('research_query') or ' '.join(
                str(value or '').strip() for value in (identity.get('candidate_name'), goal))).strip()
            if (normalized(query), normalized(next_goal)) == (normalized(current_query), normalized(goal)):
                return False
            recipe = [normalized(query), normalized(next_goal)]
            next_scope = 'model:' + hashlib.sha256(canonical(recipe).encode()).hexdigest()[:32]
            plan_key = hashlib.sha256(canonical([
                story['photo_sha256'], story['_identity_generation'], identity['candidate_id'], recipe]).encode()).hexdigest()
            history = research.setdefault('fact_research_continuations', {})
            if plan_key in history:
                return False
            if scope == next_scope:
                return False
            revision = int(research.get('fact_request_revision') or 0) + 1
            request = {
                'voice_session_ids': [], 'live_transcript': next_goal,
                'input_revision': 'model-facts-' + plan_key[:40],
                'confirmed_candidate_id': identity['candidate_id'], 'coverage_goal': next_goal,
                'research_query': query, 'extraction_scope': next_scope, 'request_revision': revision,
                'identity_generation': story['_identity_generation'], 'photo_sha256': story['photo_sha256'],
            }
            pending = research.get('pending_fact_request')
            if isinstance(pending, dict):
                # Preserve already accepted owner requests ahead of this suggestion.
                queued = list(pending.get('queued_requests') or [])
                if any(item.get('input_revision') == request['input_revision'] for item in [pending, *queued]):
                    return False
                pending['queued_requests'] = [*queued, request]
            else:
                research['pending_fact_request'] = request
            history[plan_key] = {'source_run_id': run_id, 'query': query, 'goal': next_goal,
                                 'extraction_scope': next_scope, 'request_revision': revision,
                                 'identity_generation': story['_identity_generation'],
                                 'photo_sha256': story['photo_sha256'], 'created_at': self.service.store.now()}
            research['fact_request_revision'] = revision
            db.execute('UPDATE stories SET research_json=?,updated_at=? WHERE id=?',
                       (canonical(research), self.service.store.now(), story['id']))
            return True

    def _capacity_phase(self, job, value):
        with self.service.store.tx() as db:
            db.execute('INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(job_id,stage) DO UPDATE SET value_json=excluded.value_json',
                       (job['id'], 'headless_fact_capacity_wait', canonical(value), self.service.store.now()))

    def _capacity_retry(self, job, run_id, failures, due, committed):
        """Back off repeated local admission polling, never actual model quota."""
        key = 'headless_fact_capacity_wait'
        if committed:
            self._capacity_phase(job, {'streak': 0})
            return due
        codes = [getattr(error, 'route_failures', []) for error in failures]
        capacity = {'RESOURCE_NO_CAPACITY', 'RESOURCE_CAPACITY', 'RESOURCE_TOKEN_BUDGET', 'RESOURCE_START_BUDGET'}
        if not codes or any(not reasons or not set(reasons).issubset(capacity) for reasons in codes):
            return due
        previous = self.service.store.checkpoint_get(job['id'], key) or {}
        streak = int(previous.get('streak') or 0) + 1
        delay = min(60, 5 * 2 ** min(streak-1, 4))
        self._capacity_phase(job, {'streak': streak, 'delay_seconds': delay})
        retry_at = max(due or 0, self.service.store.now()+delay)
        LOG.info('street_story_fact_capacity_wait story_id=%s run_id=%s job_id=%s streak=%s delay_seconds=%s',
                 job['story_id'], run_id, job['id'], streak, delay)
        return retry_at

    async def _review_candidates(self, job, run_id, control_revision):
        from .headless_fact_review import HeadlessFactReview
        engine = HeadlessFactReview(self)
        if not engine._qualified_routes(available=False):
            return 0
        return await engine.run(job, run_id, control_revision)

    async def run(self, job, run_id, goal, scope):
        snapshot = self._snapshot(job, run_id)
        if snapshot is None or snapshot[2]['state'] == 'completed':
            return
        story, research, _ = snapshot
        control_revision = story['_fact_research_control_revision']
        provider = getattr(self.service.providers, 'research', None)
        if not callable(getattr(provider, 'extract_fact_page', None)):
            raise RetryableProviderError('research_fact_executor_unavailable', retry_at=self.service.store.now() + 60)
        with self.service.store.connection() as db:
            sources = [dict(row) for row in db.execute(
                'SELECT * FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url', (run_id,))]
        search_receipt = {}
        if not sources:
            # Attach acquisition hints before requiring an external discovery.
            # The reader still checks article subject/content and reuses only
            # exact frozen version/scope checkpoints; URL familiarity is no verdict.
            with self.service.store.connection() as db:
                keys = memory_keys(db, research['visual_identity'])
                placeholders = ','.join('?' for _ in keys)
                remembered = list(db.execute(
                    f'SELECT url,title,last_seen_at AS seen FROM poi_research_sources WHERE poi_key IN ({placeholders}) '
                    'UNION ALL SELECT s.url,s.title,s.updated_at AS seen FROM research_run_sources s '
                    f'JOIN research_runs r ON r.run_id=s.run_id WHERE r.poi_key IN ({placeholders}) '
                    'ORDER BY seen DESC,url', (*keys, *keys))) if keys else []
                by_url = {}
                for row in remembered:
                    by_url.setdefault(row['url'], {'url': row['url'], 'title': row['title']})
                sources = list(by_url.values())
            if sources:
                search_receipt = {'backend': 'poi_memory'}
                LOG.info('street_story_fact_sources_reused story_id=%s run_id=%s sources=%s',
                         story['id'], run_id, len(sources))
        if not sources:
            search = getattr(provider, 'search_fact_articles', None)
            if not callable(search):
                search = getattr(provider, 'search_articles', None)
            if not callable(search):
                raise RetryableProviderError('research_search_unavailable', retry_at=self.service.store.now() + 60)
            identity = research['visual_identity']
            payload = json.loads(job.get('payload_json') or '{}')
            query = str(payload.get('research_query') or ' '.join(
                str(value or '').strip() for value in (identity.get('candidate_name'), goal))).strip()
            if self._snapshot(job, run_id, control_revision) is None:
                return
            found = await search(query, story)
            snapshot = self._snapshot(job, run_id, control_revision)
            if snapshot is None:
                return
            sources = [source for source in found.get('sources') or [] if isinstance(source, dict)]
            search_receipt = found.get('receipt') or {}
        if not self._bind_discovery(job, run_id, goal, scope, sources, search_receipt, control_revision):
            return
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        story, research, _ = snapshot
        # Hydrate the complete eligible POI ledger before freezing the source
        # recipe revision; hydration itself must not stale a page already sent.
        with self.service.store.tx() as db:
            self.service._hydrate_poi_memory(db, self.service._story_row(db, story['id']))
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        story, research, _ = snapshot
        configured_model = getattr(getattr(provider, 'client', None), 'model_id', None) or 'unknown'
        units = await self._prepare_units(job, run_id, configured_model, control_revision)
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        if not units:
            await self._review_candidates(job, run_id, control_revision)
            with self.service.store.connection() as db:
                complete = manifest_complete(run_manifest(db, run_id))
                unreviewed = db.execute("SELECT 1 FROM fact_assertions a JOIN fact_observations o "
                    "ON o.story_id=a.story_id AND o.assertion_id=a.assertion_id "
                    "WHERE a.story_id=? AND o.run_id=? AND a.eligibility='unreviewed' LIMIT 1",
                    (story['id'], run_id)).fetchone()
            if complete and unreviewed:
                # Keep a durable retry while the backend verifier owns this
                # scope. A closed client must never be required to resume it.
                from .headless_fact_review import HeadlessFactReview
                background_review = bool(HeadlessFactReview(self)._qualified_routes(available=False))
                with self.service.store.tx() as db:
                    if self._snapshot(job, run_id, control_revision) is not None:
                        set_run_state(db, run_id, 'verifying', detail=('research_fact_review_partial'
                                      if background_review else 'awaiting_live_semantic_review'),
                                      now=self.service.store.now(), completed=False)
                if background_review:
                    self._partial(run_id, 'research_fact_review_partial', retry_at=self.service.store.now()+60)
                return
            if complete and not unreviewed:
                payload = json.loads(job.get('payload_json') or '{}')
                cached_only = any(item.get('research_run_id') == run_id and item.get('search_provider') == 'poi_memory'
                                  for item in snapshot[1].get('live_web_searches') or [] if isinstance(item, dict))
                if snapshot[2]['status_detail'] == 'research_fact_discovery_pending' or (payload.get('research_query') and cached_only):
                    await self._discover_requested_gap(job, run_id, goal, scope, provider, control_revision)
                return
            self._partial(run_id, 'research_fact_source_coverage_partial',
                          retry_at=self._pending_retry_at(job, run_id))
        story, research, _ = snapshot
        inventory = self.adapter._get_facts(story['id'], {'eligibility': 'all', 'limit': 50})
        # A model window is not a ledger coverage limit. The provider owns the
        # frozen authorized paging tool; this private handoff must be removed
        # from the public context before constructing its initial prompt.
        complete_inventory = list(inventory['facts'])
        cursor = inventory['next_cursor']
        while cursor is not None:
            extra = self.adapter._get_facts(story['id'], {'eligibility': 'all', 'limit': 50, 'cursor': cursor})
            complete_inventory.extend(extra['facts'])
            cursor = extra['next_cursor']
        with self.service.store.connection() as db:
            poi_keys = memory_keys(db, research['visual_identity'])
            placeholders = ','.join('?' for _ in poi_keys)
            prior_window = prior_facts(db, research['visual_identity'], story['id'])
            source_window = processed_sources(db, research['visual_identity'])
            prior_ids = {row[0] for row in db.execute(
                f"SELECT DISTINCT assertion_id FROM poi_research_assertions WHERE poi_key IN ({placeholders}) "
                "AND eligibility<>'withheld' AND review_status<>'quarantined'", tuple(poi_keys))}
            source_urls = {row[0] for row in db.execute(
                f'SELECT DISTINCT url FROM poi_research_sources WHERE poi_key IN ({placeholders})', tuple(poi_keys))}
            source_urls.update(row[0] for row in db.execute(
                'SELECT DISTINCT s.url FROM research_run_sources s JOIN research_runs r ON r.run_id=s.run_id '
                f'WHERE r.poi_key IN ({placeholders})', tuple(poi_keys)))
            context = {
                'confirmed_identity': research['visual_identity'], 'coverage_goal': goal, 'extraction_scope': scope,
                'known_facts': inventory['facts'], 'known_inventory_complete': not inventory['has_more'],
                'known_inventory_next_cursor': inventory['next_cursor'],
                'known_inventory_total': len(complete_inventory),
                'known_inventory_omitted_count': len(complete_inventory) - len(inventory['facts']),
                '_known_fact_inventory': complete_inventory,
                'prior_poi_facts': prior_window,
                'prior_poi_ledger_omitted_count': len(prior_ids - {fact['fact_id'] for fact in prior_window}),
                'previously_processed_sources': source_window,
                'previously_processed_sources_omitted_count': len(source_urls - {source['url'] for source in source_window}),
            }
        suggestions, failures = [], []
        review_task = None
        tasks = [asyncio.create_task(self._extract_unit(unit, provider, story, context, job)) for unit in units]
        try:
            for ready in asyncio.as_completed(tasks):
                unit, extracted, error = await ready
                if self._snapshot(job, run_id, control_revision) is None:
                    continue
                if error is not None:
                    failures.append(error)
                    LOG.warning('street_story_headless_fact_unit_partial story_id=%s run_id=%s chunk_id=%s reason=%s',
                                story['id'], run_id, unit['page']['chunk_id'], type(error).__name__)
                    continue
                try:
                    committed = await self._commit_unit(unit, extracted, job, run_id, goal, scope, control_revision)
                    if committed:
                        suggestions.append(extracted['result'])
                        if review_task is None or review_task.done():
                            if review_task is not None:
                                await review_task
                            review_task = asyncio.create_task(self._review_candidates(job, run_id, control_revision))
                except (ConflictError, MalformedProviderResponse) as exc:
                    LOG.info('street_story_headless_fact_commit_deferred story_id=%s run_id=%s chunk_id=%s reason=%s',
                             story['id'], run_id, unit['page']['chunk_id'], getattr(exc, 'code', None) or type(exc).__name__)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if review_task is not None:
                await review_task
        await self._review_candidates(job, run_id, control_revision)
        if self._snapshot(job, run_id, control_revision) is not None:
            for result in suggestions:
                self._queue_model_continuation(job, run_id, goal, scope, result, control_revision)
            with self.service.store.tx() as db:
                unfinished = [row[0] for row in db.execute("SELECT chunk_id FROM research_chunk_runs WHERE run_id=? "
                    "AND status NOT IN ('extracted','no_claims')", (run_id,))]
                pending = bool(unfinished)
                unread = db.execute("SELECT 1 FROM research_run_sources WHERE run_id=? "
                    "AND source_version_id IS NULL AND status!='failed' LIMIT 1", (run_id,)).fetchone()
                unreviewed = bool(review_packets.pending_candidates(db, story['id'], run_id))
                set_run_state(db, run_id, 'partial' if pending or unread else 'verifying',
                              detail=('research_fact_units_partial' if pending or unread else
                                      'research_fact_review_partial' if unreviewed else 'source_batches_reviewed'),
                              now=self.service.store.now(), completed=False)
            if pending or unread:
                unvisited = set(unfinished) - {unit['page']['chunk_id'] for unit in units}
                due = self._pending_retry_at(job, run_id) if len(failures) == len(units) else None
                due = self._capacity_retry(job, run_id, failures, due, bool(suggestions))
                self._partial(run_id, 'research_fact_next_page' if unread or unvisited
                              else 'research_fact_source_coverage_partial', retry_at=due)
            if unreviewed:
                from .headless_fact_review import HeadlessFactReview
                if HeadlessFactReview(self)._qualified_routes(available=False):
                    self._partial(run_id, 'research_fact_review_partial', retry_at=self.service.store.now()+60)

    def _owner_fence(self, db, story, research):
        """Candidate additions may change revision, never these author inputs."""
        selected = [row[0] for row in db.execute(
            'SELECT assertion_id FROM fact_assertions WHERE story_id=? AND owner_selected=1 ORDER BY assertion_id',
            (story['id'],))]
        voices = [list(row) for row in db.execute('SELECT session_id,recording_finished,transcript FROM voice_sessions '
                                                'WHERE story_id=? ORDER BY session_id', (story['id'],))]
        return {'photo': story['photo_sha256'], 'generation': story['_identity_generation'],
                'controls': research.get('research_controls'), 'input_revision': research.get('input_revision'),
                'fact_request_revision': research.get('fact_request_revision'), 'transcript': research.get('transcript'),
                'latitude': story['latitude'], 'longitude': story['longitude'], 'draft': story['draft_text'],
                'concept': research.get('publication_concept'), 'selected': selected, 'voices': voices}

    def _unit_phase(self, job, unit_id, phase, **detail):
        with self.service.store.tx() as db:
            db.execute('INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(job_id,stage) DO UPDATE SET value_json=excluded.value_json',
                       (job['id'], 'headless_fact_unit:' + unit_id, canonical({'phase': phase, **detail}),
                        self.service.store.now()))

    def _boundary_closed(self, story, unit_id):
        """Provider attempt receipts are authoritative about a prior send."""
        with self.service.store.connection() as db:
            attempts = list(db.execute("SELECT logical_id,role,receipt_json FROM research_provider_attempts "
                                       "WHERE story_id=? AND role LIKE 'facts%' ORDER BY created_at DESC,rowid DESC",
                                       (story['id'],)))
        latest = {}
        for row in attempts:
            expected = hashlib.sha256(canonical([story['id'], story['photo_sha256'],
                story['_identity_generation'], row['role'], unit_id]).encode()).hexdigest()
            if row['logical_id'] == expected:
                latest.setdefault(expected, json.loads(row['receipt_json']))
        return bool(latest) and all(
            receipt.get('phase') in {'created', 'completed'}
            or receipt.get('phase') == 'failed' and (receipt.get('provider_send_state') in {'not_sent', 'response_closed'}
                                                    or receipt.get('retry_safe') is True)
            or receipt.get('phase') == 'aborted' and receipt.get('abort_acknowledged') is True
            for receipt in latest.values())

    async def _prepare_units(self, job, run_id, model, control_revision):
        units, visited, prepared = [], set(), set()
        while len(units) < 3:
            # Frozen reads can complete without an async suspension. A backlog
            # of fenced pages must still let Live sockets receive/ACK speech
            # between page preparations, even when no model route is ready.
            await asyncio.sleep(0)
            snapshot = self._snapshot(job, run_id, control_revision)
            if snapshot is None:
                break
            story, research, _ = snapshot
            with self.service.store.connection() as db:
                rows = list(db.execute("SELECT r.chunk_id FROM research_chunk_runs r "
                    "JOIN source_chunks c ON c.chunk_id=r.chunk_id WHERE r.run_id=? "
                    "AND r.status NOT IN ('extracted','no_claims') ORDER BY c.source_version_id,c.ordinal", (run_id,)))
                candidate = next((row for row in rows if row['chunk_id'] not in visited), None)
                source = None if candidate else db.execute("SELECT url FROM research_run_sources WHERE run_id=? "
                    "AND source_version_id IS NULL AND status!='failed' ORDER BY discovered_at,url LIMIT 1", (run_id,)).fetchone()
                owner = self._owner_fence(db, story, research)
            if candidate is None and source is None:
                break
            key = candidate['chunk_id'] if candidate else source['url']
            if key in visited:
                break
            visited.add(key)
            session = SimpleNamespace(id=f"headless-facts:{job['id']}:{key}", resource_id=story['id'], closed=False,
                model=model, state={'research_run_id': run_id, 'fact_research_control_revision': control_revision,
                                    'live_first_research': True, 'headless_research': True})
            args = {'run_id': run_id, **({'chunk_id': key} if candidate else {'source_ref': _search_source_ref(key)})}
            try:
                page = await self.adapter._get_research_chunk(session, args)
            except (ConflictError, RetryableProviderError) as exc:
                LOG.info('street_story_headless_fact_prepare_deferred story_id=%s run_id=%s reason=%s',
                         story['id'], run_id, getattr(exc, 'code', None) or type(exc).__name__)
                continue
            if page.get('all_chunks_processed') or not page.get('chunk_id'):
                continue
            if page['chunk_id'] in prepared:
                continue
            prepared.add(page['chunk_id'])
            visited.add(page['chunk_id'])
            # The source is fetched serially, then its actual frozen core owns the task.
            chunk_owner = f"headless-facts:{job['id']}:{page['chunk_id']}"
            if session.id != chunk_owner:
                with self.service.store.tx() as db:
                    db.execute('UPDATE research_chunk_runs SET lease_owner=? WHERE run_id=? AND chunk_id=? '
                               'AND lease_owner=? AND lease_fence=?',
                               (chunk_owner, run_id, page['chunk_id'], session.id,
                                session.state['research_chunk_leases'][page['chunk_id']]))
                session.id = chunk_owner
            identity = [run_id, page['source_version_id'], page['chunk_id'], page['batch_index'],
                        [passage['passage_id'] for passage in page['evidence_passages']],
                        snapshot[2]['extraction_scope']]
            page['_unit_id'] = 'factpage_' + hashlib.sha256(canonical(identity).encode()).hexdigest()[:24]
            old = self.service.store.checkpoint_get(job['id'], 'headless_fact_unit:' + page['_unit_id']) or {}
            saved = self.service.store.checkpoint_get(job['id'], 'headless_fact_result:' + page['_unit_id'])
            due = old.get('retry_at')
            if not saved and isinstance(due, (int, float)) and due > self.service.store.now():
                continue  # This exact unit remains fenced until its existing route deadline.
            if not saved and old.get('phase') in {'started', 'unknown'} and not self._boundary_closed(story, page['_unit_id']):
                self._unit_phase(job, page['_unit_id'], 'unknown', chunk_id=page['chunk_id'],
                                 retry_at=self.service.store.now()+300)
                continue
            if saved and saved['owner'] != owner:
                old_owner = saved['owner']
                current_control = (owner.get('controls') or {}).get('facts') or {}
                old_control = (old_owner.get('controls') or {}).get('facts') or {}
                same_inputs = {k: v for k, v in old_owner.items() if k != 'controls'} == {k: v for k, v in owner.items() if k != 'controls'}
                explicitly_resumed = (current_control.get('stopped') is False
                    and current_control.get('resumed_at', 0) > old_control.get('resumed_at', 0))
                if not same_inputs or not explicitly_resumed:
                    self._unit_phase(job, page['_unit_id'], 'deferred', chunk_id=page['chunk_id'])
                    continue
                # A new claimed Resume may import the known result as unreviewed
                # candidates. The interrupted attempt never commits or sends again.
                saved = {**saved, 'owner': owner}
                self.service.store.checkpoint_put(job['id'], 'headless_fact_result:' + page['_unit_id'], saved)
            units.append({'page': page, 'session': session, 'owner': saved['owner'] if saved else owner,
                          'saved': saved})
        with self.service.store.connection() as db:
            ordered = [row[0] for row in db.execute(
                "SELECT r.chunk_id FROM research_chunk_runs r JOIN source_chunks c ON c.chunk_id=r.chunk_id "
                "JOIN research_run_sources s ON s.run_id=r.run_id AND s.source_version_id=c.source_version_id "
                "WHERE r.run_id=? GROUP BY r.chunk_id ORDER BY MIN(s.discovered_at),MIN(s.url),c.ordinal", (run_id,))]
        for unit in units:
            unit['page']['_extractor_ordinal'] = ordered.index(unit['page']['chunk_id'])
        return units

    async def _extract_unit(self, unit, provider, story, context, job):
        page = unit['page']
        if unit['saved']:
            return unit, unit['saved']['extracted'], None
        self._unit_phase(job, page['_unit_id'], 'started', chunk_id=page['chunk_id'])
        try:
            extracted = await provider.extract_fact_page(page, dict(story), dict(context))
            if not isinstance(extracted, dict):
                raise MalformedProviderResponse('research_fact_page_contract_invalid')
            result = extracted.get('result')
            if (not isinstance(result, dict) or not isinstance(result.get('facts'), list)
                    or any(type(result.get(flag)) is not bool for flag in
                           ('source_matches_poi', 'source_content_valid', 'continuation_needed'))):
                raise MalformedProviderResponse('research_fact_page_contract_invalid')
            if any(not isinstance(fact, dict) for fact in result['facts']):
                raise MalformedProviderResponse('research_fact_page_claim_invalid')
            self.service.store.checkpoint_put(job['id'], 'headless_fact_result:' + page['_unit_id'],
                {'extracted': extracted, 'owner': unit['owner']})
            self._unit_phase(job, page['_unit_id'], 'result', chunk_id=page['chunk_id'])
            return unit, extracted, None
        except asyncio.CancelledError:
            self._unit_phase(job, page['_unit_id'], 'unknown', chunk_id=page['chunk_id'],
                                 retry_at=self.service.store.now()+300)
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            known = isinstance(exc, MalformedProviderResponse) or self._boundary_closed(story, page['_unit_id'])
            self._unit_phase(job, page['_unit_id'], 'closed_error' if known else 'unknown',
                             chunk_id=page['chunk_id'], error_type=type(exc).__name__,
                             error_code=type(exc).__name__,
                             retry_at=getattr(exc, 'retry_at', None) or (None if known else self.service.store.now()+300))
            return unit, None, exc

    async def _commit_unit(self, unit, extracted, job, run_id, goal, scope, control_revision):
        from .research_runs import chunk_lease_owned
        page, session = unit['page'], unit['session']
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        story, research, _ = snapshot
        with self.service.store.connection() as db:
            self.adapter._research_run_guard(db, session, run_id)
            if (unit['owner'] != self._owner_fence(db, story, research)
                    or not chunk_lease_owned(db, run_id=run_id, chunk_id=page['chunk_id'], owner=session.id,
                        fence=session.state['research_chunk_leases'][page['chunk_id']], now=self.service.store.now())):
                raise ConflictError('live_research_save_stale', 'Owner inputs or the extraction lease changed; saved result is deferred.')
        # Only candidate additions can rebase the frozen recipe while owner inputs remain identical.
        session.state['research_chunk_receipts'][page['chunk_id']]['expected_story_revision'] = int(story['revision'])
        result = extracted['result']
        claims = result['facts'] if result['source_matches_poi'] and result['source_content_valid'] else []
        if any(not isinstance(fact, dict) for fact in claims):
            raise MalformedProviderResponse('research_fact_page_claim_invalid')
        receipt = extracted.get('receipt') or {}
        actual_models = [usage.get('model_id') for usage in receipt.get('assistants') or []
                         if isinstance(usage, dict) and usage.get('model_id')]
        session.model = str(actual_models[-1] if actual_models else receipt.get('model_id') or session.model)
        committed = await self.adapter._save_research_facts(session, page['_unit_id'], {
            'facts': [{**fact, 'selected': False} for fact in claims], 'batch_reviewed': False,
            'extractor_candidates': True, 'inventory_reviewed': False,
            'source_matches_poi': result['source_matches_poi'], 'source_content_valid': result['source_content_valid'],
            'continuation_needed': result['continuation_needed'],
        })
        self._unit_phase(job, page['_unit_id'], 'committed', chunk_id=page['chunk_id'])
        record_identity_event(self.service, story['id'], 'fact_background_batch', {
            'generation': story['_identity_generation'], 'run_id': run_id, 'source_version_id': page['source_version_id'],
            'chunk_id': page['chunk_id'], 'batch_index': page['batch_index'], 'unit_id': page['_unit_id'],
            'model_id': session.model, 'provider_id': receipt.get('provider_id', 'unknown'),
            'backend': receipt.get('backend', 'unknown'), 'audit': committed.get('save_research_audit') or {},
            'candidate_only': True,
        }, source='fact_research')
        LOG.info('street_story_headless_fact_candidate_saved story_id=%s run_id=%s chunk_id=%s batch=%s',
                 story['id'], run_id, page['chunk_id'], page['batch_index'])
        return committed
