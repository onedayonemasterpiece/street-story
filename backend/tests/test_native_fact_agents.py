"""Native tool receipts become immutable own reads through existing runtime paths."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.fact_agent_providers import AgentRoute, run_tool_agent
from street_story.opencode_research import ResearchLimits, ResearchUnavailable
from street_story.shared_devcoveer_research import (
    AGENT_GUARD_SOURCE, SharedDevCoveerResearch, guard_profile, scoped_research_config,
)
from test_headless_facts import fixture
from test_opencode_research import Harness
from test_shared_devcoveer_research import Backend


@pytest.mark.asyncio
@pytest.mark.parametrize('native_content', [False, True])
async def test_one_turn_adapter_saves_before_final_then_reads_and_returns_same_canonical_outcome(tmp_path, native_content):
    from test_native_fact_basis import setup
    from street_story.native_fact_research import CONTRACT
    from test_headless_facts import URL
    service, job, reader, fetches, agent, original, _ = await setup(tmp_path)
    fact = copy.deepcopy(original['raw_result']['facts'][0])
    document = original['native_tool_calls']['original-read']['result']['documents'][0]
    source_reads, saves = [], []
    async def frozen_read(urls, context):
        assert urls == [URL] and context['research_run_id'] == 'headless-run'
        source_reads.append(URL)
        return {URL: copy.deepcopy(document)}
    service.providers.gemini._fetch_page_documents = frozen_read
    binding = {**original['binding'], 'attempt_id': agent['attempt_id']}
    def checkpoint(_binding, receipt):
        with service.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                (json.dumps(receipt), binding['attempt_id']))
    class Native:
        async def run(self, **kwargs):
            receipt = kwargs['receipt']
            receipt.update(native_contract=CONTRACT, phase='submitted', provider_send_state='possibly_sent',
                thread_id='original-luna', turn_id='original-turn', native_threads={
                    'original-luna': {'model_id': 'gpt-6-luna', 'turn_id': 'original-turn'}}, native_tool_calls={})
            async def receive():
                if not native_content:
                    return await kwargs['callback']('webfetch', {'url': URL})
                from street_story.native_received_sources import CONTRACT as SOURCE_CONTRACT
                proof = {'contract': SOURCE_CONTRACT, 'url': URL, 'source_ref': 'turn0view0',
                    'thread_id': 'original-luna', 'turn_id': 'original-turn', 'call_id': 'native-open',
                    'result_item_id': 'native-result', 'completed_at': 0.5,
                    'content': document['normalized_text'], 'title': 'Own native archive',
                    'content_sha256': hashlib.sha256(document['normalized_text'].encode()).hexdigest()}
                return await kwargs['callback']('retain_native_source', {'url': URL, 'source_ref': 'turn0view0'},
                    checkpoint_owner={'call_id': 'retain', 'thread_id': 'original-luna',
                        'turn_id': 'original-turn', 'native_source': proof})
            read = await receive()
            kind = 'retain_native_source' if native_content else 'webfetch'
            if native_content:
                owned = read['documents'][0]
                source = fact['sources'][0]
                source.update(source_version_id=owned['source_version_id'], content_sha256=owned['content_sha256'])
                ids = [chunk['chunk_id'] for chunk in owned['chunks']]
                source['assessment']['chunk_ids'] = ids
                source['assessment']['checks']['relation_basis']['chunk_ids'] = ids
                assert owned['native_source_provenance']['call_id'] == 'native-open'
                assert owned['read_status'] == 'native_received_partial'
            receipt['native_tool_calls']['read-one'] = {'phase': 'completed',
                'input': {'name': kind}, 'result': read, 'completed_at': 1}
            packet = {'facts': [fact]}
            # Actual102 failure: the nested assessment had a date while the
            # fact omitted its own required source_date. No server fill-in.
            from street_story.native_research_tools import ResearchToolArgumentsInvalid
            invalid = copy.deepcopy(packet)
            del invalid['facts'][0]['source_date']
            before_facts = copy.deepcopy(service.story(job['story_id'])['facts'])
            with pytest.raises(ResearchToolArgumentsInvalid) as error:
                await kwargs['callback']('save_facts', invalid, checkpoint_owner={
                    'call_id': 'invalid-save', 'thread_id': 'original-luna', 'turn_id': 'original-turn'})
            assert error.value.public_result['validation_issues'] == [
                {'path': ['facts', 0], 'rule': 'required', 'missing_fields': ['source_date']}]
            assert error.value.public_result['saved'] is False
            assert 'source_date' not in invalid['facts'][0]
            assert service.story(job['story_id'])['facts'] == before_facts
            # Actual104 retained its own source but dropped the final character
            # of a chunk ID in both proof fields. Rejection must be actionable
            # in this same turn without host repair or another fetch.
            bad_packet = copy.deepcopy(packet)
            bad_assessment = bad_packet['facts'][0]['sources'][0]['assessment']
            exact_id = bad_assessment['chunk_ids'][0]
            bad_id = exact_id[:-1]
            bad_assessment['chunk_ids'] = [bad_id]
            bad_assessment['checks']['relation_basis']['chunk_ids'] = [bad_id]
            original_bad_packet = copy.deepcopy(bad_packet)
            receipt['native_tool_calls']['save-bad-reference'] = {'phase': 'submitted', 'submitted_at': 2,
                'input': {'thread_id': 'original-luna', 'turn_id': 'original-turn', 'name': 'save_facts',
                    'arguments': bad_packet, 'execution_contexts': [
                        {'model': 'gpt-6-luna', 'effort': 'medium', 'turn_id': 'original-turn'}]}}
            checkpoint(binding, receipt)
            rejected = await kwargs['callback']('save_facts', bad_packet, checkpoint_owner={
                'call_id': 'save-bad-reference', 'thread_id': 'original-luna', 'turn_id': 'original-turn'})
            assert rejected['saved_fact_ids'] == []
            assert rejected['outcomes'][0]['status'] == 'unverified'
            diagnostic = rejected['outcomes'][0]['reference_diagnostics']
            assert diagnostic['source_version_id'] == fact['sources'][0]['source_version_id']
            assert diagnostic['received_chunk_ids'] == sorted(chunk['chunk_id'] for chunk in read['documents'][0]['chunks'])
            assert diagnostic['invalid_references'] == [
                {'path': ['assessment', 'chunk_ids', 0], 'reference': bad_id},
                {'path': ['assessment', 'checks', 'relation_basis', 'chunk_ids', 0], 'reference': bad_id}]
            assert 'NEW save_facts' in rejected['instruction'] and 'same turn' in rejected['instruction']
            assert bad_packet == original_bad_packet and service.story(job['story_id'])['facts'] == before_facts
            receipt['native_tool_calls']['save-bad-reference'].update(phase='completed', result=rejected)
            # The next distinct model-style call copies its existing exact read
            # IDs; the server has never changed the rejected packet.
            receipt['native_tool_calls']['save-one'] = {'phase': 'submitted', 'submitted_at': 2, 'input': {
                'thread_id': 'original-luna', 'turn_id': 'original-turn', 'name': 'save_facts',
                'arguments': packet, 'execution_contexts': [
                    {'model': 'gpt-6-luna', 'effort': 'medium', 'turn_id': 'original-turn'}]}}
            checkpoint(binding, receipt)
            saved = await kwargs['callback']('save_facts', packet, checkpoint_owner={
                'call_id': 'save-one', 'thread_id': 'original-luna', 'turn_id': 'original-turn'})
            assert saved['saved_fact_ids'] and receipt['phase'] == 'submitted'
            assert service.story(job['story_id'])['facts'][0]['eligibility'] == 'eligible'
            replay = await kwargs['callback']('save_facts', packet, checkpoint_owner={
                'call_id': 'save-one', 'thread_id': 'original-luna', 'turn_id': 'original-turn'})
            assert replay == saved
            saves.append(saved)
            receipt['native_tool_calls']['save-one'].update(phase='completed', result=saved)
            later = await receive()
            receipt['native_tool_calls']['read-two'] = {'phase': 'completed',
                'input': {'name': kind}, 'result': later, 'completed_at': 3}
            receipt['native_threads']['original-luna']['execution_contexts'] = [
                {'model': 'gpt-6-luna', 'effort': 'medium', 'turn_id': 'original-turn'}]
            receipt.update(phase='completed', provider_send_state='response_closed', native_terminal_status='completed')
            return {'receipt': receipt, 'text': json.dumps({'facts': [], 'completion': {
                'research_sufficient': False, 'reason': 'Controlled component only, not research sufficiency.',
                'remaining_gaps': ['Independent history remains'], 'stop_basis': 'unresolved_gap', 'next_action': None, 'strongest_candidate': None}})}
    route = AgentRoute('codex_native', 'gpt-6-luna', 'native', Native())
    adapter = SimpleNamespace(service=service, _checkpoint_receipt=checkpoint)
    try:
        result = await run_tool_agent(adapter, route, {'id': job['story_id']},
            {'run_id': 'headless-run'}, binding=binding, guard=lambda: None)
        assert result['receipt']['raw_result']['facts'] == []
        assert len(result['result']['facts']) == 2
        assert [candidate['native_checkpoint_call_id'] for candidate in result['result']['facts']] == [
            'save-bad-reference', 'save-one']
        with service.store.connection() as db:
            before = db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0]
        original_rejected = await service.confirm_agent_fact(job, 'headless-run', result['result']['facts'][0],
            {**agent, 'route_id': route.route_id})
        assert original_rejected['status'] == 'unverified' and original_rejected.get('fact_id') is None
        outcome = await service.confirm_agent_fact(job, 'headless-run', result['result']['facts'][1],
            {**agent, 'route_id': route.route_id})
        assert outcome['fact_id'] == saves[0]['saved_fact_ids'][0]
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == before
        assert len(service.story(job['story_id'])['facts']) == 1
        assert len(source_reads) == (0 if native_content else 2) and len(fetches) == 1
        assert not result['receipt']['raw_result']['completion']['research_sufficient']
    finally:
        await reader.search_http.aclose()


def test_discovery_union_keeps_all_distinct_leads_and_attempt_provenance():
    from street_story.fact_agents import _distinct_discovery_context
    base = {'title': 'Archive', 'url': 'https://example.org/archive',
        'kind': 'unverified_search_lead', 'discovery_provider': 'native_hosted'}
    items = [{**base, 'original_attempt_id': 'one'}] * 300
    items += [{**base, 'original_attempt_id': 'two'},
        {**base, 'title': 'Different title', 'original_attempt_id': 'three'},
        {**base, 'url': base['url'] + '?revision=2', 'original_attempt_id': 'four'}]
    original = copy.deepcopy(items)
    result = _distinct_discovery_context(items)
    assert len(result) == 3
    assert result[0]['original_attempt_ids'] == ['one', 'two']
    assert [item['title'] for item in result] == ['Archive', 'Different title', 'Archive']
    assert result[2]['url'].endswith('?revision=2')
    assert items == original


def test_native_conflict_context_preserves_both_claims_and_all_source_refs(tmp_path):
    from street_story.fact_agents import native_agent_context
    from street_story.conflict_journal import conflict_journal
    from test_conflict_journal import journal_fixture
    service, _, session, _, _, _ = journal_fixture(tmp_path)
    with service.store.connection() as db:
        row = dict(service._story_row(db, session.resource_id))
    row['_identity_generation'] = 0
    before = conflict_journal(service, session.resource_id)['items']
    context = native_agent_context(service, row, {}, 'Continue actual gaps', 'new-run')
    after = context['conflict_journal']
    assert len(after) == len(before) == 1
    for side in ('left', 'right'):
        assert after[0][side]['text'] == before[0][side]['text']
        assert after[0][side]['recorded_revision_digest'] == before[0][side]['recorded_revision_digest']
        spans = after[0][side]['sources'][0]['supports']
        assert len(spans) == 6
        assert [(s['source_version_id'], s['page_number']) for s in spans] == [(f'v{n}', n) for n in range(6)]
        assert all('text' not in span for span in spans)
    assert {k: v for k, v in after[0].items() if k not in ('left', 'right')} == {
        k: v for k, v in before[0].items() if k not in ('left', 'right')}
    assert conflict_journal(service, session.resource_id)['items'] == before


@pytest.mark.asyncio
async def test_native_model_receives_exact_failed_read_outcome_from_its_run(tmp_path):
    from street_story.research_runs import register_discovered_source
    service, job, _, reader, _ = await fixture(tmp_path)
    url = 'https://example.org/blocked/'
    received, saved = [], []
    async def blocked(urls, context):
        assert urls == [url] and context['research_run_id'] == 'headless-run'
        with service.store.tx() as db:
            register_discovered_source(db, run_id='headless-run', url=url.rstrip('/'), title='Blocked source',
                status='failed', error_code='http_403', now=service.store.now())
        return {}
    service.providers.gemini._fetch_page_documents = blocked
    class Native:
        async def run(self, **kwargs):
            received.append(await kwargs['callback']('webfetch', {'url': url}))
            kwargs['receipt'].update(phase='completed', provider_send_state='response_closed')
            return {'text': '{"facts":[]}', 'receipt': kwargs['receipt']}
    adapter = SimpleNamespace(service=service,
        _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
    try:
        await run_tool_agent(adapter, AgentRoute('codex_native', 'gpt-6-luna', 'native', Native()),
            {}, {'run_id': 'headless-run'}, binding={'attempt_id': 'own'}, guard=lambda: None)
        assert received == [{'documents': [], 'read_attempts': [{'requested_url': url,
            'status': 'failed', 'error_code': 'http_403', 'source_version_id': None}]}]
        assert saved[-1]['completed_tool_transcript'][0]['result'] == received[0]
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_native_discovery_uses_hosted_search_and_never_dispatches_ddg_callback(tmp_path):
    service, job, _, reader, _ = await fixture(tmp_path)
    raw = {'type': 'web_search', 'title': 'Catalog', 'url': 'https://example.org/catalog',
        'supports': [{'kind': 'search_snippet', 'text': 'A stale statement absent from the actual page.'}]}
    async def search(query):
        return SimpleNamespace(grounding_sources=[raw], payload={})
    service.providers.gemini._public_web_search = search
    received, saved = [], []
    class Native:
        async def run(self, **kwargs):
            assert [tool['name'] for tool in kwargs['tools']] == ['webfetch', 'read_research_memory', 'retain_native_source', 'save_facts']
            with pytest.raises(ValueError, match='agent_tool_outside_scope'):
                await kwargs['callback']('websearch', {'query': 'Selected building'})
            kwargs['receipt'].update(phase='completed', provider_send_state='response_closed')
            return {'text': '{"facts":[]}', 'receipt': kwargs['receipt']}
    adapter = SimpleNamespace(service=service,
        _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
    try:
        await run_tool_agent(adapter, AgentRoute('codex_native', 'gpt-6-luna', 'native', Native()),
            {}, {'run_id': 'headless-run'}, binding={'attempt_id': 'own'}, guard=lambda: None)
        assert not received and 'raw_search_discovery' not in saved[-1]
        assert 'completed_tool_transcript' not in saved[-1]
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_native_known_context_explains_own_current_and_stale_reviews_without_foreign_scope(tmp_path):
    from street_story.fact_agents import native_agent_context
    from street_story.headless_facts import HeadlessFacts
    from street_story.native_fact_contract import source_candidates
    from street_story.review_packets import SCHEMA
    from test_native_fact_basis import setup
    service, job, reader, _, agent, receipt, _ = await setup(tmp_path)
    try:
        await service.confirm_agent_fact(job, 'headless-run', source_candidates(receipt['raw_result'])['facts'][0], agent)
        fact = service.story(job['story_id'])['facts'][0]
        with service.store.connection() as db:
            db.executescript(SCHEMA)
        story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
        with service.store.tx() as db:
            db.execute("UPDATE fact_assertions SET review_status='withheld',eligibility='withheld',"
                "eligibility_reason='assessment_withheld' WHERE story_id=?", (story['id'],))
            for ref, revision, photo, generation, state in [
                    ('current', fact['revision_digest'], story['photo_sha256'], 0, 'finished'),
                    ('stale', 'older-revision', story['photo_sha256'], 0, 'finished'),
                    ('foreign-photo', fact['revision_digest'], 'other-photo', 0, 'finished'),
                    ('foreign-generation', fact['revision_digest'], story['photo_sha256'], 5, 'finished'),
                    ('pending', fact['revision_digest'], story['photo_sha256'], 0, 'pending')]:
                packet = {'owner_fence': {'photo': photo}, 'bundle': {fact['fact_id']: revision},
                    'items': [{'id': fact['fact_id'], 'evidence': [{'source_version_id': 'own-version'}]}]}
                decision = {'0': {'fact': 0, 'verdict': 'not_supported', 'reason': 'Claim absent from the actual frozen page'}}
                db.execute('INSERT INTO live_review_packets(packet_ref,story_id,run_id,binding,story_revision,'
                    'identity_generation,payload_json,decisions_json) VALUES(?,?,?,?,?,?,?,?)',
                    (ref, story['id'], 'headless-run', 'scope', 0, generation, json.dumps(packet), json.dumps(decision)))
                db.execute('INSERT INTO live_review_attempts(packet_ref,policy_version,affected_json,state) '
                    'VALUES(?,?,?,?)', (ref, 'controlled', '[]', state))
        context = native_agent_context(service, story, research['visual_identity'], 'Continue real gaps', 'headless-run')
        assert context['known'][0]['eligibility'] == 'withheld'
        assert context['known'][0]['eligibility_reason'] == 'assessment_withheld'
        history = context['source_review_history']
        assert [(r['packet_ref'], r['current_revision']) for r in history] == [('current', True), ('stale', False)]
        assert all(r['reason'] == 'Claim absent from the actual frozen page' for r in history)
        assert all(r['kind'] == 'own_review_history_context_not_evidence' for r in history)
        assert context['known'][0]['sources']
        assert all('text' not in span for source in context['known'][0]['sources'] for span in source['supports'])
        assert all('confirmed_fragment' not in source for source in context['known'][0]['sources'])
        # The product ledger retains the original supporting text unchanged.
        with service.store.connection() as db:
            original_sources = json.loads(db.execute('SELECT sources_json FROM facts WHERE story_id=?', (story['id'],)).fetchone()[0])
        assert any(span.get('text') for source in original_sources for span in source['supports'])
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_native_continuation_receives_own_failed_leads_but_excludes_foreign_generation(tmp_path):
    from street_story.fact_agents import native_agent_context
    from street_story.headless_facts import HeadlessFacts
    service, job, _, reader, _ = await fixture(tmp_path)
    story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
    try:
        with service.store.tx() as db:
            for attempt, generation, url in [('own', 0, 'https://example.org/own'),
                    ('foreign', 5, 'https://example.org/foreign')]:
                receipt = {'provider_id': 'codex_native', 'binding': {'attempt_id': attempt,
                    'photo_sha256': story['photo_sha256'], 'generation': generation},
                    'native_discovery': [{'action': {'type': 'search', 'queries': [attempt+' hosted history']},
                        'results': [{'url': url+'/hosted', 'title': attempt,
                            'snippet': 'Untrusted indexed text must never become evidence.'}]}],
                    'native_tool_calls': {'original-search': {'phase': 'completed',
                        'input': {'name': 'websearch'}, 'result': {'sources': [{'title': attempt, 'url': url}]}}}}
                db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?)', (attempt, attempt, story['id'], 'facts_agent_native', json.dumps(receipt), 1, 1))
        context = native_agent_context(service, story, research['visual_identity'], 'Continue actual source gaps', 'headless-run')
        assert context['prior_search_leads'] == [{'title': 'own', 'url': 'https://example.org/own',
            'original_attempt_id': 'own', 'kind': 'unverified_search_lead'},
            {'title': 'own', 'url': 'https://example.org/own/hosted', 'original_attempt_id': 'own',
             'kind': 'unverified_search_lead', 'discovery_provider': 'native_hosted'}]
        assert context['prior_search_queries'][-1] == {'query': 'own hosted history', 'cursor': None,
            'original_attempt_id': 'own', 'discovery_provider': 'native_hosted'}
        assert context['prior_native_attempt_ids'] == ['own']
    finally:
        await reader.search_http.aclose()


def test_native_agent_mask_is_ordered_and_profile_bound_without_changing_legacy(tmp_path):
    legacy = scoped_research_config('configured-model')
    assert 'permission' not in legacy['agent']['plan']
    agent = scoped_research_config('configured-model', agent_research=True)
    assert list(agent['agent']['plan']['permission'].items()) == [
        ('*', 'deny'), ('websearch', 'allow'), ('webfetch', 'allow')]
    for mode, config in [(False, legacy), (True, agent)]:
        client = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model',
            agent_research=mode, require_guard=False)
        assert client.profile_fingerprint == hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    prior_agent = copy.deepcopy(agent)
    del prior_agent['agent']['plan']['permission']
    assert hashlib.sha256(json.dumps(prior_agent, sort_keys=True).encode()).hexdigest() != client.profile_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize('sorted_json', [False, True])
@pytest.mark.parametrize('later_rule', [None,
    {'permission': 'external_directory', 'pattern': '*', 'action': 'allow'},
    {'permission': '*', 'pattern': '*', 'action': 'allow'},
    {'permission': 'read', 'pattern': '*', 'action': 'allow'},
    {'permission': 'grep', 'pattern': '*.md', 'action': 'deny'},
    {'permission': 'websearch', 'pattern': '*', 'action': 'deny'},
    {'permission': 'r*', 'pattern': '*', 'action': 'allow'},
    {'permission': 'unknown_future_tool', 'pattern': '*', 'action': 'allow'}])
async def test_native_agent_resolved_permissions_require_closed_tail_before_dispatch(tmp_path, later_rule, sorted_json):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, agent_research=True)
    h.config = scoped_research_config(client.model_id, agent_research=True)
    h.config = json.loads(json.dumps(h.config, sort_keys=sorted_json))
    closed_tail = [{'permission': name, 'pattern': '*', 'action': action}
        for name, action in h.config['agent']['plan']['permission'].items()]
    backend.agent_permission = [
        {'permission': '*', 'pattern': '*', 'action': 'allow'},
        {'permission': 'read', 'pattern': '*', 'action': 'allow'}, *closed_tail]
    if later_rule is not None:
        backend.agent_permission.append(later_rule)
    if later_rule is not None and later_rule['permission'] != 'external_directory':
        with pytest.raises(ResearchUnavailable, match='research_agent_tool_permissions_unverified'):
            await client._run('agent_research', 'Controlled tools', {'attempt_id': 'mask-control'}, {'type': 'object'})
    else:
        proof = await client._attest(None, 'agent_research')
        assert proof['resolved_agent_tool_permission_tail'] == closed_tail
    assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.parametrize('context', [None, 0, -1, True, '1048576'])
def test_bounded_native_config_requires_actual_positive_catalog_context(context):
    with pytest.raises(ValueError, match='research_catalog_context_required'):
        scoped_research_config('configured-model', limits=ResearchLimits(max_output_tokens=4096), model_context=context)


def test_native_config_includes_context_with_output_and_preserves_unbounded_default():
    assert 'provider' not in scoped_research_config('configured-model')
    config = scoped_research_config('configured-model', limits=ResearchLimits(max_output_tokens=4096), model_context=1048576)
    assert config['provider']['opencode']['models']['configured-model']['limit'] == {'context': 1048576, 'output': 4096}


@pytest.mark.parametrize('names', ['connector', ['', 'valid'], ['../connector'], [None], {'connector': False}])
def test_native_capsule_mcp_override_accepts_only_observed_valid_name_list(names):
    with pytest.raises(ValueError, match='research_disabled_mcp_names_invalid'):
        scoped_research_config('configured-model', disabled_mcp=names)


def test_native_capsule_disables_only_named_mcp_and_pins_override_in_profile(tmp_path):
    assert 'mcp' not in scoped_research_config('configured-model')
    config = scoped_research_config('configured-model', disabled_mcp=['kalinigrad-base'], agent_research=True)
    assert config['mcp'] == {'kalinigrad-base': {'enabled': False}}
    legacy_original = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').profile_fingerprint
    original = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model', agent_research=True).profile_fingerprint
    (tmp_path / 'opencode.json').write_text(json.dumps(config))
    client = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model', agent_research=True)
    assert client.disabled_mcp == ('kalinigrad-base',)
    assert client.profile_fingerprint != original
    assert SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').profile_fingerprint == legacy_original
    config['mcp']['active-other'] = {'enabled': None}
    (tmp_path / 'opencode.json').write_text(json.dumps(config))
    assert SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').disabled_mcp == ('kalinigrad-base',)


@pytest.mark.asyncio
@pytest.mark.parametrize('context', [200000, 199999])
async def test_native_attestation_checks_actual_catalog_context_before_send(tmp_path, context):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, limits=ResearchLimits(max_output_tokens=4096))
    h.config['provider']['opencode']['models']['mimo-v2.6-flash-free']['limit']['context'] = context
    client.model_context = context
    if context == 200000:
        result = await client.search_articles('Facade history', {'attempt_id': 'control'})
        assert result['receipt']['isolation']['model_context'] == context
        assert len(h.sends) == 1
    else:
        with pytest.raises(ResearchUnavailable, match='research_model_context_unverified'):
            await client.search_articles('Facade history', {'attempt_id': 'control'})
        assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.asyncio
async def test_native_attestation_refuses_output_above_unscoped_catalog_limit(tmp_path):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, limits=ResearchLimits(max_output_tokens=65536))
    with pytest.raises(ResearchUnavailable, match='research_model_output_not_bounded'):
        await client.search_articles('Facade history', {'attempt_id': 'control'})
    assert ('GET', '/config/providers', None) in [call[:3] for call in backend.calls]
    assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,category', [(400, 'validation'), (500, 'unavailable')])
async def test_native_closed_http_error_retains_safe_status_and_binding_without_private_body(tmp_path, status, category):
    class OpenCodeError(RuntimeError):
        def __init__(self):
            super().__init__('private provider body must not enter receipt')
            self.status, self.category = status, category
    class BadBackend:
        async def request(self, *args, **kwargs):
            raise OpenCodeError()
    h = Harness()
    client = SharedDevCoveerResearch(str(tmp_path), backend=BadBackend(), require_guard=False,
        model_id='configured-model', admission=h.admission, checkpoint=h.checkpoint)
    with pytest.raises(ResearchUnavailable) as failed:
        await client._run('facts', 'Control prompt', {'attempt_id': 'original-control'}, {'type': 'object'})
    assert failed.value.receipt['transport_status'] == status
    assert failed.value.receipt['transport_category'] == category
    assert failed.value.receipt['transport_response_closed'] is True
    assert failed.value.receipt['binding']['attempt_id'] == 'original-control'
    assert not failed.value.receipt.get('session_id') and not h.sends
    assert 'private' not in json.dumps(failed.value.receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_end', [False, True])
async def test_actual_native_tool_loop_versions_only_its_completed_read_output(tmp_path, missing_end):
    service, job, _, reader, fetches = await fixture(tmp_path)
    try:
        capsule = tmp_path / 'capsule'
        capsule.mkdir()
        h = Harness()
        limits = ResearchLimits(timeout_seconds=120, max_steps=4, max_output_tokens=4096)
        h.config = scoped_research_config('mimo-v2.6-flash-free', limits=limits, directory=capsule,
            agent_research=True, model_context=200000)
        (capsule / 'opencode.json').write_text(json.dumps(h.config))
        profile = guard_profile(capsule, limits, agent_research=True)
        (capsule / profile[0].split('/')[-1]).write_bytes(AGENT_GUARD_SOURCE.read_bytes())
        url, text = 'https://archive.example/native', 'The building was constructed in 1901. Exact native output.'
        candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
            'url': url, 'quote': 'constructed in 1901', 'source_date': None}
        h.result = {'facts': [candidate, {**candidate, 'url': 'https://archive.example/unread'}]}
        original_messages = h.messages
        def messages():
            result = original_messages()
            if len(result) > 1:
                result[-1]['parts'][:0] = [
                    {'type': 'tool', 'tool': 'websearch', 'callID': 'search-own', 'state': {
                        'status': 'completed', 'input': {'query': 'building history', 'numResults': 5},
                        'output': 'Title: History\nURL: '+url+'\nText: discovery only\n', 'time': {'end': 1000}}},
                    {'type': 'tool', 'tool': 'webfetch', 'callID': 'read-own', 'state': {
                        'status': 'completed', 'input': {'url': url, 'format': 'text'}, 'output': text,
                        'metadata': {'truncated': True}, 'time': {} if missing_end else {'end': 2000}}}]
            return result
        h.messages = messages
        backend = Backend(h, capsule)
        client = SharedDevCoveerResearch(str(capsule), backend=backend, model_id='mimo-v2.6-flash-free',
            limits=limits, agent_research=True, admission=h.admission, checkpoint=h.checkpoint)
        saved = []
        adapter = SimpleNamespace(service=service, _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
        invoke = run_tool_agent(adapter, AgentRoute('opencode', client.model_id, client.endpoint, client, str(capsule)),
            {}, {'run_id': 'headless-run'}, binding={'attempt_id': 'native-control', 'story_id': job['story_id'], 'purpose': 'facts'},
            guard=lambda: None)
        if missing_end:
            with pytest.raises(ResearchUnavailable, match='agent_result_no_valid_candidates'):
                await invoke
            assert saved[-1]['tool_receipts'][1]['completed_at'] is None
        else:
            result = await invoke
            receipt = result['receipt']
            assert receipt['phase'] == 'completed' and receipt['provider_send_state'] == 'response_closed'
            assert receipt['model_attempts'] and receipt['isolation']['tool_boundary_enforced'] is True
            assert receipt['tool_receipts'][1]['arguments'] == {'url': url}
            assert receipt['tool_receipts'][1]['completed_at'] == 2
            assert receipt['native_tool_receipts'][1]['state']['input']['format'] == 'text'
            assert result['result']['facts'] == [candidate]
            assert receipt['rejected_candidates'][0]['reason'] == 'agent_source_not_read'
            assert receipt['sources'][0]['http_status_verified'] is False
        with service.store.connection() as db:
            rows = db.execute("SELECT * FROM source_versions WHERE requested_url=?", (url,)).fetchall()
            assert len(rows) == (0 if missing_end else 1)
            if rows:
                assert rows[0]['normalized_text'] == text
                assert rows[0]['content_sha256'] == hashlib.sha256(text.encode()).hexdigest()
                assert rows[0]['read_status'] == 'native_tool_output_partial' and rows[0]['http_status'] == 0
        assert not fetches and len(h.sends) == 1
    finally:
        await reader.search_http.aclose()
