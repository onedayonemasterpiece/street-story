"""One bounded background executor over the SAME Live visual queue/verdict gate."""
from __future__ import annotations
import json
import logging
import asyncio
import time
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
        if provider is None or not provider.vision_available:
            raise RetryableProviderError('research_vision_unavailable', retry_at=self.service.store.now()+300)
        story,research = self.service._identity_snapshot(job['story_id'])
        generation = int(research.get('identity_generation') or 0)
        payload = json.loads(job.get('payload_json') or '{}')
        from .research_control import research_stopped
        if payload.get('identity_generation') != generation or research_stopped(
                research, 'identity', photo_sha256=story['photo_sha256'], identity_generation=generation):
            return
        if (research.get('visual_identity') or {}).get('status') in {'match','owner_confirmed'}:
            return
        session = SimpleNamespace(id=f"headless:{job['id']}:{job['attempts']}", resource_id=story['id'],
            state={}, closed=False, model=provider.vision_model)
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
            raise RetryableProviderError('identity_background_waiting', retry_at=self.service.store.now()+(300 if unit.get('exhausted') else 30))
        current,latest = self.service._identity_snapshot(story['id'])
        try:
            self._assert_visual_current(current, latest, scope, session=session)
        except ConflictError:
            return True
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
