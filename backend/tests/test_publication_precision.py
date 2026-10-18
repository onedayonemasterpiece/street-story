"""Whole-second scheduling and typed, closed native preflight recovery."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from street_story.service import ConflictError, InvalidStateError, canonical
from test_backend import create, seed_ready_visual, service
from test_live_editor import make_service, mark_identity_ready


def future(*, microsecond=0):
    return (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=microsecond).isoformat()


def fractional_offset():
    return future().replace('+00:00', '+01:00:00.500000')


def live_ready(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='ready_to_publish',draft_text='Owner text',"
            "vibepublish_asset_ref='asset-1',processed_image_url='/v1/assets/x',visual_context_json=? WHERE id=?",
            (canonical({'content_revision': 'visual-r1', 'stale': False}), session.resource_id))
    return svc, adapter, session, events


def closed_precision_intent(svc, sid, *, change=None):
    svc.mutate_publish(sid, 'original-intent', {'destinations': ['street_story_e2e_test'], 'delay_minutes': 5})
    with svc.store.tx() as db:
        intent = dict(db.execute('SELECT * FROM publish_intents WHERE story_id=?', (sid,)).fetchone())
        request = json.loads(intent['request_json'])
        request['scheduled_for'] = future(microsecond=951244)
        receipt = {'operation_id': 'op_original', 'resource_id': 'pub_original', 'action': 'publish',
            'state': 'blocked', 'operation_complete': True, 'retry_safe': False,
            'error': {'code': 'native_time_precision'}, 'deliveries': [{
                'destination': 'street_story_e2e_test', 'provider': 'telegram', 'attempt_id': 'attempt_original',
                'state': 'blocked', 'stage': 'blocked', 'observed': 'not_attempted',
                'missing_checks': ['native_time_precision'], 'applied_revision': None, 'retry_safe': False}]}
        if change:
            change(receipt, request)
        db.execute("UPDATE publish_intents SET state='blocked',vibepublish_operation_id='op_original',"
            "vibepublish_publication_id='pub_original',receipt_json=?,request_json=?,scheduled_for=? WHERE id=?",
            (canonical(receipt), canonical(request), request['scheduled_for'], intent['id']))
        db.execute("UPDATE jobs SET state='failed',last_error='VibePublish publication operation ended in blocked' "
            "WHERE kind='publish' AND story_id=?", (sid,))
        db.execute("UPDATE stories SET state='needs_review',error_code='provider_permanent_error' WHERE id=?", (sid,))
        return dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (intent['id'],)).fetchone())


@pytest.mark.parametrize('scheduled', [lambda: future(microsecond=1), lambda: future(microsecond=951244), fractional_offset])
def test_fractional_schedule_rejected_before_new_intent_or_dispatch(tmp_path, scheduled):
    svc, _, vp = service(tmp_path)
    story = create(svc)
    seed_ready_visual(svc, story['id'])
    before = svc.story(story['id'])
    with pytest.raises(ConflictError) as rejected:
        svc.mutate_publish(story['id'], 'fractional', {'destinations': ['polyubit-kaliningrad-telegram'],
            'scheduled_for': scheduled()})
    assert rejected.value.code == 'publish_time_precision'
    assert svc.story(story['id']) == before and not vp.publish_calls
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 0
        assert not db.execute("SELECT * FROM idempotency WHERE key='fractional'").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('scheduled', [lambda: future(microsecond=951244), fractional_offset])
async def test_live_fractional_schedule_rejected_before_confirmation_card(tmp_path, scheduled):
    svc, adapter, session, _ = live_ready(tmp_path)
    with pytest.raises(ConflictError) as rejected:
        await adapter._prepare_publication(session.resource_id, 'fractional-card', {
            'destinations': ['street_story_e2e_test'], 'scheduled_for': scheduled(), 'timezone': 'UTC'})
    assert rejected.value.code == 'publish_time_precision'
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_publication_confirmations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 0


@pytest.mark.parametrize('body', [{}, {'delay_minutes': 5}])
def test_default_schedule_is_whole_seconds_in_intent_and_request(tmp_path, body):
    svc, _, vp = service(tmp_path)
    story = create(svc)
    seed_ready_visual(svc, story['id'])
    svc.mutate_publish(story['id'], 'whole-default', {'destinations': ['polyubit-kaliningrad-telegram'], **body})
    with svc.store.connection() as db:
        intent = dict(db.execute('SELECT * FROM publish_intents').fetchone())
        scheduled = intent['scheduled_for']
        assert datetime.fromisoformat(scheduled.replace('Z', '+00:00')).microsecond == 0
        assert '.' not in scheduled and json.loads(intent['request_json'])['scheduled_for'] == scheduled
    assert not vp.publish_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('prior_offset', [False, True])
async def test_closed_precision_all_not_attempted_allows_separate_normal_card_and_intent(tmp_path, prior_offset):
    svc, adapter, session, _ = live_ready(tmp_path)
    original = closed_precision_intent(svc, session.resource_id, change=(
        lambda _receipt, request: request.update(scheduled_for=fractional_offset())) if prior_offset else None)
    result = await adapter._prepare_publication(session.resource_id, 'corrected-card', {
        'destinations': ['street_story_e2e_test'], 'scheduled_for': future(), 'timezone': 'UTC'})
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 1
    confirmed = adapter._confirm_publication(session.resource_id, 'owner-explicit-confirmation',
        {'confirmation_id': result['confirmation']['confirmation_id']})
    assert confirmed['accepted'] is True and confirmed['story']['state'] == 'scheduling'
    with svc.store.connection() as db:
        assert dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (original['id'],)).fetchone()) == original
        intents = [dict(row) for row in db.execute('SELECT * FROM publish_intents')]
        assert len(intents) == 2
        current = next(row for row in intents if row['id'] != original['id'])
        assert current['request_key'] != original['request_key'] and current['vibepublish_request_key'] != original['vibepublish_request_key']
        assert current['vibepublish_operation_id'] is None


@pytest.mark.parametrize('fault', ['unknown', 'incomplete', 'wrong_operation', 'wrong_resource', 'wrong_action',
    'generic_blocked', 'missing_dest', 'duplicate_dest', 'attempted', 'applied_revision', 'missing_applied_revision',
    'missing_attempt_id', 'other_missing_checks', 'whole_prior_time'])
def test_reconfirmation_requires_complete_bound_negative_effect_proof(tmp_path, fault):
    svc, _, session, _ = live_ready(tmp_path)
    def change(receipt, request):
        delivery = receipt['deliveries'][0]
        if fault == 'unknown':
            receipt['state'] = 'outcome_unknown'
        elif fault == 'incomplete':
            receipt['operation_complete'] = False
        elif fault == 'wrong_operation':
            receipt['operation_id'] = 'op_other'
        elif fault == 'wrong_resource':
            receipt['resource_id'] = 'pub_other'
        elif fault == 'wrong_action':
            receipt['action'] = 'update'
        elif fault == 'generic_blocked':
            receipt['error']['code'] = 'generic_provider_blocked'
        elif fault == 'missing_dest':
            receipt['deliveries'] = []
        elif fault == 'duplicate_dest':
            receipt['deliveries'].append(copy.deepcopy(delivery))
        elif fault == 'attempted':
            delivery['observed'] = 'outcome_unknown'
        elif fault == 'applied_revision':
            delivery['applied_revision'] = 1
        elif fault == 'missing_applied_revision':
            del delivery['applied_revision']
        elif fault == 'missing_attempt_id':
            del delivery['attempt_id']
        elif fault == 'other_missing_checks':
            delivery['missing_checks'].append('native_time_lead')
        elif fault == 'whole_prior_time':
            request['scheduled_for'] = future()
    original = closed_precision_intent(svc, session.resource_id, change=change)
    with pytest.raises(InvalidStateError):
        svc.mutate_publish(session.resource_id, 'new-corrected-intent', {
            'destinations': ['street_story_e2e_test'], 'scheduled_for': future()})
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 1
        assert dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (original['id'],)).fetchone()) == original


@pytest.mark.parametrize('invalid_receipt', ['[]', 'null', '{bad JSON', '{"error":"native_time_precision"}'])
def test_malformed_receipt_cannot_establish_safe_reconfirmation(tmp_path, invalid_receipt):
    svc, _, session, _ = live_ready(tmp_path)
    original = closed_precision_intent(svc, session.resource_id)
    with svc.store.tx() as db:
        db.execute('UPDATE publish_intents SET receipt_json=? WHERE id=?', (invalid_receipt, original['id']))
    with pytest.raises(InvalidStateError):
        svc.mutate_publish(session.resource_id, 'no-corrupt-proof', {
            'destinations': ['street_story_e2e_test'], 'scheduled_for': future()})
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 1
