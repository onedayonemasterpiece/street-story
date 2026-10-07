"""One bounded background executor over the SAME Live visual queue/verdict gate."""
from __future__ import annotations
import json
import logging
import asyncio
import time
import base64
from types import SimpleNamespace
from .live_visual_comparison import LiveVisualComparisonMixin
from .service import canonical,digest,ConflictError
from .errors import RetryableProviderError, PermanentProviderError
from .identity_telemetry import record_identity_event

LOG = logging.getLogger('uvicorn.error')

VERDICT_SCHEMA = {'type':'object','properties':{
    'status':{'enum':['match','uncertain','mismatch']}, 'candidate_id':{'type':'string'},
    'reference_subject_candidate_id':{'type':'string'},'reference_subject_observations':{'type':'array','items':{'type':'string'}},
    'confidence':{'type':'number','minimum':0,'maximum':1},'observations':{'type':'array','items':{'type':'string'}},
    'alternative_candidate_ids':{'type':'array','items':{'type':'string'}}},
    'required':['status','candidate_id','confidence','observations','alternative_candidate_ids'], 'additionalProperties':False}


def grouped_verdict_schema():
    from copy import deepcopy
    schema = deepcopy(VERDICT_SCHEMA)
    item = deepcopy(VERDICT_SCHEMA)
    item['properties']['reference_id'] = {'type': 'string'}
    item['required'].append('reference_id')
    # Validate each result independently at the host gate. One malformed item
    # must not erase valid results or mark unreturned references reviewed.
    schema['properties']['reference_verdicts'] = {'type': 'array', 'maxItems': 4, 'items': {'type': 'object'}}
    schema['properties']['reference_id'] = {'type': 'string'}
    return schema, item


