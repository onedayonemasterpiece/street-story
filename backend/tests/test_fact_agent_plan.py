"""New plans randomize healthy routes; original plans durably retain outcomes."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.db import Store
from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agents import _persist_plan, route_plan, run_fact_agents
from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import ConflictError, canonical
from test_headless_facts import fixture


def route(provider, model):
    return AgentRoute(provider, model, 'https://example.org', object())


@pytest.fixture
def fixed_shuffle(monkeypatch):
    monkeypatch.setattr('street_story.fact_agents.SystemRandom',
        lambda: SimpleNamespace(shuffle=lambda rows: rows.reverse()))


def test_new_plan_shuffles_copy_and_keeps_two_independent_services(fixed_shuffle):
    rows = [route('google', 'a'), route('google', 'b'), route('opencode', 'c'),
        route('gigachat', 'd'), route('gigachat', 'e')]
    original = list(rows)
    plan = route_plan(rows)
    assert rows == original
    assert plan == [rows[4], rows[2], rows[3], rows[1]]
    assert len({row.provider_id for row in plan[:2]}) == 2
    assert len({row.route_id for row in plan}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('proof_lost', [False, True])
async def test_completed_and_unknown_states_survive_store_reopen_without_replan_or_send(
        tmp_path, monkeypatch, fixed_shuffle, proof_lost):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [route('google', 'unknown'), route('gigachat', 'completed')]
    calls = []
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)

    async def invoke(_adapter, current, _story, context, *, binding, guard):
        guard()
        calls.append((current.route_id, copy.deepcopy(context), binding['attempt_id']))
        receipt = {'binding': binding, 'phase': 'unknown' if current.provider_id == 'google' else 'completed',
            'result': {'facts': []}}
        adapter._checkpoint_receipt(binding, receipt)
        if receipt['phase'] == 'unknown':
            raise RetryableProviderError('controlled_response_unknown')
        return {'result': receipt['result'], 'receipt': receipt}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    try:
        result = await run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        assert result['outcome'] == 'resource_blocked'
        stage = 'fact_agent_plan:headless-run'
        saved = service.store.checkpoint_get(job['id'], stage)
        assert saved['routes'][routes[0].route_id]['phase'] == 'unknown'
        assert saved['routes'][routes[1].route_id]['phase'] == 'completed'
        with service.store.connection() as db:
            journals = list(db.execute('SELECT attempt_id,receipt_json FROM research_provider_attempts ORDER BY attempt_id'))
        service.store = Store(service.store.path)
        monkeypatch.setattr('street_story.fact_agents.route_plan', lambda _routes: pytest.fail('Replanned original routes'))
        if proof_lost:
            routes.clear()
        result = await run_fact_agents(adapter, job, 'headless-run', 'Changed goal must not replace context')
        assert result['outcome'] == 'resource_blocked'
        assert len(calls) == 2
        assert service.store.checkpoint_get(job['id'], stage) == saved
        with service.store.connection() as db:
            assert [tuple(row) for row in db.execute(
                'SELECT attempt_id,receipt_json FROM research_provider_attempts ORDER BY attempt_id')] == [tuple(row) for row in journals]
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_slot', [False, True])
async def test_unknown_native_reopens_original_address_without_new_send_or_cooldown_reset(
        tmp_path, monkeypatch, fixed_shuffle, missing_slot):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [route('opencode', 'native'), route('gigachat', 'completed')]
    sends, readbacks = [], []
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)

    async def invoke(_adapter, current, _story, context, *, binding, guard):
        guard()
        if binding.get('phase') == 'unknown':
            readbacks.append(copy.deepcopy(binding))
            assert binding['session_id'] == 'ses_original' and binding['message_id'] == 'msg_original'
        else:
            sends.append((current.route_id, binding['attempt_id'], copy.deepcopy(context)))
            if current.provider_id == 'opencode':
                adapter._checkpoint_receipt(binding, {'binding': binding, 'phase': 'unknown',
                    'session_id': 'ses_original', 'message_id': 'msg_original'})
                raise RetryableProviderError('controlled_response_unknown')
        receipt = {'binding': binding, 'phase': 'completed', 'result': {'facts': []}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'result': receipt['result'], 'receipt': receipt}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    try:
        with pytest.raises(RetryableProviderError, match='research_agent_original_pending'):
            await run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        stage = 'fact_agent_plan:headless-run'
        original = service.store.checkpoint_get(job['id'], stage)
        assert original['routes'][routes[0].route_id]['phase'] == 'unknown'
        original_attempt = original['routes'][routes[0].route_id]['attempt_id']
        if missing_slot:
            interrupted = copy.deepcopy(original)
            interrupted['routes'].pop(routes[0].route_id)
            service.store.checkpoint_put(job['id'], stage, interrupted)
        health_key = 'research-route-health:' + hashlib.sha256(canonical([
            routes[0].endpoint, routes[0].model_id, routes[0].directory,
            routes[0].profile_fingerprint]).encode()).hexdigest()
        health = {'category': 'research_provider_quota', 'retry_at': service.store.now()+3600}
        service.store.cache_put(health_key, health, 3600)
        service.store = Store(service.store.path)
        monkeypatch.setattr('street_story.fact_agents.route_plan', lambda _routes: pytest.fail('Replanned native original'))
        await run_fact_agents(adapter, job, 'headless-run', 'Changed goal')
        saved = service.store.checkpoint_get(job['id'], stage)
        assert saved['context'] == original['context'] and saved['route_ids'] == original['route_ids']
        assert saved['routes'][routes[0].route_id]['phase'] == 'completed'
        assert saved['routes'][routes[0].route_id]['attempt_id'] == original_attempt
        assert len(sends) == 2 and len(readbacks) == 1
        assert readbacks[0]['attempt_id'] == original_attempt
        assert service.store.cache_get(health_key) == health
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('health_kind', ['route', 'quota'])
async def test_only_new_plan_excludes_existing_provider_cooldown(tmp_path, monkeypatch, fixed_shuffle, health_kind):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [route('google', 'blocked'), route('gigachat', 'healthy'), route('opencode', 'healthy')]
    blocked = routes[0]
    key = ('research-quota-health:google:blocked' if health_kind == 'quota' else
        'research-route-health:' + hashlib.sha256(canonical([
            blocked.endpoint, blocked.model_id, blocked.directory, blocked.profile_fingerprint]).encode()).hexdigest())
    health = {'category': 'research_provider_quota', 'status': 429, 'retry_at': service.store.now()+3600}
    service.store.cache_put(key, health, 3600)
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)
    sends = []

    async def invoke(_adapter, current, _story, _context, *, binding, guard):
        guard()
        sends.append(current.route_id)
        receipt = {'phase': 'completed', 'binding': binding, 'result': {'facts': []}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'result': receipt['result'], 'receipt': receipt}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    try:
        await run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        saved = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')
        assert set(saved['route_ids']) == {row.route_id for row in routes[1:]}
        assert set(sends) == set(saved['route_ids'])
        assert service.store.cache_get(key) == health
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fence', ['stop', 'generation', 'control', 'photo'])
async def test_plan_progress_cannot_replace_inputs_or_write_after_owner_fence(tmp_path, fence):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = SimpleNamespace(service=service)
    headless = HeadlessFacts(service)
    initial = {'pipeline': 'original', 'route_ids': ['one', 'two'], 'context': {'goal': 'Frozen'}, 'routes': {}}
    try:
        _persist_plan(adapter, job, 'headless-run', headless, 0, initial=initial)
        _persist_plan(adapter, job, 'headless-run', headless, 0, route_id='one', state={'phase': 'completed'})
        replacement = {**initial, 'context': {'goal': 'Changed'}, 'route_ids': ['outside']}
        saved = _persist_plan(adapter, job, 'headless-run', headless, 0, initial=replacement)
        assert saved == {**initial, 'routes': {'one': {'phase': 'completed'}}}
        assert _persist_plan(adapter, job, 'headless-run', headless, 0,
            route_id='one', state={'phase': 'unknown'}) == saved
        with pytest.raises(ConflictError, match='outside the original'):
            _persist_plan(adapter, job, 'headless-run', headless, 0, route_id='outside', state={'phase': 'unknown'})
        with service.store.tx() as db:
            story = service._story_row(db, job['story_id'])
            research = json.loads(story['research_json'])
            if fence == 'stop':
                research['fact_research_cancelled'] = True
            elif fence == 'generation':
                research['identity_generation'] = 1
            elif fence == 'control':
                research.setdefault('research_controls', {}).setdefault('facts', {})['revision'] = 1
            else:
                db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b'*64, story['id']))
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        with pytest.raises(ConflictError, match='Owner selection or Stop changed'):
            _persist_plan(adapter, job, 'headless-run', headless, 0, route_id='two', state={'phase': 'completed'})
        assert service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run') == saved
        if fence != 'control':
            assert await run_fact_agents(adapter, job, 'headless-run', 'Forbidden new goal') is None
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 0
    finally:
        await reader.search_http.aclose()
