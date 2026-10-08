import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story.research_reconciliation import reconcile_terminal_attempts
from street_story.service import canonical
from test_visual_search_continuation import prepared


SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
    'required': ['summary'], 'additionalProperties': False}


def pending(tmp_path, *, native=False):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    now = [1000.0]
    svc.store.now = lambda: now[0]
    binding = {'story_id': story['id'], 'photo_sha256': story['photo_sha256'], 'generation': 0,
        'purpose': 'identity', 'control_revision': 0, 'job_id': 'original-job', 'job_attempt': 2,
        'attempt_id': 'original-attempt', 'request_id': 'original-logical'}
    receipt = {'binding': binding, 'phase': 'unknown', 'provider_send_state': 'possibly_sent',
        'provider_id': 'original-provider', 'model_id': 'original-model', 'role': 'facts',
        'frozen_schema': SCHEMA, 'frozen_prompt': 'original exact prompt',
        **({'thread_id': 'original-thread', 'turn_id': 'original-turn'} if native else
           {'session_id': 'ses_original', 'message_id': 'msg_original'})}
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, story['id'])['research_json'])
        research['automatic_research_outcome'] = {'outcome': 'deadline_exceeded', 'finished_at': now[0],
            'photo_sha256': story['photo_sha256'], 'identity_generation': 0}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        db.execute('INSERT INTO jobs(id,story_id,kind,semantic_key,payload_json,state,attempts,available_at,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)', ('original-job', story['id'], 'identity_visual', 'original-job-key',
                canonical({'photo_sha256': story['photo_sha256'], 'identity_generation': 0}), 'done', 2, now[0], now[0], now[0]))
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-attempt', 'original-logical', story['id'], 'facts_opencode', canonical(receipt), now[0], now[0]))
    return svc, story, now, receipt


def saved(svc):
    with svc.store.connection() as db:
        return json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts '
            'WHERE attempt_id=?', ('original-attempt',)).fetchone()[0])


def original_messages(*, completed=False):
    return [{'info': {'id': 'msg_original'}, 'parts': [{'type': 'text', 'text': 'original exact prompt'}]},
        {'info': {'id': 'assistant-original', 'parentID': 'msg_original', 'providerID': 'original-provider',
             'modelID': 'original-model', 'time': {'completed': 1} if completed else {}, 'finish': 'stop'},
         'parts': [{'type': 'text', 'text': '{"summary":"Original response"}'}]}]


def adapter(svc, request):
    client = SimpleNamespace(provider_id='original-provider', model_id='original-model',
        client=None, _request=request)
    svc.providers.research = SimpleNamespace(client=client)


@pytest.mark.asyncio
async def test_original_id_readback_after_job_done_validates_schema_without_product_changes(tmp_path):
    svc, story, _now, original = pending(tmp_path)
    calls = []
    async def request(client, method, path, **kwargs):
        calls.append((method, path))
        assert method == 'GET' and path == '/session/ses_original/message'
        return original_messages(completed=True)
    adapter(svc, request)
    before = svc._identity_snapshot(story['id'])
    result = await reconcile_terminal_attempts(svc, story_id=story['id'])
    assert result == {'observed': 1, 'closed': 1}
    receipt = saved(svc)
    assert receipt['session_id'] == original['session_id'] and receipt['message_id'] == original['message_id']
    assert receipt['binding'] == original['binding']
    assert receipt['phase'] == 'response_completed' and receipt['retry_safe'] is False
    late = receipt['late_provider_response']
    assert late['result'] == {'summary': 'Original response'} and late['frozen_schema_validated']
    assert late['product_projection_allowed'] is False
    assert svc._identity_snapshot(story['id']) == before
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE id=?', ('original-job',)).fetchone()[0] == 'done'
        assert db.execute('SELECT count(*) FROM research_provider_attempts').fetchone()[0] == 1
    assert await reconcile_terminal_attempts(svc) == {'observed': 0, 'closed': 0}
    assert calls == [('GET', '/session/ses_original/message')]