class HeadlessIdentity(LiveVisualComparisonMixin):
    def __init__(self, service):
        self.service = service
        self.write = lambda *_args: None

    def _store_command(self, db, story_id, command_id, tool_name, args, result):
        db.execute('INSERT OR IGNORE INTO live_commands(story_id,command_id,tool_name,request_digest,result_json,created_at) VALUES(?,?,?,?,?,?)',
            (story_id,command_id,tool_name,digest({'tool':tool_name,'args':args}),canonical(result),self.service.store.now()))

    async def run(self, job):
        provider = getattr(self.service.providers, 'research', None)
        if provider is None:
            raise RetryableProviderError('research_vision_unavailable', retry_at=self.service.store.now()+300)
        story,research = self.service._identity_snapshot(job['story_id'])
        generation = int(research.get('identity_generation') or 0)
        payload = json.loads(job.get('payload_json') or '{}')
        from .research_control import research_stopped
        if payload.get('identity_generation') != generation or research_stopped(
                research, 'identity', photo_sha256=story['photo_sha256'], identity_generation=generation):
            return
        active_pairs = any(item.get('phase') not in {'completed', 'failed', 'skipped'}
                           for item in (research.get('visual_search_operation') or {}).get('parallel_pairs', []))
        if not provider.vision_available and not active_pairs:
            raise RetryableProviderError('research_vision_unavailable', retry_at=self.service.store.now()+300)
        if (research.get('visual_identity') or {}).get('status') in {'match','owner_confirmed'} and not active_pairs:
            return
        if story.get('error_code') == 'visual_identity_conflict' and not active_pairs:
            return
        session = SimpleNamespace(id=f"headless:{job['id']}:{job['attempts']}", resource_id=story['id'],
            state={}, closed=False, model=provider.vision_model)
        routes = getattr(provider, 'parallel_visual_routes', lambda: ())()
        session.visual_reference_limit = min(4, len(routes)) if len(routes) > 1 else 1
        scope = {'photo_sha256': story['photo_sha256'], 'generation': generation,
                 'control_revision': self._visual_control_revision(research, story['photo_sha256'], generation)}
        try:
            started = time.monotonic()
            for _ in range(4):
                if await self._run_owned_unit(job, provider, story, session, scope):
                    return
                # A completed negative is progress. Yield between units so Stop
                # and other stories can run; each next send gets fresh admission.
                await asyncio.sleep(0)
                current, latest = self.service._identity_snapshot(story['id'])
                try:
                    self._assert_visual_current(current, latest, scope)
                except ConflictError:
                    return
                if (time.monotonic() - started >= 15
                        or not (session.state.get('visual_comparison') or {}).get('queue')):
                    break
            # Normal continuation, without a failure count/backoff. Keep this
            # job addressable; the worker's guarded done update cannot claim it.
            with self.service.store.tx() as db:
                db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=NULL,updated_at=? "
                           "WHERE id=? AND state='running' AND attempts=?",
                           (self.service.store.now(), self.service.store.now(), job['id'], job['attempts']))
            record_identity_event(self.service, story['id'], 'identity_background_progress',
                                  {'generation': generation, 'status': 'continuing'})
        except ConflictError:
            current, latest = self.service._identity_snapshot(story['id'])
            try:
                self._assert_visual_current(current, latest, scope)
            except ConflictError:
                return  # Stale completions must not fail the replacement story.
            with self.service.store.connection() as db:
                owned = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?",
                                   (job['id'], job['attempts'])).fetchone()
            if not owned:
                return
            saved = latest.get('visual_search_operation') or {}
            if saved.get('lease_owner') not in {None, session.id}:
                raise RetryableProviderError('visual_unit_busy', retry_at=saved.get('lease_until')) from None
            raise
        finally:
            # The awaited provider operation has exited. Preserve its durable
            # unknown/completed receipt and pending pair for ordinary resume.
            try:
                self._release_visual_lease(session, scope)
            except Exception as exc:
                LOG.warning('street_story_identity component=visual_queue stage=lease_release story_id=%s status=failed error_type=%s',
                            story['id'], type(exc).__name__)

    def _release_visual_lease(self, session, scope):
        """Release only this still-current worker, reading fresh queue state."""
        with self.service.store.tx() as db:
            row = db.execute('SELECT photo_sha256,research_json FROM stories WHERE id=?', (session.resource_id,)).fetchone()
            if row is None:
                return
            research = json.loads(row['research_json'] or '{}')
            saved = research.get('visual_search_operation') or {}
            if (saved.get('lease_owner') != session.id or saved.get('photo_sha256') != scope['photo_sha256']
                    or saved.get('generation') != scope['generation']
                    or int(saved.get('control_revision') or 0) != scope['control_revision']):
                return
            try:
                self._assert_visual_current(row, research, scope, session=session)
            except ConflictError:
                return
            saved.update(lease_owner=None, lease_until=0)
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), session.resource_id))
        LOG.info('street_story_identity component=visual_queue stage=lease_release story_id=%s status=released lease_owner=%s',
                 session.resource_id, session.id)

    async def _run_owned_unit(self, job, provider, story, session, scope):
        generation = scope['generation']
        current, latest = self.service._identity_snapshot(story['id'])
        saved = latest.get('visual_search_operation') or {}
        if any(pair.get('phase') not in {'completed', 'failed', 'skipped'} for pair in saved.get('parallel_pairs', [])):
            state = self._visual_lease(session, expected=scope)
            session.state['visual_comparison'] = state
            return await self._run_parallel_pairs(job, provider, story, session, scope)
        if current.get('error_code') == 'visual_identity_conflict':
            return True
        try:
            unit = await self._compare_place_images(session,{},expected_scope=scope)
        except ConflictError:
            current,latest = self.service._identity_snapshot(story['id'])
            try:
                self._assert_visual_current(current, latest, scope)
            except ConflictError:
                return True
            raise
        if unit.get('already_resolved'):
            return True
        pending = (session.state.get('visual_comparison') or {}).get('pending')
        if not pending:
            record_identity_event(self.service,story['id'],'identity_background_waiting',{
                'generation':generation,'reason':'insufficient_evidence' if unit.get('exhausted') else 'resource_or_source_wait'})
            if unit.get('exhausted'):
                return True  # Known finite no-evidence outcome; fresh owner leads may resume it.
            raise RetryableProviderError('identity_background_waiting', retry_at=self.service.store.now()+30)
        current,latest = self.service._identity_snapshot(story['id'])
        try:
            self._assert_visual_current(current, latest, scope, session=session)
        except ConflictError:
            return True
        routes = getattr(provider, 'parallel_visual_routes', lambda: ())()
        if (len(routes) > 1 and len(pending['candidates']) > 1
                and not self._parallel_parent_attempted(story, pending, generation)):
            self._freeze_parallel_pairs(session, pending, routes[:4])
            return await self._run_parallel_pairs(job, provider, story, session, scope)
        grouped = len(pending['candidates']) > 1
        extra = {'_visual_image_parts': pending['image_parts'],
                 '_visual_reference_mapping': list(pending['reply']['references'])}
        try:
            result = await provider.visual_verdict(None,{**story, **extra, '_identity_generation':generation,
                '_identity_research_control_revision': scope['control_revision'],
                '_research_job_id': job['id'], '_research_job_attempt': job['attempts']},
                grouped_verdict_schema()[0] if grouped else VERDICT_SCHEMA,canonical(pending['reply']))
        except PermanentProviderError as exc:
            if not grouped or str(exc) != 'research_visual_group_pair_required':
                raise
            state = session.state['visual_comparison']
            state['queue'] = pending['candidates'] + state['queue']
            state['pending'] = None
            session.visual_reference_limit = 1
            self._save_visual_queue(session, state)
            return False  # Known-unsent shape reduction, never an unknown replay.
        verdict = dict(result['result'])
        session.model = result['receipt'].get('model') or result['receipt'].get('model_id') or session.model
        verdict.update(comparison_id=pending['id'],provider_receipt=result['receipt'])
        # Persisted model result can be replayed after crash; photo/generation,
        # current lease and the common acceptance gate fence the actual commit.
        try:
            committed = self._record_place_comparison(session,'headless:'+pending['id'],verdict)
        except ConflictError:
            current,latest = self.service._identity_snapshot(story['id'])
            try:
                self._assert_visual_current(current, latest, scope, session=session)
            except ConflictError:
                return True
            raise
        return bool(committed['matched'])

    def _parallel_parent_attempted(self, story, pending, generation):
        # A previously addressed grouped unit must retain its exact original
        # send/readback even when independent routes have become available.
        from .visual_attachments import visual_operation_unit
        addressed = {**story, '_identity_generation': generation,
                     '_visual_reference_mapping': pending['reply']['references']}
        unit = canonical(visual_operation_unit(addressed, pending['reply']))
        with self.service.store.connection() as db:
            return any(row['logical_id'] == digest([story['id'], generation, row['role'], unit])
                       for row in db.execute("SELECT logical_id,role FROM research_provider_attempts WHERE story_id=? AND role LIKE 'vision%'",
                                             (story['id'],)))

    def _freeze_parallel_pairs(self, session, pending, routes):
        state = session.state['visual_comparison']
        pairs = []
        for index, (candidate, evidence, route) in enumerate(zip(pending['candidates'], pending['evidence'], routes)):
            reference = {**pending['reply']['references'][index], 'label': 'REF 1'}
            pair_id = pending['id'] + ':' + candidate['reference_id']
            pairs.append({'id': pair_id, 'route': route, 'phase': 'ready',
                'reference_id': candidate['reference_id'], 'candidates': [candidate], 'evidence': [evidence],
                'reply': {**pending['reply'], 'comparison_id': pair_id, 'references': [reference]}})
        # Only URLs, request addresses and exact proof context persist. No bytes.
        state['parallel_pairs'] = pairs
        state['queue'] = pending['candidates'][len(pairs):] + state['queue']
        state['pending'] = None
        self._save_visual_queue(session, state)
        record_identity_event(self.service, session.resource_id, 'identity_parallel_pairs_frozen', {
            'generation': state['generation'], 'pair_ids': [pair['id'] for pair in pairs],
            'routes': [pair['route'] for pair in pairs], 'pair_count': len(pairs)})

    def _parallel_pair_pending(self, story, pair, *, require_source):
        source = None
        try:
            source = self.service._source_photo_bytes(story['id'])
        except ConflictError:
            if require_source:
                raise RetryableProviderError('source_unavailable', retry_at=self.service.store.now()+30) from None
        return {**pair, 'image_parts': [
            {'label': 'SOURCE', 'mime_type': story.get('photo_mime_type') or 'image/jpeg',
             **({'data': base64.b64encode(source).decode('ascii')} if source is not None else {})},
            {'label': 'REF 1', 'mime_type': 'image/jpeg',
             'url': pair['candidates'][0]['reference_image_urls'][0]}]}

    async def _run_parallel_pairs(self, job, provider, story, session, scope):
        state = session.state['visual_comparison']
        pairs = [item for item in state['parallel_pairs'] if item.get('phase') not in {'completed', 'failed', 'skipped'}]
        if len(pairs) > 4:
            raise PermanentProviderError('research_visual_parallel_pair_limit')

        async def run_pair(pair):
            current, latest = self.service._identity_snapshot(story['id'])
            self._assert_visual_current(current, latest, scope, session=session)
            accepted = ((latest.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
                or current.get('error_code') == 'visual_identity_conflict')
            if pair['phase'] == 'result':
                return pair, {'result': pair['result'], 'receipt': pair['receipt']}, None
            if accepted and pair['phase'] == 'ready':
                pair['phase'] = 'skipped'
                self._save_visual_queue(session, state)
                return pair, None, None
            resume = pair['phase'] != 'ready'
            pending = self._parallel_pair_pending(story, pair, require_source=not resume)
            # The frozen dispatch address is durable before an external boundary.
            # A crash in this gap must be observed, never turn into a fresh send.
            pair['phase'] = 'submitted'
            self._save_visual_queue(session, state)
            pair_story = {**story, '_visual_image_parts': pending['image_parts'],
                '_visual_reference_mapping': list(pair['reply']['references']),
                '_identity_generation': scope['generation'],
                '_identity_research_control_revision': scope['control_revision'],
                '_research_job_id': job['id'], '_research_job_attempt': job['attempts'],
                '_visual_pair_resume_only': resume, '_visual_pair_observe_only': accepted,
                '_visual_source_unavailable': 'data' not in pending['image_parts'][0]}
            LOG.info('street_story_identity component=parallel_vision stage=dispatch story_id=%s comparison_id=%s route=%s resume=%s observe=%s',
                     story['id'], pair['id'], pair['route'], resume, accepted)
            try:
                result = await provider.visual_pair_route(pair['route'], None, pair_story,
                    VERDICT_SCHEMA, canonical(pair['reply']))
                return pair, result, None
            except (RetryableProviderError, PermanentProviderError) as exc:
                return pair, None, exc

        tasks = [asyncio.create_task(run_pair(pair)) for pair in pairs]
        waits = []
        try:
            for completed in asyncio.as_completed(tasks):
                pair, result, error = await completed
                if error is not None:
                    known_codes = {'research_visual_pair_dispatch_unknown', 'research_visual_pair_outcome_unknown',
                        'research_visual_pair_multiple_outcomes_unknown', 'research_visual_pair_source_required',
                        'research_visual_pair_observation_closed', 'research_visual_pair_routes_closed',
                        'research_vision_waiting', 'research_vision_route_unverified'}
                    pair['last_error'] = str(error) if str(error) in known_codes else type(error).__name__
                    if isinstance(error, PermanentProviderError):
                        pair['phase'] = 'failed'
                    else:
                        waits.append(error)
                    self._save_visual_queue(session, state)
                    LOG.warning('street_story_identity component=parallel_vision stage=waiting story_id=%s comparison_id=%s route=%s phase=%s error_type=%s reason=%s',
                                story['id'], pair['id'], pair['route'], pair['phase'], type(error).__name__, pair['last_error'])
                    continue
                if result is None:
                    continue
                pair.update(phase='result', result=result['result'], receipt=result['receipt'])
                self._save_visual_queue(session, state)
                # Commit one pair through the same subject/geometry/proof gate.
                # It atomically retains every submitted peer before publishing match.
                session.model = result['receipt'].get('model') or result['receipt'].get('model_id') or session.model
                state['pending'] = self._parallel_pair_pending(story, pair, require_source=False)
                verdict = {**result['result'], 'comparison_id': pair['id'], 'provider_receipt': result['receipt']}
                self._record_place_comparison(session, 'headless:'+pair['id'], verdict)
                LOG.info('street_story_identity component=parallel_vision stage=completed story_id=%s comparison_id=%s route=%s model=%s',
                         story['id'], pair['id'], pair['route'], session.model)
        except asyncio.CancelledError:
            # Process/session shutdown: provider adapters retain UNKNOWN receipts.
            # A sufficient match never enters this cancellation path.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            # No first-match cancellation: already-submitted siblings drain normally.
            if not all(task.done() for task in tasks):
                await asyncio.gather(*tasks, return_exceptions=True)
        if waits:
            raise waits[0]
        current, latest = self.service._identity_snapshot(story['id'])
        return ((latest.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
            or current.get('error_code') == 'visual_identity_conflict')
