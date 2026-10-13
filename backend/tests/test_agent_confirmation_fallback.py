"""Qualified alternate confirmation over real SDK/journal/ledger boundaries."""
import json
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from street_story.agent_evidence import AgentEvidenceMixin, SCHEMA
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agents import QUALIFICATION_KEY, configured_routes
from test_agent_evidence import AGENT, CANDIDATE
from test_gigachat_research import Admission, client
from test_headless_facts import fixture


@pytest.fixture(autouse=True)
def controlled_sdk_estimator(monkeypatch):
    sdk = ModuleType('ai_resource_control.client')
    sdk.estimate_input_tokens = lambda body: len(json.dumps(body).encode())
    monkeypatch.setitem(sys.modules, 'ai_resource_control.client', sdk)


async def setup(tmp_path, *, mode='supported', qualified=True, google_unknown=False):
    service, job, _, reader, fetches = await fixture(tmp_path, text=
        'The gate opened as a documented museum in 2005. Facade restored in 1998. Exhibition opened in 2010. '
        'The neighboring house opened in 1930. As of 2025, repairs were underway.')
    service.__class__ = type('ConfirmedService', (AgentEvidenceMixin, type(service)), {})
    admission, bodies = Admission(), []
    async def provider(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert not any(key in body for key in ('tools', 'functions', 'function_call'))
        assert body['max_tokens'] == 2048 and body['response_format']['schema'] == SCHEMA
        payload = json.loads(body['messages'][1]['content'].removeprefix('INPUT='))
        assert payload['source']['chunks'] and payload['review_as_of_utc']
        if mode == 'unknown':
            raise httpx.ReadTimeout('Controlled transport loss after send')
        if mode == 'http_closed':
            return httpx.Response(503)
        row = {'verdict': 'supported' if mode in {'supported', 'malformed'} else 'insufficient',
            'text': payload['candidate']['text'], 'claim_key': payload['candidate']['text'],
            'subject_scope': 'selected_building', 'confirmed_fragment': payload['candidate']['quote'],
            'source_date': None, 'chunk_ids': [payload['source']['chunks'][0]['chunk_id']],
            'reason': 'Controlled model rejects wrong subject/date.' if mode != 'supported' else 'Controlled own-source support.',
            'existing_fact_id': '', 'conflicts_with_existing': []}
        text = 'malformed' if mode == 'malformed' else json.dumps(row)
        return httpx.Response(200, headers={'x-request-id': 'controlled-actual-request'}, json={
            'model': 'GigaChat-2:controlled', 'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40}})
    giga = client(provider, admission)
    giga._token, giga._expires = 'controlled-token', giga._clock() + 1000
    route = AgentRoute('gigachat', giga.binding['model'], 'https://api.giga.chat/v1/chat/completions', giga)
    if qualified:
        service.store.cache_put(QUALIFICATION_KEY, {'routes': [{'route_id': route.route_id,
            'profile_sha256': route.profile_fingerprint, 'contract': QUALIFICATION_KEY,
            'tool_access_verified': True, 'tool_safety_verified': True, 'qualification_receipts': ['unit-controlled-proof']}]}, 3600)
    adapter = SimpleNamespace(service=service, giga=giga)
    adapter.fact_agent_routes = lambda: configured_routes(adapter)
    service.providers.research = adapter
    async def google(payload, *, checkpoint, guard):
        checkpoint({'attempt_id': 'google-original', 'provider_id': 'google', 'model_id': 'configured-google',
            'phase': 'unknown' if google_unknown else 'failed',
            'provider_send_state': 'possibly_sent' if google_unknown else 'response_closed', 'http_status': None if google_unknown else 503})
        return {'status': 'unverified'} if google_unknown else {'status': 'unverified', 'reason': 'confirmation_routes_unavailable'}
    service.providers.gemini.verify_agent_evidence = google
    return service, job, reader, giga, bodies, admission, fetches


async def close(reader, giga):
    await reader.search_http.aclose()
    await giga.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['closed', 'unknown', 'not_sent'])
async def test_google_explicit_fallback_reason_requires_every_original_response_closed(mode):
    from google.genai.errors import ServerError
    from street_story.agent_evidence import verify_with_gemini
    from street_story.errors import RetryableProviderError
    saved = []
    async def generate(*args, **kwargs):
        if mode != 'not_sent':
            kwargs['before_provider_send']()
        if mode == 'closed':
            raise ServerError(503, {'error': {'message': 'Controlled unavailable', 'status': 'UNAVAILABLE'}})
        if mode == 'unknown':
            raise httpx.ReadTimeout('Controlled unaddressed inference')
        error = RetryableProviderError('controlled_admission_refused')
        error.provider_send_state = 'not_sent'
        raise error
    async def execute(_operation, call, **kwargs):
        return await call('controlled-key', 1)
    gemini = SimpleNamespace(_generate=generate, store=SimpleNamespace(now=lambda: 10),
        research_routes=[('route-one', None, None, SimpleNamespace(execute=execute)),
            ('route-two', None, None, SimpleNamespace(execute=execute))])
    result = await verify_with_gemini(gemini, {'source': {'source_version_id': 'version', 'content_sha256': 'hash'}},
        lambda receipt: saved.append(dict(receipt)), lambda: None)
    if mode == 'closed':
        assert result['reason'] == 'confirmation_routes_unavailable' and len(result['closed_failures']) == 2
        assert all(row['http_status'] == 503 and row['provider_send_state'] == 'response_closed' for row in result['closed_failures'])
    elif mode == 'unknown':
        assert 'reason' not in result and result['receipt']['phase'] == 'unknown'
        assert len({row['attempt_id'] for row in saved}) == 1
    else:
        assert result['reason'] == 'confirmation_routes_not_sent'
        assert all(row['provider_send_state'] == 'not_sent' for row in result['closed_failures'])


@pytest.mark.asyncio
async def test_qualified_alternate_commits_three_distinct_facts_to_normal_ledger_and_memory(tmp_path):
    service, job, reader, giga, bodies, admission, _ = await setup(tmp_path)
    try:
        candidates = [CANDIDATE, {**CANDIDATE, 'text': 'Facade restored in 1998.', 'quote': 'Facade restored in 1998.'},
            {**CANDIDATE, 'text': 'Exhibition opened in 2010.', 'quote': 'Exhibition opened in 2010.'}]
        for candidate in candidates:
            assert (await service.confirm_agent_fact(job, 'headless-run', candidate, AGENT))['status'] == 'supported'
        assert len(bodies) == 3
        assert len(service.story(job['story_id'])['facts']) == 3
        assert all(row['eligibility'] == 'eligible' for row in service.story(job['story_id'])['facts'])
        with service.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 3
            proofs = [json.loads(row[0]) for row in db.execute('SELECT accepted_basis_json FROM fact_assertions')]
            assert all(row['operation']['confirmation_model'] == 'GigaChat-2' for row in proofs)
            receipts = [json.loads(row[0]) for row in db.execute('SELECT receipt_json FROM research_provider_attempts')]
            assert any(row.get('provider_id') == 'google' and row['phase'] == 'failed' for row in receipts)
            assert sum(row.get('provider_id') == 'gigachat' and row.get('result') is not None for row in receipts) == 3
        assert all(workload['max_provider_sends'] == 2 for _, workload in admission.bindings)
        assert all(row['cost'] == 'unknown' and row['usage_known'] for row in admission.finalized)
    finally:
        await close(reader, giga)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['wrong_subject', 'wrong_date', 'malformed', 'http_closed', 'unknown'])
async def test_alternate_nonpositive_or_unknown_never_admits_and_never_resends_on_reopen(tmp_path, mode):
    service, job, reader, giga, bodies, admission, _ = await setup(tmp_path, mode=mode)
    try:
        candidate = {**CANDIDATE, 'text': 'The selected house opened in 1930.',
            'quote': 'The neighboring house opened in 1930.'} if mode == 'wrong_subject' else {
            **CANDIDATE, 'text': 'Repairs are currently underway.', 'quote': 'As of 2025, repairs were underway.'} if mode == 'wrong_date' else CANDIDATE
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, AGENT)
        assert outcome['status'] == ('insufficient' if mode in {'wrong_subject', 'wrong_date'} else 'unverified')
        assert service.story(job['story_id'])['facts'] == []
        await service.confirm_agent_fact(job, 'headless-run', candidate, AGENT)
        assert len(bodies) == 1
        with service.store.connection() as db:
            receipts = [json.loads(row[0]) for row in db.execute('SELECT receipt_json FROM research_provider_attempts')]
            target = [row for row in receipts if row.get('provider_id') == 'gigachat' and row.get('model_attempts')][-1]
            assert target['phase'] == ('unknown' if mode == 'unknown' else 'failed' if mode in {'malformed', 'http_closed'} else 'completed')
        assert admission.finalized[-1]['cost'] == 'unknown'
    finally:
        await close(reader, giga)