@pytest.mark.asyncio
async def test_unknown_original_remains_bounded_and_never_resends(tmp_path):
    svc, story, now, original = pending(tmp_path)
    calls = []
    async def request(client, method, path, **kwargs):
        calls.append((method, path))
        return original_messages()
    adapter(svc, request)
    for elapsed in [0, 1, 20, 21, 40, 41, 59, 61, 80]:
        now[0] = 1000 + elapsed
        await reconcile_terminal_attempts(svc)
    receipt = saved(svc)
    assert len(calls) == 3 and all(method == 'GET' for method, _path in calls)
    assert receipt['phase'] == 'unknown' and receipt['provider_send_state'] == 'possibly_sent'
    assert receipt['terminal_reconciliation']['read_count'] == 3
    assert receipt['terminal_reconciliation']['status'] in {'expired', 'exhausted'}
    assert receipt['binding'] == original['binding']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['photo', 'generation', 'control', 'job_attempt', 'resumed'])
async def test_changed_scope_is_fenced_before_any_network_read(tmp_path, change):
    svc, story, _now, original = pending(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail('Changed owner/photo/generation/job cannot be observed in this terminal scope')
    adapter(svc, forbidden)
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        if change == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('new-photo', story['id']))
        elif change == 'generation':
            research['identity_generation'] = 1
        elif change == 'control':
            research['research_controls'] = {'identity': {'revision': 1}}
        elif change == 'job_attempt':
            db.execute('UPDATE jobs SET attempts=3 WHERE id=?', ('original-job',))
        else:
            research.pop('automatic_research_outcome')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    assert await reconcile_terminal_attempts(svc) == {'observed': 0, 'closed': 0}
    assert saved(svc) == original


@pytest.mark.asyncio
async def test_owner_change_while_original_read_in_flight_blocks_receipt_commit(tmp_path):
    svc, story, _now, original = pending(tmp_path)
    async def request(*args, **kwargs):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            research['identity_generation'] = 1
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        return original_messages(completed=True)
    adapter(svc, request)
    assert await reconcile_terminal_attempts(svc) == {'observed': 1, 'closed': 0}
    receipt = saved(svc)
    assert receipt['phase'] == 'unknown' and 'late_provider_response' not in receipt
    assert receipt['binding'] == original['binding']


@pytest.mark.asyncio
async def test_native_terminal_observer_only_reads_exact_original_turn(tmp_path):
    svc, story, _now, original = pending(tmp_path, native=True)
    calls = []
    async def request(method, payload, **kwargs):
        calls.append((method, payload))
        assert method == 'thread/read' and payload['threadId'] == 'original-thread'
        return {'thread': {'turns': [{'id': 'unrelated', 'status': 'completed', 'items': []},
            {'id': 'original-turn', 'status': 'completed', 'items': [
                {'type': 'agentMessage', 'text': '{"summary":"Original response"}'}]}]}}
    svc.providers.research = SimpleNamespace(native_vision=SimpleNamespace(client=SimpleNamespace(request=request)))
    assert await reconcile_terminal_attempts(svc) == {'observed': 1, 'closed': 1}
    assert saved(svc)['turn_id'] == original['turn_id'] and len(calls) == 1


@pytest.mark.asyncio
async def test_observer_timeout_is_small_and_cannot_call_abort_or_send(tmp_path):
    svc, story, _now, original = pending(tmp_path)
    calls = []
    async def request(client, method, path, **kwargs):
        calls.append(method)
        await asyncio.Event().wait()
    adapter(svc, request)
    assert await asyncio.wait_for(reconcile_terminal_attempts(svc, timeout_seconds=.02), .5) == {'observed': 1, 'closed': 0}
    assert calls == ['GET'] and saved(svc)['phase'] == 'unknown'


@pytest.mark.asyncio
async def test_restart_after_technical_window_leaves_unknown_without_network(tmp_path):
    svc, _story, now, original = pending(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail('Expired observer window never restarts on recovery')
    adapter(svc, forbidden)
    now[0] = 1061
    assert await reconcile_terminal_attempts(svc) == {'observed': 0, 'closed': 0}
    receipt = saved(svc)
    assert receipt['phase'] == 'unknown' and receipt['binding'] == original['binding']
    assert receipt['terminal_reconciliation']['status'] == 'expired'
