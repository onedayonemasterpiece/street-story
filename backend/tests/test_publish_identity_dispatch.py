"""Identity conflict blocks new dispatch without orphaning original operations."""
import asyncio
from copy import deepcopy
import json

import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.service import canonical
from test_product_checkpoint import ProductFakeVibePublish, create_story, product_service


class Boundary(ProductFakeVibePublish):
    def __init__(self):
        super().__init__()
        self.calls, self.status_calls, self.bootstrap_calls = [], [], 0
        self.on_bootstrap = self.on_publish = None
    async def bootstrap(self):
        self.bootstrap_calls += 1
        if self.on_bootstrap:
            self.on_bootstrap()
        return await super().bootstrap()
    async def publish(self, payload, request_key):
        self.calls.append((request_key, deepcopy(payload)))
        if self.on_publish:
            self.on_publish()
        return await super().publish(payload, request_key)
    async def status(self, operation_id):
        self.status_calls.append(operation_id)
        return await super().status(operation_id)


def prepare(tmp_path):
    boundary = Boundary()
    service, _, _ = product_service(tmp_path, vp=boundary)
    story = create_story(service)
    with service.store.tx() as db:
        research = {'visual_identity': {'status': 'match', 'candidate_id': 'wiki:1', 'visual_reference_verified': True},
                    'publication_concept': 'Preserved owner concept'}
        db.execute("UPDATE stories SET state='ready_to_publish',vibepublish_asset_ref='verified-asset',"
                   "draft_text='Preserved selected facts draft',research_json=? WHERE id=?", (canonical(research), story['id']))
    service.mutate_publish(story['id'], 'owner-explicit-confirm', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    with service.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='publish' AND story_id=?", (story['id'],)).fetchone())
    return service, story['id'], job, boundary


def conflict(service, sid, *, kind='sibling'):
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        if kind == 'sibling':
            research['visual_identity'].update(status='uncertain', visual_reference_verified=False,
                confidence=None, conflicting_comparison_ids=['parent:ref-a', 'parent:ref-b'])
        elif kind == 'research':
            research['visual_identity_conflict'] = {'comparison_ids': ['parent:ref-a', 'parent:ref-b']}
        elif kind == 'uncertain':
            research['visual_identity']['status'] = 'uncertain'
        db.execute("UPDATE stories SET research_json=?,state='needs_review',error_code=? WHERE id=?",
                   (canonical(research), 'visual_identity_conflict' if kind == 'sibling' else None, sid))


def intent(service, job):
    iid = json.loads(job['payload_json'])['intent_id']
    with service.store.connection() as db:
        return dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (iid,)).fetchone())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['sibling', 'research', 'uncertain'])
async def test_frozen_unsubmitted_intent_blocked_before_any_boundary_call(tmp_path, kind):
    service, sid, job, boundary = prepare(tmp_path)
    before = intent(service, job)
    conflict(service, sid, kind=kind)
    with pytest.raises(PermanentProviderError, match='conflict_before_send'):
        await service._run_publish(job)
    after = intent(service, job)
    assert not boundary.calls and not boundary.status_calls and boundary.bootstrap_calls == 0
    assert after['vibepublish_request_key'] == before['vibepublish_request_key']
    assert not after['receipt_json']
    assert service.story(sid)['draft_text'] == 'Preserved selected facts draft'
    with service.store.connection() as db:
        assert json.loads(service._story_row(db, sid)['research_json'])['publication_concept'] == 'Preserved owner concept'


@pytest.mark.asyncio
async def test_conflict_during_bootstrap_is_caught_before_durable_send_marker(tmp_path):
    service, sid, job, boundary = prepare(tmp_path)
    boundary.on_bootstrap = lambda: conflict(service, sid)
    with pytest.raises(PermanentProviderError, match='conflict_before_send'):
        await service._run_publish(job)
    assert not boundary.calls and not intent(service, job)['receipt_json']


@pytest.mark.asyncio
async def test_before_await_marker_is_durable_and_success_retains_it(tmp_path):
    service, _, job, boundary = prepare(tmp_path)
    observed = []
    boundary.on_publish = lambda: observed.append(json.loads(intent(service, job)['receipt_json'])['_publish_dispatch'])
    await service._run_publish(job)
    saved = json.loads(intent(service, job)['receipt_json'])
    assert observed[0]['phase'] == 'possibly_sent'
    assert observed[0]['request_key'] == boundary.calls[0][0]
    assert observed[0]['payload'] == boundary.calls[0][1]
    assert saved['_publish_dispatch'] == observed[0] and saved['state'] == 'scheduled'


