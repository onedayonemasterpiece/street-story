import json
from types import SimpleNamespace

import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.headless_identity import HeadlessIdentity
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical, digest
from street_story.visual_attachments import visual_operation_unit
from test_parallel_identity_pairs import prepare, response


def setup(tmp_path, receipts):
    svc, story, _ = prepare(tmp_path)
    worker = HeadlessIdentity(svc)
    session = SimpleNamespace(id='headless:single-ref', resource_id=story['id'],
        state={}, closed=False, model='test-model', visual_reference_limit=1)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = svc
    calls = []
    async def verdict(snapshot, addressed, schema, context):
        supplied = json.loads(context)
        calls.append(supplied)
        if len(calls) == 1:
            unit = canonical(visual_operation_unit(addressed, supplied))
            with svc.store.tx() as db:
                for role, receipt in receipts.items():
                    attempt_id = 'exact-' + role
                    receipt = {**receipt, 'comparison_id': supplied['comparison_id'],
                        'binding': {'attempt_id': attempt_id, 'generation': 0, 'visual_scope': True}}
                    db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (attempt_id, digest([story['id'], 0, role, unit]), story['id'], role,
                         canonical(receipt), svc.store.now(), svc.store.now()))
            raise PermanentProviderError('native_vision:reference_unavailable')
        return response(addressed, 'actual-next-frame', 'match')
    provider = SimpleNamespace(visual_verdict=verdict, visual_pair_receipts=adapter.visual_pair_receipts,
        parallel_visual_routes=lambda: ())
    scope = {'generation': 0, 'photo_sha256': story['photo_sha256'], 'control_revision': 0}
    return svc, story, worker, session, provider, calls, scope


@pytest.mark.asyncio
async def test_exact_unsent_bad_ref_retires_without_verdict_and_next_frame_reaches_common_gate(tmp_path):
    receipts = {'vision_google_pair': {'phase': 'failed', 'provider_send_state': 'not_sent'},
        'vision_native': {'phase': 'failed', 'provider_send_state': 'not_sent',
                          'error_code': 'native_vision:reference_unavailable', 'thread_id': None, 'turn_id': None}}
    svc, story, worker, session, provider, calls, scope = setup(tmp_path, receipts)
    assert not await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    _, research = svc._identity_snapshot(story['id'])
    queue = research['visual_search_operation']
    retired = queue['unavailable_references'][0]
    original_id = calls[0]['comparison_id']
    first_ref = calls[0]['references'][0]['reference_id']
    assert retired['comparison_id'] == original_id
    assert retired['reference_id'] == first_ref
    assert set(retired['provider_receipts']) == set(receipts)
    assert retired['provider_receipts']['vision_native']['binding']['attempt_id'] == 'exact-vision_native'
    assert queue['unavailable_reference_ids'] == [first_ref]
    assert first_ref not in queue['reviewed_reference_ids']
    assert not queue.get('verdict_history')
    assert 'pending_descriptor' not in queue
    assert svc.story(story['id'])['state'] == 'identifying'
    assert not svc._identity_snapshot(story['id'])[0]['error_code']
    assert await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    assert len(calls) == 2 and calls[1]['comparison_id'] != original_id
    assert calls[1]['references'][0]['reference_id'] != first_ref
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1
    _, research = svc._identity_snapshot(story['id'])
    assert [v['comparison_id'] for v in research['visual_search_operation']['verdict_history']] == [calls[1]['comparison_id']]
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts WHERE story_id=?', (story['id'],)).fetchone()[0] == 2
        event = db.execute("SELECT payload_json FROM live_diagnostics WHERE story_id=? AND event_type='identity_reference_unavailable'", (story['id'],)).fetchone()
    assert json.loads(event[0])['provider_send_state'] == 'not_sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', [
    {'phase': 'unknown', 'provider_send_state': 'possibly_sent'},
    {'phase': 'submitted', 'provider_send_state': 'not_sent'},
    {'phase': 'prompt_intent', 'provider_send_state': 'not_sent'},
    {'phase': 'completed', 'provider_send_state': 'not_sent', 'result': {'status': 'mismatch'}},
    {'phase': 'failed'},
    {'phase': 'created'},
    {'phase': 'failed', 'provider_send_state': 'not_sent', 'turn_id': 'original-submitted-turn'},
    {'phase': 'aborted', 'provider_send_state': 'not_sent'},
])
async def test_any_unsafe_exact_receipt_preserves_original_pending_and_receipts(tmp_path, unsafe):
    svc, story, worker, session, provider, calls, scope = setup(tmp_path, {
        'vision_google_pair': unsafe, 'vision_native': {'phase': 'failed', 'provider_send_state': 'not_sent'}})
    with pytest.raises(RetryableProviderError, match='research_visual_pair_outcome_unknown'):
        await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    _, research = svc._identity_snapshot(story['id'])
    queue = research['visual_search_operation']
    assert queue['pending_descriptor']['id'] == calls[0]['comparison_id']
    assert len(calls) == 1
    assert not queue.get('unavailable_references')
    assert not queue.get('unavailable_reference_ids')
    assert not queue.get('verdict_history')
    with svc.store.connection() as db:
        receipt = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', ('exact-vision_google_pair',)).fetchone()[0])
    assert all(receipt[key] == value for key, value in unsafe.items())


@pytest.mark.asyncio
async def test_missing_exact_receipt_proof_never_clears_original_pending(tmp_path):
    svc, story, worker, session, provider, calls, scope = setup(tmp_path, {})
    with pytest.raises(RetryableProviderError, match='research_visual_pair_outcome_unknown'):
        await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    _, research = svc._identity_snapshot(story['id'])
    assert research['visual_search_operation']['pending_descriptor']['id'] == calls[0]['comparison_id']
    assert not research['visual_search_operation'].get('unavailable_reference_ids')


@pytest.mark.asyncio
async def test_normal_identity_visual_worker_continues_same_job_to_next_usable_frame(tmp_path):
    svc, story, _worker, _session, provider, calls, _scope = setup(tmp_path, {
        'vision_google_pair': {'phase': 'failed', 'provider_send_state': 'not_sent'},
        'vision_native': {'phase': 'failed', 'provider_send_state': 'not_sent'}})
    provider.vision_available = True
    provider.vision_model = 'test-model'
    svc.providers.research = provider
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(calls) == 2
    assert svc.story(story['id'])['visual_identity']['status'] == 'match'
    with svc.store.connection() as db:
        job = db.execute("SELECT state,last_error,attempts FROM jobs WHERE story_id=? AND kind='identity_visual'", (story['id'],)).fetchone()
    assert tuple(job) == ('done', None, 1)
    assert svc._identity_snapshot(story['id'])[0]['error_code'] is None


@pytest.mark.asyncio
async def test_retired_reference_rematerialized_from_gallery_is_not_sent_again(tmp_path):
    svc, story, worker, session, provider, calls, scope = setup(tmp_path, {
        'vision_native': {'phase': 'failed', 'provider_send_state': 'not_sent'}})
    assert not await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    state = session.state['visual_comparison']
    first_ref = state['unavailable_reference_ids'][0]
    identity = svc._identity_snapshot(story['id'])[1]['visual_identity']
    rematerialized = next(worker._image_entries(identity['candidates'][0]))
    assert rematerialized['reference_id'] == first_ref
    state['queue'].insert(0, rematerialized)
    worker._save_visual_queue(session, state)
    assert await worker._run_owned_unit({'id': 'single-job', 'attempts': 1}, provider, story, session, scope)
    assert len(calls) == 2
    assert all(call['references'][0]['reference_id'] != first_ref for call in calls[1:])
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1
