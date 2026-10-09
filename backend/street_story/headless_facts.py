"""Bounded fact pages through the existing frozen Live research intake."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from types import SimpleNamespace

from . import review_packets
from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .identity_telemetry import record_identity_event
from .identity_proof import accepted_identity
from .identity_model_context import compact_physical_identity
from .live import StreetStoryLiveAdapter, _search_source_ref
from .poi_memory import memory_keys, prior_facts, processed_sources
from .research_control import research_stopped
from .research_runs import (
    manifest_complete,
    manifest_exhausted,
    mark_chunk,
    register_discovered_source,
    run_manifest,
    set_run_state,
)
from .service import ConflictError, canonical

LOG = logging.getLogger(__name__)


def reviewed_reference_articles(identity):
    """Recover only article leads bound to an actually reviewed image."""
    if identity.get('status') != 'match' or identity.get('visual_reference_verified') is not True:
        return {}
    from .article_media import public_url
    from .identity_subject_binding import subject_aliases
    aliases = subject_aliases(identity.get('candidates') or []).get(
        identity.get('candidate_id'), {identity.get('candidate_id')})
    articles = {}
    mappings = (identity.get('provider_receipt') or {}).get('reference_mapping') or []
    for evidence in identity.get('reference_evidence') or []:
        if (evidence.get('subject_candidate_id', evidence.get('candidate_id')) not in aliases
                or not evidence.get('reference_id')):
            continue
        url = public_url(str(evidence.get('article_url') or ''))
        if not url:
            # Initial direct-image comparisons retain the page in
            # the frozen mapping rather than reference_evidence.
            # Join only the actual reviewed image and candidate;
            # the other discovered pages remain unselected leads.
            image = public_url(str(evidence.get('image_url') or ''))
            mapped = next((entry for entry in mappings if isinstance(entry, dict)
                and entry.get('reference_id') == evidence['reference_id']
                and entry.get('candidate_id') in aliases
                and image and public_url(str(entry.get('source_url') or '')) == image), {})
            url = public_url(str(mapped.get('article_url') or ''))
        if url:
            articles.setdefault(url, {'url': url, 'title': identity.get('candidate_name') or url})
    return articles


def acquired_subject_articles(identity, research):
    """Existing text leads for the accepted physical subject, never image proof.

    A selected nearby page, proximity or the page title alone does not bind its
    subject. Exact mapped links and current explicit subject addresses schedule
    the normal frozen reader; its semantic subject/fact review remains required.
    """
    revision = int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)
    if not accepted_identity(identity, generation=int(research.get('identity_generation') or 0), control_revision=revision):
        return {}
    photo, generation = identity.get('photo_sha256'), identity.get('generation')
    if not photo or generation != int(research.get('identity_generation') or 0):
        return {}
    from .article_media import public_url
    from .identity_subject_binding import subject_aliases
    aliases = subject_aliases(identity.get('candidates') or []).get(
        identity.get('candidate_id'), {identity.get('candidate_id')})
    articles = {}
    def lead(item, subject_ids, *, provenance=None):
        url = public_url(str(item.get('url') or ''))
        if url and aliases.intersection(subject_ids):
            articles.setdefault(url, {'url': url, 'title': str(item.get('title') or item.get('name') or url),
                'subject_candidate_ids': sorted(aliases.intersection(subject_ids)),
                'acquisition_kind': 'accepted_identity_subject_lead', 'visual_reference_verified': False,
                **(provenance or {})})
    for page in research.get('wikipedia') or []:
        if isinstance(page, dict):
            lead(page, {str(item.get('candidate_id') or '') for item in
                page.get('mapped_wikipedia_sources') or [] if isinstance(item, dict)})
    for candidate in identity.get('candidates') or []:
        if not isinstance(candidate, dict) or candidate.get('candidate_id') not in aliases:
            continue
        cid = candidate['candidate_id']
        urls = [candidate.get('wikipedia_url')]
        if str(cid).startswith('wiki:'):
            urls.append(candidate.get('url'))
        for url in urls:
            if url:
                lead({'url': url, 'title': candidate.get('name')}, {cid})
    for article in (identity.get('architectural_text_proof') or {}).get('article_sources') or []:
        if isinstance(article, dict):
            lead(article, {identity.get('candidate_id')})
    history = research.get('identity_article_discovery') or {}
    plan = history.get('search_plan') or {}
    if (history.get('photo_sha256') == photo and history.get('generation') == generation
            and plan.get('photo_sha256') == photo and plan.get('generation') == generation
            and plan.get('control_revision') == revision):
        for source in history.get('sources') or []:
            if not isinstance(source, dict):
                continue
            subjects = {str(source.get('subject_candidate_id') or source.get('physical_subject_candidate_id') or '')}
            subjects.update(source.get('physical_subject_candidate_ids') or [])
            subjects.update(source.get('memory_candidate_ids') or [])
            lead(source, subjects)
        pages = {f"wiki:{page.get('pageid')}": page for page in research.get('wikipedia') or [] if isinstance(page, dict)}
        payload = plan.get('payload') or {}
        for binding in payload.get('subject_article_bindings') or []:
            if (isinstance(binding, dict) and binding.get('physical_binding_resolved') is True
                    and str(binding.get('scope') or '').strip() and str(binding.get('binding_basis') or '').strip()
                    and binding.get('candidate_id') in aliases and binding.get('article_id') in pages):
                lead(pages[binding['article_id']], {binding['candidate_id']})
        # A joint call may accept geometry after reading a nominated article.
        # Keep that actual text acquisition as a lead independently of whether
        # it became an architectural-text identity proof. The normal reader
        # reuses the raw-byte cache and every claim still needs semantic review.
        text_receipt = payload.get('source_text_receipt') or {}
        if text_receipt.get('source_photo_sha256') == photo:
            for article in text_receipt.get('articles') or []:
                if not isinstance(article, dict):
                    continue
                text, digest = article.get('text'), article.get('source_sha256')
                if (article.get('input_kind') != 'acquired_article_text'
                        or article.get('raw_body_sha256_verified') is not True
                        or not isinstance(text, str) or not text.strip()
                        or not isinstance(digest, str) or len(digest) != 64
                        or any(char not in '0123456789abcdef' for char in digest)
                        or article.get('text_sha256') != hashlib.sha256(text.encode()).hexdigest()
                        or not str(article.get('scope') or '').strip()
                        or not str(article.get('binding_basis') or '').strip()):
                    continue
                lead(article, set(article.get('lookup_candidate_ids') or []), provenance={
                    'article_id': str(article.get('article_id') or ''),
                    'physical_scope': str(article['scope'])[:400],
                    'binding_basis': str(article['binding_basis'])[:400],
                    'source_sha256': digest, 'text_sha256': article['text_sha256']})
        # Initial geometry acceptance need not wait for article bodies. Only a
        # closed explicit selection of a card actually received in this scope
        # can schedule its ordinary reader; nearby/unselected cards stay leads
        # for identity discovery alone.
        catalogue = payload.get('regional_catalogue') or {}
        scope = catalogue.get('scope') or {}
        if (scope.get('photo_sha256') == photo and scope.get('generation') == generation
                and scope.get('control_revision') == revision):
            from jsonschema import Draft202012Validator
            from .identity_architectural_context import regional_selection_schema
            selections = payload.get('regional_article_selections') or []
            cards = {card['article_id']: card for card in reversed(catalogue.get('results') or [])
                     if isinstance(card, dict) and card.get('article_id')}
            received = {'results': list(cards.values())}
            if Draft202012Validator(regional_selection_schema(aliases, received)).is_valid(selections):
                for selection in selections:
                    if (selection['physical_binding_resolved'] is True and selection['scope'].strip()
                            and selection['binding_basis'].strip()):
                        card = cards[selection['article_id']]
                        lead({**card, 'url': card.get('canonical_url')}, {selection['candidate_id']}, provenance={
                            'article_id': selection['article_id'], 'physical_scope': selection['scope'],
                            'binding_basis': selection['binding_basis']})
    return articles


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
                    or not accepted_identity(identity, photo_sha256=story['photo_sha256'], generation=generation,
                        control_revision=int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0))
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

    def _finish(self, job, run_id, control_revision, reason):
        with self.service.store.tx() as db:
            snapshot = self._snapshot(job, run_id, control_revision)
            if snapshot is None:
                return None
            manifest = run_manifest(db, run_id)
            eligible = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                                  (job['story_id'],)).fetchone()[0]
            complete = (manifest_complete(manifest) and not manifest['counts']['sources_snippet_only']
                        and not review_packets.pending_candidates(db, job['story_id'], run_id))
            outcome = ('useful_complete' if complete else 'useful_partial') if eligible else 'no_supported_facts'
            value = {'outcome': outcome, 'reason': reason, 'coverage_complete': complete, 'eligible_count': eligible}
            set_run_state(db, run_id, 'completed', detail=outcome, now=self.service.store.now(), completed=True)
            pending = snapshot[1].get('pending_fact_request')
            joined = (isinstance(pending, dict) and pending.get('photo_sha256', snapshot[0]['photo_sha256']) == snapshot[0]['photo_sha256']
                      and pending.get('identity_generation', snapshot[0]['_identity_generation']) == snapshot[0]['_identity_generation'])
        if joined:
            return None  # Finish this scope; the existing joined request keeps the original envelope.
        self.service.store.checkpoint_put(job['id'], 'headless_fact_outcome:' + run_id, value)
        LOG.info('street_story_fact_research_terminal story_id=%s run_id=%s outcome=%s reason=%s coverage_complete=%s eligible=%s',
                 job['story_id'], run_id, outcome, reason, complete, eligible)
        return value

    def _unreviewed_actionable(self, job, run_id):
        from .headless_fact_review import HeadlessFactReview
        engine = HeadlessFactReview(self)
        with self.service.store.connection() as db:
            pending = review_packets.pending_candidates(db, job['story_id'], run_id)
        exhausted = engine.exhausted_candidates(job)
        return any(fid not in exhausted for fid in pending)

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
        return self._finish(job, run_id, control_revision,
                            'search_exhausted' if not added else 'source_batches_reviewed')

    def _queue_model_continuation(self, job, run_id, goal, scope, result, control_revision):
        """Join an explicit model-owned new aspect after the current run finishes.

        Page continuation describes unread text; research sufficiency describes
        the publication goal. Neither is inferred from a server fact count.
        """
        if (result.get('research_sufficient') is not False or result.get('source_matches_poi') is not True
                or type(result.get('source_content_valid')) is not bool):
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

    def _handoff_rejected_source(self, job, run_id, goal, scope, control_revision):
        """Use a saved model query when the remaining material is rejected text.

        The old run stays partial, with its failed coverage evidence intact.
        The ordinary terminal worker hook can then run the joined query.
        """
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return False
        story = snapshot[0]
        with self.service.store.connection() as db:
            if (db.execute("SELECT 1 FROM research_chunk_runs WHERE run_id=? AND status NOT IN "
                           "('extracted','no_claims') AND COALESCE(error_code,'')<>'not_article_text' LIMIT 1",
                           (run_id,)).fetchone()
                    or db.execute('SELECT 1 FROM research_run_sources WHERE run_id=? AND source_version_id IS NULL LIMIT 1',
                                  (run_id,)).fetchone()
                    or review_packets.pending_candidates(db, story['id'], run_id)):
                return False
            rows = list(db.execute('SELECT stage,value_json FROM research_checkpoints WHERE job_id=?', (job['id'],)))
            owner = self._owner_fence(db, story, snapshot[1])
        saved = {row['stage']: json.loads(row['value_json']) for row in rows}
        for stage, value in saved.items():
            if (stage.startswith('headless_fact_unit:') and value.get('phase') in {'started', 'unknown'}
                    and not self._boundary_closed(story, stage.split(':', 1)[1])):
                return False
        for stage, value in saved.items():
            if not stage.startswith('headless_fact_result:'):
                continue
            unit = stage.split(':', 1)[1]
            result = (value.get('extracted') or {}).get('result') or {}
            if (saved.get('headless_fact_unit:' + unit, {}).get('phase') != 'committed'
                    or result.get('source_content_valid') is not False):
                continue
            research = snapshot[1]
            pending = research.get('pending_fact_request') or {}
            joined = [pending, *(pending.get('queued_requests') or [])]
            matching = [plan for key, plan in (research.get('fact_research_continuations') or {}).items()
                if plan.get('source_run_id') == run_id and plan.get('query') == result.get('next_research_query')
                and plan.get('goal') == result.get('next_research_goal')
                and any(item.get('input_revision') == 'model-facts-' + key[:40] for item in joined)]
            previous_owner = value.get('owner') or {}
            same_owner = previous_owner == owner or (
                {k: v for k, v in previous_owner.items() if k != 'fact_request_revision'}
                == {k: v for k, v in owner.items() if k != 'fact_request_revision'}
                and any(plan.get('request_revision') == owner.get('fact_request_revision') for plan in matching))
            if not same_owner:
                continue
            queued = self._queue_model_continuation(job, run_id, goal, scope, result, control_revision)
            if not queued:
                current = self._snapshot(job, run_id, control_revision)
                research = current[1] if current else {}
                pending = research.get('pending_fact_request') or {}
                joined = [pending, *(pending.get('queued_requests') or [])]
                plans = research.get('fact_research_continuations') or {}
                queued = any(plan.get('source_run_id') == run_id
                    and plan.get('query') == result.get('next_research_query')
                    and plan.get('goal') == result.get('next_research_goal')
                    and any(item.get('input_revision') == 'model-facts-' + key[:40] for item in joined)
                    for key, plan in plans.items())
            if not queued:
                continue
            with self.service.store.tx() as db:
                if self._snapshot(job, run_id, control_revision) is None:
                    return False
                set_run_state(db, run_id, 'partial', detail='research_rejected_source_replaced',
                              now=self.service.store.now(), completed=False)
            LOG.info('street_story_fact_rejected_source_handoff story_id=%s run_id=%s unit_id=%s',
                     story['id'], run_id, unit)
            return True
        return False

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

    def _review_retry_at(self, job, run_id, committed):
        """Continue ready packets promptly after progress, retaining blocked waits."""
        from .headless_fact_review import HeadlessFactReview
        now = self.service.store.now()
        if not committed:
            return now + 60
        engine = HeadlessFactReview(self)
        if not engine._qualified_routes():
            return now + 60
        with self.service.store.connection() as db:
            current = review_packets.bundle(db, job['story_id'])
            pending = set(review_packets.pending_candidates(db, job['story_id'], run_id))
            waiting = set()
            for row in db.execute(
                "SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage LIKE 'headless_fact_review:%'",
                (job['id'],)):
                saved = json.loads(row[0])
                if saved.get('retry_at', 0) <= now:
                    continue
                packet = db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=? AND story_id=?',
                                    (saved.get('packet_ref'), job['story_id'])).fetchone()
                if not packet:
                    waiting.update(pending)  # Unknown wait scope cannot authorize a fresh operation.
                    continue
                frozen = json.loads(packet[0]).get('bundle', {})
                waiting.update(fid for fid, digest in frozen.items() if current.get(fid) == digest)
        ready = pending - waiting - engine.exhausted_candidates(job) - engine._unknown_candidates(job, current)
        if not ready:
            return now + 60
        return now + 1

    async def run(self, job, run_id, goal, scope):
        snapshot = self._snapshot(job, run_id)
        if snapshot is None:
            return None
        if snapshot[2]['state'] == 'completed':
            return self.service.store.checkpoint_get(job['id'], 'headless_fact_outcome:' + run_id)
        story, research, _ = snapshot
        control_revision = story['_fact_research_control_revision']
        if self._handoff_rejected_source(job, run_id, goal, scope, control_revision):
            return
        provider = getattr(self.service.providers, 'research', None)
        if not callable(getattr(provider, 'extract_fact_page', None)):
            raise RetryableProviderError('research_fact_executor_unavailable', retry_at=self.service.store.now() + 60)
        with self.service.store.connection() as db:
            sources = [dict(row) for row in db.execute(
                'SELECT * FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url', (run_id,))]
        search_receipt = {}
        payload = json.loads(job.get('payload_json') or '{}')
        with self.service.store.connection() as db:
            rejected_urls = {row[0] for row in db.execute("SELECT s.url FROM research_run_sources s JOIN research_runs r "
                "ON r.run_id=s.run_id WHERE r.story_id=? AND r.poi_key=? AND s.error_code='not_article_text'",
                (story['id'], snapshot[2]['poi_key']))} if payload.get('research_query') else set()
        if not sources:
            identity = research['visual_identity']
            if identity.get('status') == 'match' and identity.get('visual_reference_verified') is True:
                articles = reviewed_reference_articles(identity)
                sources = [item for url, item in articles.items() if url not in rejected_urls]
                if sources:
                    # These already acquired pages are leads, never accepted
                    # facts. The same frozen reader, subject check and qualified
                    # own-evidence verifier still process every claim.
                    search_receipt = {'backend': 'visual_reference_articles'}
                    LOG.info('street_story_fact_identity_sources_reused story_id=%s run_id=%s sources=%s',
                             story['id'], run_id, len(sources))
        if not sources:
            articles = acquired_subject_articles(research['visual_identity'], research)
            sources = [item for url, item in articles.items() if url not in rejected_urls]
            if sources:
                search_receipt = {'backend': 'accepted_identity_subject_articles'}
                LOG.info('street_story_fact_subject_sources_reused story_id=%s run_id=%s sources=%s proof_kind=%s',
                         story['id'], run_id, len(sources), research['visual_identity'].get('proof_kind'))
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
                sources = [item for url, item in by_url.items() if url not in rejected_urls]
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
            if not sources and found.get('outcome', 'completed_empty') == 'completed_empty':
                self._bind_discovery(job, run_id, goal, scope, [], search_receipt, control_revision)
                return self._finish(job, run_id, control_revision, 'search_exhausted')
            if not sources and found.get('outcome') == 'selection_unavailable':
                return self._finish(job, run_id, control_revision, 'source_selection_unavailable')
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
            reviewed = await self._review_candidates(job, run_id, control_revision)
            with self.service.store.connection() as db:
                manifest = run_manifest(db, run_id)
                complete = manifest_complete(manifest)
                unreviewed = db.execute("SELECT 1 FROM fact_assertions a JOIN fact_observations o "
                    "ON o.story_id=a.story_id AND o.assertion_id=a.assertion_id "
                    "WHERE a.story_id=? AND o.run_id=? AND a.eligibility='unreviewed' LIMIT 1",
                    (story['id'], run_id)).fetchone()
            if ((not complete or unreviewed) and manifest_exhausted(manifest)
                    and not self._unreviewed_actionable(job, run_id)):
                return self._finish(job, run_id, control_revision,
                                    'source_manifest_exhausted' if not complete else 'fact_review_exhausted')
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
                    self._partial(run_id, 'research_fact_review_partial', retry_at=self._review_retry_at(job, run_id, reviewed))
                return
            if complete and not unreviewed:
                payload = json.loads(job.get('payload_json') or '{}')
                cached_only = any(item.get('research_run_id') == run_id and item.get('search_provider') == 'poi_memory'
                                  for item in snapshot[1].get('live_web_searches') or [] if isinstance(item, dict))
                if snapshot[2]['status_detail'] == 'research_fact_discovery_pending' or (payload.get('research_query') and cached_only):
                    return await self._discover_requested_gap(job, run_id, goal, scope, provider, control_revision)
                return self._finish(job, run_id, control_revision, 'source_batches_reviewed')
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
                'confirmed_identity': compact_physical_identity(research['visual_identity'],
                    photo_sha256=story['photo_sha256'], generation=story['_identity_generation'],
                    control_revision=int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)),
                'coverage_goal': goal, 'extraction_scope': scope,
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
        reviewed = 0
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
                                reviewed += await review_task
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
                reviewed += await review_task
        reviewed += await self._review_candidates(job, run_id, control_revision)
        if self._snapshot(job, run_id, control_revision) is not None:
            if self._handoff_rejected_source(job, run_id, goal, scope, control_revision):
                return
            for result in suggestions:
                self._queue_model_continuation(job, run_id, goal, scope, result, control_revision)
            with self.service.store.connection() as db:
                manifest = run_manifest(db, run_id)
            if manifest_exhausted(manifest) and not self._unreviewed_actionable(job, run_id):
                return self._finish(job, run_id, control_revision, 'source_manifest_exhausted')
            with self.service.store.tx() as db:
                unfinished = [row[0] for row in db.execute("SELECT chunk_id FROM research_chunk_runs WHERE run_id=? "
                    "AND status NOT IN ('extracted','no_claims','failed','cancelled')", (run_id,))]
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
                    self._partial(run_id, 'research_fact_review_partial', retry_at=self._review_retry_at(job, run_id, reviewed))

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
            old = db.execute('SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage=?',
                             (job['id'], 'headless_fact_unit:' + unit_id)).fetchone()
            previous = json.loads(old[0]) if old else {}
            if 'owner' not in detail and previous.get('owner'):
                detail['owner'] = previous['owner']
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
                rows = list(db.execute("SELECT r.chunk_id,c.ordinal FROM research_chunk_runs r "
                    "JOIN source_chunks c ON c.chunk_id=r.chunk_id WHERE r.run_id=? "
                    "AND r.status NOT IN ('extracted','no_claims','failed','cancelled') ORDER BY c.ordinal,c.source_version_id", (run_id,)))
                candidate = next((row for row in rows if row['chunk_id'] not in visited), None)
                source = db.execute("SELECT url FROM research_run_sources WHERE run_id=? "
                    "AND source_version_id IS NULL AND status!='failed' ORDER BY discovered_at,url LIMIT 1", (run_id,)).fetchone()
                if candidate and (candidate['ordinal'] == 0 or not source):
                    source = None
                elif source:
                    candidate = None  # Read a good source's first core before another page's tenth core.
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
            if not saved and old.get('owner') and old['owner'] != owner:
                self._unit_phase(job, page['_unit_id'], 'deferred', chunk_id=page['chunk_id'])
                continue  # A result-checkpoint crash cannot rebind old inference to edited inputs.
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
        self._unit_phase(job, page['_unit_id'], 'started', chunk_id=page['chunk_id'], owner=unit['owner'])
        async def renew_alive_owner():
            from .research_runs import renew_chunk_lease
            while True:
                await asyncio.sleep(45)
                alive = self._snapshot(job, story['_research_run_id'], story['_fact_research_control_revision'])
                if alive is None:
                    return
                with self.service.store.tx() as db:
                    if unit['owner'] != self._owner_fence(db, alive[0], alive[1]):
                        return
                    if not renew_chunk_lease(db, run_id=story['_research_run_id'], chunk_id=page['chunk_id'],
                        owner=unit['session'].id, fence=unit['session'].state['research_chunk_leases'][page['chunk_id']],
                        now=self.service.store.now()):
                        return
        heartbeat = asyncio.create_task(renew_alive_owner())
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
            if isinstance(exc, PermanentProviderError) and str(exc) == 'research_fact_routes_exhausted':
                with self.service.store.tx() as db:
                    if self._snapshot(job, story['_research_run_id'], story['_fact_research_control_revision']) is not None:
                        mark_chunk(db, run_id=story['_research_run_id'], chunk_id=page['chunk_id'], status='failed',
                                   error_code=str(exc), observation_count=0, model_name='', prompt_version='',
                                   now=self.service.store.now())
                self._unit_phase(job, page['_unit_id'], 'exhausted', chunk_id=page['chunk_id'], error_code=str(exc))
                return unit, None, exc
            known = isinstance(exc, MalformedProviderResponse) or self._boundary_closed(story, page['_unit_id'])
            self._unit_phase(job, page['_unit_id'], 'closed_error' if known else 'unknown',
                             chunk_id=page['chunk_id'], error_type=type(exc).__name__,
                             error_code=type(exc).__name__,
                             retry_at=getattr(exc, 'retry_at', None) or (None if known else self.service.store.now()+300))
            return unit, None, exc
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _commit_unit(self, unit, extracted, job, run_id, goal, scope, control_revision):
        from .research_runs import acquire_chunk_lease, chunk_lease_owned
        page, session = unit['page'], unit['session']
        snapshot = self._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return
        story, research, _ = snapshot
        with self.service.store.tx() as db:
            self.adapter._research_run_guard(db, session, run_id)
            fence = session.state['research_chunk_leases'][page['chunk_id']]
            if unit['owner'] == self._owner_fence(db, story, research) and not chunk_lease_owned(
                    db, run_id=run_id, chunk_id=page['chunk_id'], owner=session.id, fence=fence, now=self.service.store.now()):
                prior = db.execute('SELECT lease_owner,lease_fence FROM research_chunk_runs WHERE run_id=? AND chunk_id=?',
                                   (run_id, page['chunk_id'])).fetchone()
                if prior and prior['lease_owner'] == session.id and prior['lease_fence'] == fence:
                    renewed = acquire_chunk_lease(db, run_id=run_id, chunk_id=page['chunk_id'], owner=session.id,
                                                  now=self.service.store.now())
                    if renewed is not None:
                        session.state['research_chunk_leases'][page['chunk_id']] = renewed
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
