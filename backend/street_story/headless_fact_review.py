"""Small frozen candidate reviews through the existing guarded text transport."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from types import SimpleNamespace
from weakref import WeakValueDictionary

from . import review_packets
from .live import FUNCTIONS
from .service import ConflictError, canonical

LOG = logging.getLogger(__name__)
_COMMIT_LOCKS = WeakValueDictionary()


class HeadlessFactReview:
    def __init__(self, harness):
        self.harness, self.service = harness, harness.service

    def _put(self, job, unit, value):
        with self.service.store.tx() as db:
            db.execute('INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(job_id,stage) DO UPDATE SET value_json=excluded.value_json',
                       (job['id'], 'headless_fact_review:' + unit, canonical(value), self.service.store.now()))

    def _qualified_routes(self, *, available=True):
        provider = getattr(self.service.providers, 'research', None)
        build = getattr(provider, '_fact_pool_routes', None)
        proof = self.service.store.cache_get('fact-semantic-verification-v1') or {}
        entries = proof.get('routes') or []
        routes = build() if callable(build) else []
        return [route for route in routes if route.get('qualified') and route.get('endpoint')
            and (not available or route['available']) and any(isinstance(entry, dict)
                and all(entry.get(key) == route.get(key) for key in ('provider_id', 'model_id', 'endpoint'))
                and (not entry.get('directory') or entry['directory'] == route['client'].directory)
                and all(entry.get(key) is True for key in ('schema_verified', 'own_passages_verified',
                    'qualifier_negative_verified', 'nearby_duplicate_verified', 'nearby_conflict_verified')) for entry in entries)]

    async def _infer(self, packet, job, unit, saved, ordinal=0):
        if saved.get('phase') == 'result':
            return saved['args']
        if saved.get('phase') in {'started', 'unknown', 'committed', 'rejected', 'stale'}:
            return None
        if saved.get('retry_at', 0) > self.service.store.now():
            return None
        routes = self._qualified_routes()
        if routes:
            offset = ordinal % len(routes)
            routes = routes[offset:] + routes[:offset]
        provider = getattr(self.service.providers, 'research', None)
        snapshot = self.harness._snapshot(job, packet['run_id'])
        if snapshot is None:
            return None
        story = {**snapshot[0], '_fact_pool_unit_id': unit,
                 '_fact_pool_input_sha256': hashlib.sha256(canonical(packet).encode()).hexdigest()}
        schema = next(tool['parameters'] for tool in FUNCTIONS if tool['name'] == 'finalize_fact_review')
        prompt = ('Independently verify these unreviewed source-backed candidate claims. '
            + review_packets.REVIEW_CHECKS + ' Compare nearby existing claims for duplicate or conflict. '
            'Return the specified JSON for this exact packet, coverage_complete=false. '
            'Do not search, select facts, edit the publication or invent evidence. Frozen packet: '
            + canonical(packet))
        for route in routes:
            role = 'facts_review_' + route['model_id']
            with self.service.store.connection() as db:
                rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=? '
                    'AND role=? ORDER BY created_at DESC,rowid DESC', (story['id'], role)))
            prior = next((json.loads(row[0]) for row in rows
                if (json.loads(row[0]).get('binding') or {}).get('fact_unit_id') == unit), {})
            if prior.get('phase') == 'aborted' and prior.get('abort_acknowledged') is not True:
                self._put(job, unit, {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'route': role})
                return None
            if prior.get('phase') in {'failed', 'aborted'}:
                continue  # Closed unchanged semantic unit may use another route.
            client = route['client']
            self._put(job, unit, {'phase': 'started', 'packet_ref': packet['packet_ref'], 'route': role})
            LOG.info('street_story_background_fact_review_started story_id=%s run_id=%s unit_id=%s model_id=%s',
                     job['story_id'], packet['run_id'], unit, client.model_id)
            try:
                response = await provider.run(story, role, unit,
                    lambda binding, client=client: client._run('facts', prompt, binding, schema), client=client)
                from jsonschema import Draft202012Validator
                args = response.get('result')
                if (not Draft202012Validator(schema).is_valid(args)
                        or args.get('packet_ref') != packet['packet_ref']):
                    self._put(job, unit, {'phase': 'closed_error', 'route': role})
                    LOG.info('street_story_background_fact_review_fallback story_id=%s unit_id=%s model_id=%s reason=malformed',
                             job['story_id'], unit, client.model_id)
                    continue
                self._put(job, unit, {'phase': 'result', 'args': args, 'route': role, 'model_id': client.model_id})
                return args
            except asyncio.CancelledError:
                self._put(job, unit, {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'route': role})
                raise
            except Exception as exc:
                with self.service.store.connection() as db:
                    latest = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=? '
                        "AND role=? AND json_extract(receipt_json,'$.binding.fact_unit_id')=? "
                        'ORDER BY updated_at DESC,rowid DESC LIMIT 1', (story['id'], role, unit)).fetchone()
                receipt = json.loads(latest[0]) if latest else {}
                unknown = (receipt.get('phase') not in {'created', 'failed', 'completed', 'aborted'}
                    or receipt.get('phase') == 'aborted' and not receipt.get('abort_acknowledged'))
                self._put(job, unit, {'phase': 'unknown' if unknown else 'closed_error', 'route': role,
                    'packet_ref': packet['packet_ref'], 'error_type': type(exc).__name__})
                LOG.info('street_story_background_fact_review_failed story_id=%s unit_id=%s model_id=%s unknown=%s reason=%s',
                         job['story_id'], unit, client.model_id, unknown, type(exc).__name__)
                if unknown:
                    return None
        self._put(job, unit, {'phase': 'closed_error', 'packet_ref': packet['packet_ref'],
                             'retry_at': self.service.store.now()+60})
        return None

    async def run(self, job, run_id, control_revision):
        snapshot = self.harness._snapshot(job, run_id, control_revision)
        if snapshot is None:
            return 0
        with self.service.store.connection() as db:
            pending = review_packets.pending_candidates(db, job['story_id'], run_id)
        if not pending:
            return 0
        lock_key = (id(self.service), job['story_id'])
        lock = _COMMIT_LOCKS.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _COMMIT_LOCKS[lock_key] = lock
        prepared = []
        for start in range(0, len(pending), 3):
            if len(prepared) == 4:
                break
            session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'],
                model='gemini-3.8-live', actor=None, closed=False,
                state={'fact_research_control_revision': control_revision})
            candidate_ids = pending[start:start+3]
            with self.service.store.connection() as db:
                story, _ = self.harness.adapter._research_run_guard(db, session, run_id)
                current = review_packets.bundle(db, job['story_id'])
                candidate_bundle = {fid: current[fid] for fid in candidate_ids if fid in current}
                recipe = [run_id, candidate_bundle, review_packets.candidate_review_fence(db, story),
                          review_packets.eligible_bundle(db, job['story_id'])]
            unit = hashlib.sha256(canonical(recipe).encode()).hexdigest()[:24]
            saved = self.service.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit) or {}
            if (saved.get('phase') in {'started', 'unknown', 'committed', 'rejected', 'stale'}
                    or saved.get('retry_at', 0) > self.service.store.now()):
                continue
            args = ({'packet_ref': saved['args']['packet_ref']} if saved.get('phase') == 'result' else {
                'run_id': run_id, '_candidate_ids': candidate_ids, '_parallel_candidate_review': True})
            try:
                packet = review_packets.read(self.harness.adapter, session, args)
                if not packet.get('packet_ref'):
                    continue
                items = list(packet['items'])
                while packet.get('has_more'):
                    packet = review_packets.read(self.harness.adapter, session, packet['next_args'])
                    items.extend(packet['items'])
                packet = {**packet, 'items': items}
            except ConflictError:
                self._put(job, unit, {'phase': 'stale'})
                continue
            prepared.append((session, packet, unit, saved))
        async def review(item, ordinal):
            _session, packet, unit, saved = item
            return item, await self._infer(packet, job, unit, saved, ordinal)
        tasks = [asyncio.create_task(review(item, ordinal)) for ordinal, item in enumerate(prepared)]
        committed = 0
        try:
            for ready in asyncio.as_completed(tasks):
                item, args = await ready
                if args is None:
                    continue
                session, packet, unit, _ = item
                outcome = self.service.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit) or {}
                session.model = outcome.get('model_id') or session.model
                async with lock:
                    if self.harness._snapshot(job, run_id, control_revision) is None:
                        continue
                    try:
                        value = await self.harness.adapter.execute_tool(session, {
                            'name': 'finalize_fact_review', 'id': 'background-review-' + unit, 'args': args})
                        if value.get('eligible_count') is not None:
                            committed += 1
                            self._put(job, unit, {'phase': 'committed', 'packet_ref': packet['packet_ref']})
                            LOG.info('street_story_background_fact_review_committed story_id=%s run_id=%s unit_id=%s eligible=%s',
                                     job['story_id'], run_id, unit, value['eligible_count'])
                    except ConflictError as exc:
                        self._put(job, unit, {'phase': 'stale' if exc.code.endswith('stale') else 'rejected',
                                             'packet_ref': packet['packet_ref'], 'error_code': exc.code})
                        LOG.info('street_story_background_fact_review_deferred story_id=%s run_id=%s unit_id=%s reason=%s',
                                 job['story_id'], run_id, unit, exc.code)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return committed