@pytest.mark.asyncio
async def test_known_original_operation_status_survives_conflict_and_broken_bootstrap(tmp_path):
    service, sid, job, boundary = prepare(tmp_path)
    await service._run_publish(job)
    original = intent(service, job)
    conflict(service, sid)
    def no_bootstrap():
        pytest.fail('Existing original operation must use readback directly')
    boundary.on_bootstrap = no_bootstrap
    await service._run_publish(job)
    after = intent(service, job)
    assert len(boundary.calls) == 1 and boundary.status_calls == ['vp_publish_op_1']
    assert after['vibepublish_operation_id'] == original['vibepublish_operation_id']
    assert after['vibepublish_request_key'] == original['vibepublish_request_key']
    assert after['state'] == 'scheduled'
    assert service.story(sid)['state'] == 'needs_review'
    with service.store.connection() as db:
        assert service._story_row(db, sid)['error_code'] == 'visual_identity_conflict'


@pytest.mark.asyncio
async def test_lost_response_conflict_cannot_create_first_or_duplicate_post(tmp_path):
    service, sid, job, boundary = prepare(tmp_path)
    original_publish = boundary.publish
    async def lose(payload, key):
        await original_publish(payload, key)
        raise RetryableProviderError('lost response after acceptance')
    boundary.publish = lose
    with pytest.raises(RetryableProviderError, match='lost response'):
        await service._run_publish(job)
    before = intent(service, job)
    conflict(service, sid)
    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='conflict_outcome_unknown'):
            await service._run_publish(job)
    after = intent(service, job)
    assert len(boundary.calls) == len(boundary.publish_effects) == 1
    assert after['vibepublish_request_key'] == before['vibepublish_request_key']
    assert after['receipt_json'] == before['receipt_json'] and not after['vibepublish_operation_id']


@pytest.mark.asyncio
async def test_unknown_same_key_recovery_uses_exact_payload_even_routing_changes(tmp_path):
    service, _, job, boundary = prepare(tmp_path)
    original_publish = boundary.publish
    async def lose(payload, key):
        await original_publish(payload, key)
        raise RetryableProviderError('lost response')
    boundary.publish = lose
    with pytest.raises(RetryableProviderError):
        await service._run_publish(job)
    first_key, first_payload = boundary.calls[0]
    boundary.publish = original_publish
    boundary.on_bootstrap = lambda: pytest.fail('Unknown request must recover its original frozen payload')
    await service._run_publish(job)
    assert boundary.calls[1] == (first_key, first_payload)
    assert len(boundary.publish_effects) == 1 and intent(service, job)['state'] == 'scheduled'


@pytest.mark.asyncio
async def test_preflight_failure_is_known_unsent_and_no_dispatch_marker(tmp_path):
    service, _, job, boundary = prepare(tmp_path)
    def refuse():
        raise RetryableProviderError('bootstrap unavailable before dispatch')
    boundary.on_bootstrap = refuse
    with pytest.raises(RetryableProviderError, match='before dispatch'):
        await service._run_publish(job)
    assert not boundary.calls and not intent(service, job)['receipt_json']


@pytest.mark.asyncio
async def test_cancellation_at_publish_boundary_retains_unknown_not_unsent(tmp_path):
    service, sid, job, boundary = prepare(tmp_path)
    entered = asyncio.Event()
    async def pending(payload, key):
        boundary.calls.append((key, deepcopy(payload)))
        entered.set()
        await asyncio.Future()
    boundary.publish = pending
    task = asyncio.create_task(service._run_publish(job))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = intent(service, job)
    marker = json.loads(saved['receipt_json'])['_publish_dispatch']
    assert marker['phase'] == 'possibly_sent' and not saved['vibepublish_operation_id']
    conflict(service, sid)
    with pytest.raises(RetryableProviderError, match='conflict_outcome_unknown'):
        await service._run_publish(job)
    assert len(boundary.calls) == 1 and intent(service, job)['receipt_json'] == saved['receipt_json']


@pytest.mark.asyncio
async def test_legacy_retried_intent_without_marker_is_not_declared_known_unsent(tmp_path):
    service, sid, job, boundary = prepare(tmp_path)
    job['attempts'] = 2  # Before-marker implementation might already have sent.
    conflict(service, sid)
    with pytest.raises(RetryableProviderError, match='conflict_outcome_unknown'):
        await service._run_publish(job)
    assert not boundary.calls and not intent(service, job)['receipt_json']
