"""Retained extraction and ordinary model admission without search/page traffic."""
import copy
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

from street_story.fact_agent_providers import (
    AgentRoute, OWNED_SOURCE_CONTRACT, _source_packet_sha, retained_source_cohort,
    run_retained_source_agent,
)
from street_story.headless_facts import HeadlessFacts
from street_story.opencode_research import ResearchUnavailable
from street_story.research_runs import persist_source_version
from test_agent_evidence import CANDIDATE, setup
from test_headless_facts import URL


async def owned(tmp_path, phase='unknown', read_status='complete'):
    service, job, reader, fetches, judges = await setup(tmp_path)
    story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
    context = {'run_id': 'headless-run', 'selected_object': {'candidate_id': 'wiki:77'}}
    binding = {'attempt_id': 'parent-google', 'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
        'generation': story['_identity_generation'], 'control_revision': story['_fact_research_control_revision'],
        'job_id': job['id'], 'job_attempt': job['attempts']}
    with service.store.tx() as db:
        version = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
            title='Saved history', content_type='text/plain', http_status=200, redirect_chain=[],
            normalized_text='The complete own page includes the original architecture and historical tenant tail.',
            read_status=read_status, access_scope='public', now=service.store.now())
        source = {**version, 'requested_url': URL, 'final_url': URL}
        original = {'binding': binding, 'phase': phase, 'provider_send_state': 'possibly_sent',
            'provider_id': 'google', 'prompt_sha256': 'original-frozen-input', 'sources': [source],
            'tool_receipts': [{'name': 'webfetch', 'arguments': {'url': URL}, 'completed_at': 123}],
            'result': {'facts': []}}
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('parent-google', 'parent-unit', story['id'], 'facts_agent_original', json.dumps(original), 1, 1))
    def checkpoint(binding, receipt):
        with service.store.tx() as db:
            db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(attempt_id) DO UPDATE SET receipt_json=excluded.receipt_json',
                (binding['attempt_id'], binding['attempt_id'], story['id'],
                 'facts_agent_retained_' + receipt.get('route_id', 'child'), json.dumps(receipt), 1, 1))
    adapter = SimpleNamespace(service=service, _checkpoint_receipt=checkpoint,
        attempt=lambda _story, _role, unit: ({**binding, 'attempt_id': 'child-' + str(unit['turn'])}, None))
    cohort = retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None)
    return adapter, service, job, story, context, original, cohort, reader, fetches, judges


def metadata(route, cohort):
    return {'route_id': route.route_id, 'provider_id': route.provider_id, 'model_id': route.model_id,
        'attempt_id': 'new-extraction', 'original_attempt_id': 'parent-google',
        'retained_source_cohort_sha256': cohort['source_cohort_sha256']}


