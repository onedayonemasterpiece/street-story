"""Closed external no-send proof can recover a failed publication's next card."""
import copy
import json

import pytest

from street_story.service import ConflictError, InvalidStateError, canonical
from test_publication_precision import closed_precision_intent, future, live_ready


def accepted_intent(svc, sid):
    def expired(receipt, request):
        receipt['error']['code'] = 'command_expired'
        receipt['deliveries'][0]['missing_checks'] = ['command_expired']
        request['scheduled_for'] = future()
    original = closed_precision_intent(svc, sid, change=expired)
    terminal = json.loads(original['receipt_json'])
    accepted = {'operation_id': 'op_original', 'resource_id': 'pub_original',
                'action': 'publish', 'state': 'accepted', 'operation_complete': False,
                '_publish_dispatch': {'phase': 'possibly_sent', 'request_key': 'original-key'}}
    with svc.store.tx() as db:
        db.execute("UPDATE publish_intents SET state='accepted',receipt_json=? WHERE id=?",
                   (canonical(accepted), original['id']))
        # Editing/revalidating the draft clears the old generic error, without
        # changing the publication's failed job or its still-unobserved outcome.
        db.execute('UPDATE stories SET error_code=NULL WHERE id=?', (sid,))
    return original, terminal


@pytest.mark.asyncio
async def test_prepare_observes_original_failed_command_before_new_explicit_confirmation(tmp_path):
    svc, adapter, session, _ = live_ready(tmp_path)
    original, terminal = accepted_intent(svc, session.resource_id)
    reads = []
    async def status(operation_id):
        reads.append(operation_id)
        return {'receipts': [terminal]}
    svc.providers.vibepublish.status = status
    card = await adapter._prepare_publication(session.resource_id, 'new-card', {
        'destinations': ['street_story_e2e_test'], 'scheduled_for': future(), 'timezone': 'UTC'})
    assert reads == ['op_original']
    with svc.store.connection() as db:
        assert db.execute('SELECT count(*) FROM publish_intents').fetchone()[0] == 1
        saved = dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (original['id'],)).fetchone())
        assert saved['request_json'] == original['request_json']
        assert saved['vibepublish_request_key'] == original['vibepublish_request_key']
        assert saved['state'] == 'blocked'
        assert json.loads(saved['receipt_json'])['_publish_dispatch']['phase'] == 'possibly_sent'
    result = adapter._confirm_publication(session.resource_id, 'separate-owner-confirmation', {
        'confirmation_id': card['confirmation']['confirmation_id']})
    assert result['accepted'] is True
    with svc.store.connection() as db:
        assert db.execute('SELECT count(*) FROM publish_intents').fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='publish' AND state='failed'").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['pending', 'unknown', 'attempted', 'wrong_operation', 'wrong_resource', 'missing_delivery'])
async def test_observation_never_turns_uncertain_or_foreign_delivery_into_a_new_send(tmp_path, fault):
    svc, adapter, session, _ = live_ready(tmp_path)
    original, terminal = accepted_intent(svc, session.resource_id)
    reply = copy.deepcopy(terminal)
    if fault == 'pending':
        reply.update(state='accepted', operation_complete=False)
    elif fault == 'unknown':
        reply['state'] = 'outcome_unknown'
    elif fault == 'attempted':
        reply['deliveries'][0]['observed'] = 'outcome_unknown'
    elif fault == 'wrong_operation':
        reply['operation_id'] = 'op_foreign'
    elif fault == 'wrong_resource':
        reply['resource_id'] = 'pub_foreign'
    elif fault == 'missing_delivery':
        reply['deliveries'] = []
    async def status(operation_id):
        assert operation_id == 'op_original'
        return {'receipts': [reply]}
    svc.providers.vibepublish.status = status
    with pytest.raises((ConflictError, InvalidStateError)):
        card = await adapter._prepare_publication(session.resource_id, 'unsafe-card', {
            'destinations': ['street_story_e2e_test'], 'scheduled_for': future(), 'timezone': 'UTC'})
        adapter._confirm_publication(session.resource_id, 'unsafe-confirm', {
            'confirmation_id': card['confirmation']['confirmation_id']})
    with svc.store.connection() as db:
        assert db.execute('SELECT count(*) FROM publish_intents').fetchone()[0] == 1
        saved = dict(db.execute('SELECT * FROM publish_intents WHERE id=?', (original['id'],)).fetchone())
        assert saved['vibepublish_request_key'] == original['vibepublish_request_key']
        assert saved['request_json'] == original['request_json']


@pytest.mark.asyncio
async def test_failed_job_resumed_during_status_read_fences_the_observation(tmp_path):
    svc, adapter, session, _ = live_ready(tmp_path)
    original, terminal = accepted_intent(svc, session.resource_id)
    async def status(operation_id):
        assert operation_id == 'op_original'
        with svc.store.tx() as db:
            db.execute("UPDATE jobs SET state='retry' WHERE kind='publish' AND story_id=?", (session.resource_id,))
        return {'receipts': [terminal]}
    svc.providers.vibepublish.status = status
    with pytest.raises(ConflictError) as stale:
        await adapter._prepare_publication(session.resource_id, 'raced-card', {
            'destinations': ['street_story_e2e_test'], 'scheduled_for': future(), 'timezone': 'UTC'})
    assert stale.value.code == 'publication_observation_stale'
    with svc.store.connection() as db:
        assert db.execute('SELECT count(*) FROM publish_intents').fetchone()[0] == 1
        intent = db.execute('SELECT * FROM publish_intents WHERE id=?', (original['id'],)).fetchone()
        assert intent['state'] == 'accepted'
        assert json.loads(intent['receipt_json'])['operation_complete'] is False
