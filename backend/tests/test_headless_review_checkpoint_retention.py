from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from street_story import headless_review_quotes, review_packets
from street_story.headless_fact_review import HeadlessFactReview, VERIFIER_PROMPT
from street_story.live import FUNCTIONS
from test_headless_fact_review_parallel import candidates, RUN


async def setup(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    session = SimpleNamespace(id='retained-review', resource_id=job['story_id'],
                              actor=None, closed=False, state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = engine._prepare_packet(job, RUN, session, ids)
    schema = headless_review_quotes.response_schema(packet, next(tool['parameters'] for tool in FUNCTIONS
                                                 if tool['name'] == 'finalize_fact_review'))
    return svc, job, engine, packet, unit, schema


@pytest.mark.parametrize('cooling_route', [False, True])
@pytest.mark.asyncio
async def test_closed_malformed_and_final_phase_keep_exact_input_and_nonrecoverable_answer(tmp_path, monkeypatch, cooling_route):
    svc, job, engine, packet, unit, schema = await setup(tmp_path)
    invalid = {'packet_ref': packet['packet_ref'], 'decisions': 'Malformed closed response.'}
    calls, writes = [], []
    async def closed(role, prompt, binding, actual_schema):
        assert actual_schema == schema and prompt.startswith(VERIFIER_PROMPT)
        calls.append('one-offline-closed-result')
        return {'result': deepcopy(invalid)}
    client = SimpleNamespace(model_id='fixture', directory='/fixture', _run=closed)
    route = {'provider_id': 'fixture', 'model_id': 'fixture', 'endpoint': 'frozen',
             'client': client, 'qualified': True, 'available': True}
    waiting = {**route, 'model_id': 'waiting', 'available': False}
    monkeypatch.setattr(engine, '_qualified_routes', lambda available=True:
                        [route] if available or not cooling_route else [route, waiting])
    async def run(story, role, received_unit, invoke, *, client):
        assert received_unit == unit
        return await invoke({})
    svc.providers.research = SimpleNamespace(run=run)
    put = engine._put
    def record(job, unit, value):
        writes.append(deepcopy(value))
        put(job, unit, value)
    monkeypatch.setattr(engine, '_put', record)
    assert await engine._infer(packet, job, unit, {}) is None
    malformed = next(value for value in writes if value.get('invalid_args') == invalid)
    final = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    for checkpoint in [malformed, final]:
        assert checkpoint['frozen_packet'] == packet and checkpoint['verifier_schema'] == schema
        assert checkpoint['verifier_prompt'] == VERIFIER_PROMPT
        assert checkpoint['route_identity'] == engine._route_identity(route)
        assert checkpoint['invalid_args'] == invalid and 'args' not in checkpoint
    assert final['phase'] == ('closed_error' if cooling_route else 'exhausted')
    assert await engine._infer(packet, job, unit, final) is None
    engine._recover_closed_reviews(job)
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit) == final
    assert calls == ['one-offline-closed-result']
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unacknowledged_abort_keeps_latest_original_frozen_input_without_send(tmp_path, monkeypatch):
    svc, job, engine, packet, unit, schema = await setup(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail('An original unresolved abort cannot dispatch another request.')
    client = SimpleNamespace(model_id='fixture', directory='/fixture', _run=forbidden)
    route = {'provider_id': 'fixture', 'model_id': 'fixture', 'endpoint': 'original',
             'client': client, 'qualified': True, 'available': True}
    original = {'phase': 'closed_error', 'packet_ref': packet['packet_ref'], 'frozen_packet': packet,
                'verifier_prompt': 'Exact original prompt. ', 'verifier_schema': schema,
                'verifier_contract_id': 'original-private-contract',
                'route_identity': engine._route_identity(route), 'diagnostic': 'retained-original'}
    engine._put(job, unit, original)
    receipt = {'phase': 'aborted', 'abort_acknowledged': False, 'binding': {'fact_unit_id': unit},
               'session_id': 'ses_original', 'message_id': 'msg_original'}
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-aborted', 'original-logical', job['story_id'], 'facts_review_fixture',
             json.dumps(receipt), svc.store.now(), svc.store.now()))
    monkeypatch.setattr(engine, '_qualified_routes', lambda available=True: [route])
    svc.providers.research = SimpleNamespace(run=forbidden)
    assert await engine._infer(packet, job, unit, {'phase': 'closed_error'}) is None
    retained = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    assert retained == {**original, 'phase': 'unknown', 'route': 'facts_review_fixture'}
    assert await engine._infer(packet, job, unit, retained) is None