async def extract(adapter, story, context, original, cohort, provider, monkeypatch, *, tool_call=False):
    bodies, finalized = [], []
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('Retained extraction/confirmation must not search or fetch')
    adapter.service.providers.gemini._fetch_page_documents = forbidden
    adapter.service.providers.gemini._public_web_search = forbidden
    if provider == 'gigachat':
        sdk = ModuleType('ai_resource_control.client')
        sdk.estimate_input_tokens = lambda body: len(json.dumps(body))
        monkeypatch.setitem(sys.modules, 'ai_resource_control.client', sdk)
        async def finalize(usage, state):
            finalized.append((usage, state))
        @asynccontextmanager
        async def admission(binding, workload):
            assert workload['purpose'] == 'fact_research' and workload['estimated_input_tokens'] > 0
            assert binding['attempt_id'] != 'parent-google'
            yield SimpleNamespace(finalize=finalize)
        async def completion(_lease, body, receipts, _purpose, _output, before):
            bodies.append(body)
            assert 'functions' not in body and body['function_call'] == 'none'
            assert 'historical tenant tail' in json.dumps(body)
            await before({'request_id': 'controlled-extraction'})
            receipts.append({'status': 'succeeded', 'http_status': 200,
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
            return ({'function_call': {'name': 'webfetch', 'arguments': {'url': URL}}} if tool_call else
                {'content': json.dumps({'facts': [CANDIDATE]})}), 'stop'
        client = SimpleNamespace(binding={'provider': provider}, admission=admission, _completion=completion)
    else:
        class Executor:
            async def execute(self, purpose, call, **_kwargs):
                assert purpose == 'grounded_research'
                return await call('controlled-key', 20)
        async def generate(_key, _timeout, contents, config, **kwargs):
            bodies.append(config)
            assert config.tools is None and config.tool_config is None
            assert 'historical tenant tail' in contents[0].parts[0].text
            kwargs['before_provider_send']()
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=SimpleNamespace(name='webfetch', args={'url': URL}) if tool_call else None)]))],
                text=json.dumps({'facts': [CANDIDATE]}), response_id='controlled-extraction', usage_metadata=None)
        client = SimpleNamespace(research_routes=[('qualified-model', None, None, Executor())], _generate=generate)
    route = AgentRoute(provider, 'qualified-model', 'https://provider.example', client)
    binding = {**original['binding'], 'attempt_id': 'new-extraction'}
    result = await run_retained_source_agent(adapter, route, story, context, cohort,
        original_receipt=original, binding=binding, guard=lambda: None)
    return result, route, bodies, finalized


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['google', 'gigachat'])
async def test_owned_UNKNOWN_read_extracts_separately_then_confirms_without_network(tmp_path, monkeypatch, provider):
    adapter, service, job, story, context, original, cohort, reader, fetches, judges = await owned(tmp_path)
    try:
        result, route, bodies, finalized = await extract(adapter, story, context, original, cohort, provider, monkeypatch)
        assert len(bodies) == 1 and result['result']['facts'] == [CANDIDATE]
        assert result['receipt']['contract'] == OWNED_SOURCE_CONTRACT
        assert result['receipt']['retained_source_packet'] == cohort
        if provider == 'gigachat':
            assert len(finalized) == 1 and finalized[0][1] == 'completed'
            assert finalized[0][0]['input_tokens'] == 10
        agent = metadata(route, cohort)
        outcome = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, agent)
        assert outcome['status'] == 'supported'
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, agent)) == outcome
        assert len(judges) == 1 and fetches == []
        assert service.story(story['id'])['facts'][0]['eligibility'] == 'eligible'
        with service.store.connection() as db:
            assert json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                ('parent-google',)).fetchone()[0]) == original
            confirmation = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='agent_evidence_confirmation'").fetchone()[0])
            assert confirmation['retained_source_provenance']['extraction_attempt_id'] == 'new-extraction'
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            assert basis['operation']['agent']['original_attempt_id'] == 'parent-google'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['google', 'gigachat'])
async def test_retained_model_cannot_request_search_or_fetch(tmp_path, monkeypatch, provider):
    adapter, _service, _job, story, context, original, cohort, reader, fetches, _judges = await owned(tmp_path)
    try:
        with pytest.raises(ResearchUnavailable, match='tool'):
            await extract(adapter, story, context, original, cohort, provider, monkeypatch, tool_call=True)
        assert fetches == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_source_cohort_and_original_read_survive_new_job_lease(tmp_path):
    adapter, service, job, story, context, original, cohort, reader, _, _ = await owned(tmp_path)
    try:
        with service.store.tx() as db:
            db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        renewed = {**story, '_research_job_attempt': job['attempts'] + 1}
        assert retained_source_cohort(adapter, original, story=renewed, context=context, guard=lambda: None) == cohort
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['version_hash', 'chunk_text', 'foreign_run', 'uncompleted_read', 'private_source', 'origin_binding', 'origin_prompt', 'stop', 'owner_B'])
async def test_owned_loader_rejects_corrupt_foreign_stale_or_unread_sources(tmp_path, corruption):
    adapter, service, _job, story, context, original, _cohort, reader, _, _ = await owned(tmp_path)
    try:
        with service.store.tx() as db:
            if corruption == 'version_hash':
                db.execute("UPDATE source_versions SET normalized_text='different bytes'")
            elif corruption == 'chunk_text':
                db.execute("UPDATE source_chunks SET chunk_text='different chunk'")
            elif corruption == 'foreign_run':
                db.execute('DELETE FROM research_run_sources')
            elif corruption == 'private_source':
                db.execute("UPDATE source_versions SET access_scope='story'")
            elif corruption in {'origin_binding', 'origin_prompt'}:
                if corruption == 'origin_binding':
                    original['binding']['attempt_id'] = 'different-journal'
                else:
                    original['frozen_prompt'] = 'different frozen source prompt'
                db.execute('UPDATE research_provider_attempts SET receipt_json=?', (json.dumps(original),))
            elif corruption == 'uncompleted_read':
                original['tool_receipts'] = []
                db.execute('UPDATE research_provider_attempts SET receipt_json=?', (json.dumps(original),))
            else:
                row = service._story_row(db, story['id'])
                research = json.loads(row['research_json'])
                if corruption == 'stop':
                    research['research_cancelled'] = True
                else:
                    research['identity_generation'] = 1
                    research['visual_identity']['candidate_id'] = 'selected-B'
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
        if corruption == 'uncompleted_read':
            assert retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None) is None
        else:
            with pytest.raises(ValueError, match='retained_source_'):
                retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None)
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['candidate', 'cohort_hash', 'origin', 'chunk_hash', 'phase', 'selected_object', 'binding_id', 'missing_marker', 'prompt_hash'])
async def test_confirmation_requires_actual_closed_extraction_membership(tmp_path, monkeypatch, corruption):
    adapter, service, job, story, context, original, cohort, reader, fetches, judges = await owned(tmp_path)
    try:
        result, route, _, _ = await extract(adapter, story, context, original, cohort, 'gigachat', monkeypatch)
        agent, candidate = metadata(route, cohort), copy.deepcopy(CANDIDATE)
        receipt = result['receipt']
        if corruption == 'candidate':
            candidate['text'] += ' invented sibling'
        elif corruption == 'cohort_hash':
            agent['retained_source_cohort_sha256'] = 'forged'
        elif corruption == 'missing_marker':
            del agent['retained_source_cohort_sha256']
        elif corruption in {'binding_id', 'prompt_hash'}:
            if corruption == 'binding_id':
                receipt['binding']['attempt_id'] = 'different-journal'
            else:
                receipt['frozen_prompt'] += ' amended after send'
            # Deliberately retain the actual extraction row, not the forged binding ID.
            adapter._checkpoint_receipt({'attempt_id': 'new-extraction'}, receipt)
        else:
            packet = receipt['retained_source_packet']
            if corruption == 'origin':
                packet['original_attempt_id'] = 'foreign-origin'
            elif corruption == 'chunk_hash':
                packet['sources'][0]['chunks'][0]['text_sha256'] = hashlib.sha256(b'forged').hexdigest()
            elif corruption == 'selected_object':
                packet['ownership']['candidate_id'] = 'other-house'
            else:
                receipt['phase'] = 'unknown'
            packet['source_cohort_sha256'] = _source_packet_sha({k: v for k, v in packet.items() if k != 'source_cohort_sha256'})
            agent['retained_source_cohort_sha256'] = receipt['source_cohort_sha256'] = packet['source_cohort_sha256']
            adapter._checkpoint_receipt(receipt['binding'], receipt)
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert outcome == {'status': 'unverified', 'reason': 'retained_source_binding_invalid'}
        assert fetches == [] and judges == [] and service.story(story['id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_retained_closed_confirmation_recovery_preserves_original_assessment(tmp_path, monkeypatch):
    adapter, service, job, story, context, original, cohort, reader, fetches, judges = await owned(tmp_path)
    try:
        _, route, _, _ = await extract(adapter, story, context, original, cohort, 'gigachat', monkeypatch)
        verify = service.providers.gemini.verify_agent_evidence
        async def interrupted(*args, **kwargs):
            await verify(*args, **kwargs)
            raise RuntimeError('Interrupted after original closed assessment')
        service.providers.gemini.verify_agent_evidence = interrupted
        agent = metadata(route, cohort)
        with pytest.raises(RuntimeError, match='Interrupted after'):
            await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, agent)
        assert service.story(story['id'])['facts'] == []
        async def no_model(*_args, **_kwargs):
            raise AssertionError('Original closed assessment must not be sent again')
        service.providers.gemini.verify_agent_evidence = no_model
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, agent))['status'] == 'supported'
        assert len(judges) == 1 and fetches == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('read_status', ['partial_text_limit', 'partial_body_limit'])