@pytest.mark.asyncio
@pytest.mark.parametrize('qualified,google_unknown', [(False, False), (True, True)])
async def test_missing_current_proof_or_google_unknown_never_sends_alternate(tmp_path, qualified, google_unknown):
    service, job, reader, giga, bodies, admission, _ = await setup(tmp_path, qualified=qualified, google_unknown=google_unknown)
    try:
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT))['status'] == 'unverified'
        assert not bodies and not admission.bindings
    finally:
        await close(reader, giga)


@pytest.mark.asyncio
async def test_closed_giga_response_recovers_after_local_interruption_without_any_new_read_or_send(tmp_path, monkeypatch):
    service, job, reader, giga, bodies, _, fetches = await setup(tmp_path)
    from street_story import agent_evidence
    original = agent_evidence.verify_with_gigachat
    interrupted = False
    async def closed_then_interrupted(*args, **kwargs):
        nonlocal interrupted
        result = await original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise RuntimeError('Controlled interruption after closed receipt')
        return result
    monkeypatch.setattr(agent_evidence, 'verify_with_gigachat', closed_then_interrupted)
    try:
        with pytest.raises(RuntimeError, match='Controlled interruption'):
            await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert service.story(job['story_id'])['facts'] == []
        reads = len(fetches)
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT))['status'] == 'supported'
        assert len(bodies) == 1 and len(fetches) == reads
    finally:
        await close(reader, giga)
