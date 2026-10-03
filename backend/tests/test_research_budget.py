import json
from types import SimpleNamespace

import pytest

from test_facts_research_finish import fallback, findings, QUOTES
from street_story.research_budget import PAGE_UNITS, response_units
from tools.facts_research_acceptance import assess_gold, classify_failure


def test_negative_gold_and_failure_layers():
    facts = [{'fact_id': str(n), 'text': t, 'eligibility': 'eligible'} for n, t in enumerate([
        'Оттокар II не изображён на фасаде.', 'На фасаде изображён Фридрих III.', 'Реставратор Альбрехт работал с камнем.'])]
    result = {'facts': facts, 'state_ok': True, 'evidence': {'evidence': [{'fact_id': 'unrelated', 'evidence_id': 'e'}]}}
    assert not assess_gold(result, [])
    assert not assess_gold(result, [{'fact_id': f['fact_id'], 'supported_gold_relation': True, 'evidence_ids': ['e']} for f in facts])
    assert classify_failure({'code': 'RESOURCE_TOKEN_BUDGET'}) == 'BLOCKED_RESOURCE'
    assert classify_failure({'code': 'RESOURCE_CONTROL_UNAVAILABLE'}) == 'BLOCKED_AUTHORITY'
    assert classify_failure({'code': 'RESOURCE_LEASE_EXPIRED'}) == 'FAIL_CONTRACT'
    assert classify_failure({'status_code': 503}) == 'BLOCKED_PROVIDER'
    assert classify_failure({'code': 'invented_digest'}) == 'FAIL_CONTRACT'


@pytest.mark.asyncio
async def test_production_chunk_pages_and_pending_tail(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    import httpx
    body = '\n'.join('Строка ' + str(n) + ' кириллица и соседний контекст.' * 20 for n in range(18))
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<main>' + body.replace('\n', '<br>') + '</main>')))
    page = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    assert page['has_more_passages'] and response_units('get_research_chunk', page) <= PAGE_UNITS
    args = {'run_id': run_id, 'chunk_id': page['chunk_id'], 'batch_id': page['batch_id'], 'batch_index': page['batch_index'], 'expected_story_revision': page['expected_story_revision'], 'inventory_reviewed': True, 'facts': []}
    saved = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'page-save', 'args': args})
    assert saved['continuation_required']  # Unread tail cannot become no_claims.
    seen = set(p['passage_id'] for p in page['evidence_passages'])
    while page['has_more_passages']:
        page = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id, 'chunk_id': page['chunk_id'], 'passage_cursor': page['next_passage_cursor']}})
        assert response_units('get_research_chunk', page) <= PAGE_UNITS
        seen.update(p['passage_id'] for p in page['evidence_passages'])
    assert seen == session.state['research_passages_seen'][page['chunk_id']]
    control = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert control['review_available'] is False
    assert control['next_args'] == {'run_id': run_id}
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_packets').fetchone()[0] == 0
    from street_story.service import ConflictError
    with pytest.raises(ConflictError, match='passage'):
        await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id, 'chunk_id': page['chunk_id'], 'passage_cursor': 99999}})
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_installed_sdk_guard_on_real_route(tmp_path, monkeypatch):
    sdk = pytest.importorskip('ai_resource_control.client')
    from live_interaction.provider import _send_with_budget_wait
    import live_interaction.provider as provider
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    reply = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-before-denial', 'args': findings(chunk, QUOTES)})
    payload = {'toolResponse': {'functionResponses': [{'id': 'x' * 160, 'name': 'save_research_facts', 'response': {'result': reply}}]}}
    cost = sdk.estimate_input_tokens(payload)
    assert cost == response_units('save_research_facts', reply) <= PAGE_UNITS
    clock = SimpleNamespace(now=100.)
    class Authority:
        config = SimpleNamespace(grant_tokens=1024)
        occupied = True
        calls = 0
        def clock(self): return clock.now
        async def rpc(self, name, args):
            self.calls += 1
            if self.occupied or args['p_tokens'] > PAGE_UNITS:
                raise sdk.ResourceError('RESOURCE_TOKEN_BUDGET', 50)
            return {'lease_id': 'lease', 'fence': 7, 'ttl_ms': 90000, 'spend_ms': 30000, 'grant_seq': args['p_seq'], 'tokens': args['p_tokens']}, clock.now
    authority = Authority()
    lease = sdk.Lease(authority, 'lease', 'owner', 'fixture-key', 7)
    lease.deadline, lease.grant_deadline = 188., 0.
    class Socket:
        sent = []
        async def send(self, text): self.sent.append(json.loads(text))
    ws, events = Socket(), []
    async def refill(seconds):
        clock.now += seconds
        authority.occupied = False
    monkeypatch.setattr(provider.asyncio, 'sleep', refill)
    await _send_with_budget_wait(ws, payload, lease, events.append, 'tool_response', {'stopped': False, 'ws': ws})
    assert ws.sent == [payload] and authority.calls == 2
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM research_chunk_batches').fetchone()[0] == 1
    assert events[0]['type'] == 'resource_budget_wait'
    # Oversize cannot acquire a grant; no send occurs. Expiry fails closed too.
    with pytest.raises(sdk.ResourceError):
        await lease.before_send({'clientContent': {'text': 'я' * PAGE_UNITS}})
    clock.now = lease.deadline
    with pytest.raises(sdk.ResourceError, match='LEASE_EXPIRED'):
        await lease.before_send(payload)
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_pending_operation_continues_same_lease_bounded_and_author_stop_wins(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'saved', 'args': findings(chunk, QUOTES[:1], continuation=True)})
    written = []
    adapter.write = lambda owned, message: written.append((owned.id, message))
    session.awaiting_audio = True  # Shared host: intermediate boundary after tool response.
    adapter.on_event(session, {'type': 'turn_complete'})
    assert not written
    session.awaiting_audio = False  # Actual model response/playback arrived.
    adapter.on_event(session, {'type': 'turn_complete'})
    assert len(written) == 1 and written[0][0] == session.id
    assert run_id in written[0][1]['text'] and 'get_research_chunk' in written[0][1]['text']
    adapter.on_event(session, {'type': 'turn_complete'})
    assert len(written) == 1  # No repeat while the continuation is queued.
    adapter.input(session, {'text': 'Другой вопрос автора'})
    session.state['research_continuation_queued'] = False
    adapter.on_event(session, {'type': 'turn_complete'})
    assert len(written) == 1  # New author input preempts automatic continuation.
    session.state['research_author_interrupted'] = False
    adapter.on_event(session, {'type': 'turn_complete'})
    assert len(written) == 2
    session.state['research_continuation_queued'] = False
    adapter.on_event(session, {'type': 'turn_complete'})
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (run_id,)).fetchone()[0] == 'partial'
        assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 1
    adapter.on_stopped(session)
    adapter.on_event(session, {'type': 'turn_complete'})
    assert len(written) == 2
    await reader.search_http.aclose()