async def test_owned_partial_reader_snapshot_preserves_limit_status_and_hash(tmp_path, read_status):
    adapter, _service, _job, story, context, original, cohort, reader, fetches, _judges = await owned(
        tmp_path, read_status=read_status)
    try:
        source = cohort['sources'][0]
        assert source['read_status'] == read_status
        assert source['content_sha256'] == hashlib.sha256(source['normalized_text'].encode()).hexdigest()
        assert cohort['source_cohort_sha256'] == _source_packet_sha(
            {key: value for key, value in cohort.items() if key != 'source_cohort_sha256'})
        assert retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None) == cohort
        assert fetches == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_new_extraction_overrides_old_count_goal_without_rewriting_saved_context(tmp_path, monkeypatch):
    adapter, _service, _job, story, context, original, cohort, reader, _, _ = await owned(tmp_path)
    try:
        context['editorial_goal'] = 'Find at least six facts; stop once three are accepted.'
        result, _, _, _ = await extract(adapter, story, context, original, cohort, 'google', monkeypatch)
        prompt = result['receipt']['frozen_prompt']
        policy, serialized = prompt.split('\nSOURCE_PACKET_UNTRUSTED=', 1)
        assert 'numeric fact targets' in policy and 'do not determine completeness or stopping' in policy
        assert json.loads(serialized)['context'] == context
        assert result['receipt']['retained_source_packet'] == cohort
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('observed_final', [URL + '/', URL.replace('archive.example', 'ARCHIVE.example'),
    URL + '?another=object', URL + '#another-object', URL + '/neighbor', URL.replace('https:', 'HTTPS:')])
async def test_owned_redirect_slash_preserved_without_host_query_or_object_rebinding(tmp_path, observed_final):
    adapter, service, _job, story, context, original, _cohort, reader, fetches, _judges = await owned(tmp_path)
    try:
        original['sources'][0]['final_url'] = observed_final
        # Model tool used the observed redirect URL, not an invented canonical URL.
        original['tool_receipts'][0]['arguments']['url'] = observed_final
        with service.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                (json.dumps(original), 'parent-google'))
        if observed_final != URL + '/':
            with pytest.raises(ValueError, match='retained_source_version_invalid'):
                retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None)
        else:
            cohort = retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None)
            source = cohort['sources'][0]
            assert source['requested_url'] == URL and source['final_url'] == observed_final
            assert source['version_requested_url'] == URL and source['version_final_url'] == URL
            assert source['content_sha256'] == original['sources'][0]['content_sha256']
            assert retained_source_cohort(adapter, original, story=story, context=context, guard=lambda: None) == cohort
        assert fetches == []
    finally:
        await reader.search_http.aclose()
