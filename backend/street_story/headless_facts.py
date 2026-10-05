"""Bounded fact pages through the existing frozen Live research intake."""
from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace

from .errors import MalformedProviderResponse, RetryableProviderError
from .identity_telemetry import record_identity_event
from .live import StreetStoryLiveAdapter, _search_source_ref
from .poi_memory import memory_keys, prior_facts, processed_sources
from .research_control import research_stopped
from .research_runs import manifest_complete, register_discovered_source, run_manifest, set_run_state
from .service import ConflictError, canonical

LOG = logging.getLogger(__name__)


class HeadlessFacts:
    """One logical attempt processes one page; saved cursors survive restart."""

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
            if (run is None or run['state'] in {'cancelled', 'failed'}
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

    def _partial(self, run_id, reason):
        with self.service.store.tx() as db:
            run = db.execute('SELECT state FROM research_runs WHERE run_id=?', (run_id,)).fetchone()
            if run and run['state'] not in {'cancelled', 'failed'}:
                set_run_state(db, run_id, 'partial', detail=reason, now=self.service.store.now())
        delay = 300 if reason in {'research_fact_source_coverage_partial', 'research_fact_source_unreadable'} else 10
        raise RetryableProviderError(reason, retry_at=self.service.store.now() + delay)

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
        session = SimpleNamespace(id=f"headless-facts:{job['id']}", resource_id=story['id'], closed=False,
                                  model=configured_model, state={'research_run_id': run_id,
                                                               'fact_research_control_revision': control_revision,
                                                               'live_first_research': True, 'headless_research': True})
        with self.service.store.connection() as db:
            pending_chunk = db.execute("SELECT 1 FROM research_chunk_runs WHERE run_id=? "
                                       "AND status NOT IN ('extracted','no_claims') LIMIT 1", (run_id,)).fetchone()
            source = None if pending_chunk else db.execute('SELECT url FROM research_run_sources WHERE run_id=? '
                "AND source_version_id IS NULL AND status!='failed' ORDER BY discovered_at,url LIMIT 1", (run_id,)).fetchone()
        args = {'run_id': run_id}
        # This is a bounded acquisition order, never a semantic competence rank.
        # The model must check the chosen article's subject before importing it.
        if source:
            args['source_ref'] = _search_source_ref(source['url'])
        discovery_pending = snapshot[2]['status_detail'] == 'research_fact_discovery_pending'
        try:
            page = await self.adapter._get_research_chunk(session, args)
        except ConflictError as exc:
            if self._snapshot(job, run_id, control_revision) is None:
                return
            if exc.code == 'live_research_run_stale':
                self._partial(run_id, 'research_fact_owner_revision_changed')
            raise
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        if page.get('all_chunks_processed'):
            with self.service.store.connection() as db:
                complete = manifest_complete(run_manifest(db, run_id))
            if page.get('completed') and complete:
                payload = json.loads(job.get('payload_json') or '{}')
                requested_query = str(payload.get('research_query') or '').strip()
                cached_only = any(item.get('research_run_id') == run_id and item.get('search_provider') == 'poi_memory'
                                  for item in snapshot[1].get('live_web_searches') or [] if isinstance(item, dict))
                # Complete checked extraction satisfies this scope on the known
                # pages, but cannot silently consume an explicit new query. No
                # model page runs in the skip path to make another recommendation.
                if discovery_pending or (requested_query and cached_only):
                    await self._discover_requested_gap(job, run_id, goal, scope, provider, control_revision)
                return
            self._partial(run_id, 'research_fact_source_coverage_partial')
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
        unit = [run_id, page['source_version_id'], page['chunk_id'], page['batch_index'],
                [passage['passage_id'] for passage in page['evidence_passages']], scope]
        page['_unit_id'] = 'factpage_' + hashlib.sha256(canonical(unit).encode()).hexdigest()[:24]
        with self.service.store.connection() as db:
            self.adapter._research_run_guard(db, session, run_id)
        extracted = await provider.extract_fact_page(page, story, context)
        if self._snapshot(job, run_id, control_revision) is None:
            return
        result = extracted.get('result')
        if (not isinstance(result, dict) or not isinstance(result.get('facts'), list)
                or any(type(result.get(flag)) is not bool for flag in ('source_matches_poi', 'source_content_valid', 'continuation_needed'))):
            raise MalformedProviderResponse('research_fact_page_contract_invalid')
        claims = result['facts'] if result['source_matches_poi'] and result['source_content_valid'] else []
        facts = []
        for fact in claims:
            if not isinstance(fact, dict):
                raise MalformedProviderResponse('research_fact_page_claim_invalid')
            facts.append({**fact, 'selected': False})
        receipt = extracted.get('receipt') or {}
        actual_models = [usage.get('model_id') for usage in receipt.get('assistants') or []
                         if isinstance(usage, dict) and usage.get('model_id')]
        session.model = str(actual_models[-1] if actual_models else receipt.get('model_id') or configured_model)
        save_args = {
            'facts': facts, 'batch_reviewed': True, 'inventory_reviewed': False,
            'source_matches_poi': result['source_matches_poi'], 'source_content_valid': result['source_content_valid'],
            'continuation_needed': result['continuation_needed'],
        }
        try:
            committed = await self.adapter._save_research_facts(session, page['_unit_id'], save_args)
        except ConflictError as exc:
            if self._snapshot(job, run_id, control_revision) is None:
                return
            if exc.code == 'live_research_save_stale':
                self._partial(run_id, 'research_fact_owner_revision_changed')
            raise
        continued = self._queue_model_continuation(job, run_id, goal, scope, result, control_revision)
        record_identity_event(self.service, story['id'], 'fact_background_batch', {
            'generation': story['_identity_generation'], 'run_id': run_id, 'source_version_id': page['source_version_id'],
            'chunk_id': page['chunk_id'], 'batch_index': page['batch_index'], 'unit_id': page['_unit_id'],
            'model_id': session.model, 'provider_id': receipt.get('provider_id', 'unknown'),
            'backend': receipt.get('backend', 'unknown'), 'usage': receipt.get('usage', 'unknown'),
            'assistants': [{key: usage.get(key, 'unknown') for key in ('provider_id', 'model_id', 'tokens', 'time', 'cost')}
                           for usage in receipt.get('assistants') or [] if isinstance(usage, dict)],
            'cost': receipt.get('cost', 'unknown'), 'audit': committed.get('save_research_audit') or {},
            'model_research_continuation_queued': continued,
        }, source='fact_research')
        LOG.info('street_story_headless_fact_batch story_id=%s run_id=%s chunk_id=%s batch=%s completed=%s',
                 story['id'], run_id, page['chunk_id'], page['batch_index'], committed.get('completed'))
        if not committed.get('completed'):
            self._partial(run_id, 'research_fact_next_page' if result['source_content_valid'] else 'research_fact_source_unreadable')
        elif (result.get('research_sufficient') is False and result['source_matches_poi']
              and result['source_content_valid'] and not continued):
            # A model can request the current explicit discovery recipe after
            # exhausting remembered pages. Cache-first must not become cache-only.
            payload = json.loads(job.get('payload_json') or '{}')
            query = str(payload.get('research_query') or '').strip()
            def normalize(value):
                return ' '.join(str(value or '').split()).casefold()
            with self.service.store.connection() as db:
                latest = json.loads(self.service._story_row(db, story['id'])['research_json'])
                cached_only = any(item.get('research_run_id') == run_id and item.get('search_provider') == 'poi_memory'
                                  for item in latest.get('live_web_searches') or [] if isinstance(item, dict))
            if (query and cached_only and normalize(result.get('next_research_query')) == normalize(query)
                    and normalize(result.get('next_research_goal')) == normalize(goal)):
                await self._discover_requested_gap(job, run_id, goal, scope, provider, control_revision)
