"""Native researcher proof enters the ordinary frozen ledger without a judge send."""
import copy
import json

import pytest

from street_story.agent_evidence import AgentEvidenceMixin
from street_story.headless_facts import HeadlessFacts
from street_story.native_fact_contract import source_candidates
from street_story.native_fact_research import CONTRACT
from street_story.research_runs import persist_source_version
from test_headless_facts import URL, fixture


async def setup(tmp_path, *, two_sources=False):
    service, job, _, reader, fetches = await fixture(tmp_path)
    service.__class__ = type('NativeConfirmedService', (AgentEvidenceMixin, type(service)), {})
    document = (await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'}))[URL]
    documents = [document]
    if two_sources:
        with service.store.tx() as db:
            version = persist_source_version(db, run_id='headless-run', requested_url=URL,
                final_url=URL, title='Second frozen revision', content_type='text/plain',
                http_status=200, redirect_chain=[], normalized_text='Another documented account of the museum opening in 2005.',
                read_status='complete', access_scope='public', now=service.store.now())
        documents.append({**version, 'requested_url': URL, 'final_url': URL,
            'normalized_text': 'Another documented account of the museum opening in 2005.'})
    snapshot = HeadlessFacts(service)._snapshot(job, 'headless-run')
    story = snapshot[0]
    fact = {'text': 'В 2005 году в выбранных воротах открылся музей.',
        'claim_key': 'museum-opening', 'subject_scope': 'selected_building', 'source_date': None, 'sources': []}
    for doc in documents:
        with service.store.connection() as db:
            doc['chunks'] = [dict(row) for row in db.execute('SELECT chunk_id,chunk_text FROM source_chunks '
                'WHERE source_version_id=? ORDER BY ordinal', (doc['source_version_id'],))]
            chunk = db.execute('SELECT chunk_id FROM source_chunks WHERE source_version_id=? ORDER BY ordinal LIMIT 1',
                (doc['source_version_id'],)).fetchone()[0]
        row = {'verdict': 'supported', **{key: fact[key] for key in ('text', 'claim_key', 'subject_scope', 'source_date')},
            'confirmed_fragment': 'Музей принимает посетителей с 2005 года.', 'chunk_ids': [chunk],
            'reason': 'Controlled semantic assessment of the original source and selected physical subject.',
            'existing_fact_id': '', 'conflicts_with_existing': [], 'checks': {
                'source_physical_subject': 'Selected gate', 'object_relation': 'selected_building',
                'relation_basis': {'fragment': 'История выбранных ворот.', 'chunk_ids': [chunk]},
                'material_kind': 'history', 'temporal_basis': 'historical', 'temporal_preserved': True}}
        fact['sources'].append({'url': URL, 'source_version_id': doc['source_version_id'],
            'content_sha256': doc['content_sha256'], 'assessment': row})
    agent = {'route_id': 'native-controlled-route', 'provider_id': 'codex_native',
        'model_id': 'gpt-6-luna', 'attempt_id': 'native-original-attempt'}
    binding = {'story_id': story['id'], 'photo_sha256': story['photo_sha256'], 'generation': story['_identity_generation'],
        'control_revision': story['_fact_research_control_revision'], 'job_id': job['id']}
    receipt = {'binding': binding, 'provider_id': 'codex_native', 'model_id': 'gpt-6-luna', 'phase': 'completed',
        'provider_send_state': 'response_closed', 'native_contract': CONTRACT, 'native_terminal_status': 'completed',
        'thread_id': 'original-luna', 'turn_id': 'original-turn',
        'native_threads': {'original-luna': {'model_id': 'gpt-6-luna', 'turn_id': 'original-turn',
            'execution_contexts': [{'model': 'gpt-6-luna', 'turn_id': 'original-turn'}]}},
        'raw_result': {'facts': [fact]}, 'native_tool_calls': {
            'original-read': {'phase': 'completed', 'input': {'name': 'webfetch'}, 'result': {'documents': documents}}}}

    def save():
        with service.store.tx() as db:
            db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET receipt_json=excluded.receipt_json',
                (agent['attempt_id'], 'native-controlled', story['id'], 'facts_agent_native', json.dumps(receipt), 1, 1))

    save()
    async def unexpected(*args, **kwargs):
        raise AssertionError('Admission must reuse the original owned source and assessment')
    service.providers.gemini._fetch_page_documents = unexpected
    service.providers.gemini.verify_agent_evidence = unexpected
    return service, job, reader, fetches, agent, receipt, save


@pytest.mark.asyncio
async def test_complete_single_model_basis_is_admitted_without_new_fetch_or_judge(tmp_path):
    service, job, reader, fetches, agent, receipt, _ = await setup(tmp_path)
    try:
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert outcome['status'] == 'supported'
        fact = service.story(job['story_id'])['facts'][0]
        assert fact['eligibility'] == 'eligible' and fact['sources'][0]['verification_level'] == 'one_model'
        assert len(fetches) == 1
        replay = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert replay == outcome and len(service.story(job['story_id'])['facts']) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', [URL+'/', URL+'?different=1', 'https://unread.example/gate-history'])
async def test_owned_native_url_equivalence_preserves_host_query_version_and_chunks(tmp_path, alias):
    from street_story.native_fact_contract import owned_assessment
    service, job, reader, _, agent, receipt, save = await setup(tmp_path)
    try:
        receipt['raw_result']['facts'][0]['sources'][0]['url'] = alias
        save()
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        story = HeadlessFacts(service)._snapshot(job, 'headless-run')[0]
        with service.store.connection() as db:
            if alias == URL+'/':
                document, result = owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
                assert document['requested_url'] == URL
                assert result['receipt']['new_inference_sent'] is False
            else:
                with pytest.raises(ValueError, match='source_not_owned_read'):
                    owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_paged_assessment_requires_actual_received_chunks_across_owned_calls(tmp_path):
    from street_story.native_fact_contract import owned_assessment
    from street_story.native_source_reads import source_page
    service, job, reader, _, agent, receipt, save = await setup(tmp_path)
    try:
        story = HeadlessFacts(service)._snapshot(job, 'headless-run')[0]
        with service.store.tx() as db:
            frozen = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='Long own history', content_type='text/html', http_status=200, redirect_chain=[],
                normalized_text='История выбранных ворот. Музей принимает посетителей с 2005 года.\n' * 400,
                read_status='complete', access_scope='public', now=service.store.now())
            first = source_page(db, run_id='headless-run', url=URL, version=frozen['source_version_id'], byte_budget=1)
            second = source_page(db, run_id='headless-run', url=URL, cursor=first['next_cursor'], byte_budget=1)
        fact = receipt['raw_result']['facts'][0]
        basis = fact['sources'][0]
        basis.update(source_version_id=frozen['source_version_id'], content_sha256=frozen['content_sha256'])
        basis['assessment']['chunk_ids'] = [first['chunks'][0]['chunk_id'], second['chunks'][0]['chunk_id']]
        basis['assessment']['checks']['relation_basis']['chunk_ids'] = [first['chunks'][0]['chunk_id']]
        receipt['native_tool_calls']['original-read']['result']['documents'] = [first]
        save()
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        with service.store.connection() as db, pytest.raises(ValueError, match='source_not_owned_read'):
            owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
        receipt['native_tool_calls']['tail-read'] = {'phase': 'completed', 'input': {'name': 'webfetch'},
            'result': {'documents': [second]}}
        save()
        with service.store.connection() as db:
            owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
        # A pending callback and a modified chunk never become received proof.
        receipt['native_tool_calls']['tail-read']['phase'] = 'submitted'
        save()
        with service.store.connection() as db, pytest.raises(ValueError, match='source_not_owned_read'):
            owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
        receipt['native_tool_calls']['tail-read']['phase'] = 'completed'
        second['chunks'][0]['text'] += ' fabricated suffix'
        save()
        with service.store.connection() as db, pytest.raises(ValueError, match='source_not_owned_read'):
            owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_one_claim_keeps_two_frozen_versions_of_same_url(tmp_path):
    service, job, reader, _, agent, receipt, _ = await setup(tmp_path, two_sources=True)
    try:
        outcomes = [await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
            for candidate in source_candidates(receipt['raw_result'])['facts']]
        facts = service.story(job['story_id'])['facts']
        assert all(row['status'] == 'supported' for row in outcomes) and len(facts) == 1, outcomes
        assert {support['source_version_id'] for source in facts[0]['sources']
            for support in source['supports']} == {
            source['source_version_id'] for source in receipt['raw_result']['facts'][0]['sources']}
        reopened = service.story(job['story_id'])['facts']
        assert reopened[0]['sources'] == facts[0]['sources']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'changed_chunk', 'missing_chunk', 'unmarked'])
async def test_compact_native_read_preserves_complete_frozen_chunks_or_fails_closed(tmp_path, damage):
    from street_story.native_fact_contract import SOURCE_READ_CONTRACT
    service, job, reader, _, agent, receipt, save = await setup(tmp_path)
    document = receipt['native_tool_calls']['original-read']['result']['documents'][0]
    document.pop('normalized_text')
    document['read_contract'] = SOURCE_READ_CONTRACT
    if damage == 'changed_chunk':
        document['chunks'][0]['chunk_text'] = 'A statement from a different page.'
    elif damage == 'missing_chunk':
        document['chunks'] = []
    elif damage == 'unmarked':
        document.pop('read_contract')
    save()
    try:
        outcome = await service.confirm_agent_fact(job, 'headless-run',
            source_candidates(receipt['raw_result'])['facts'][0], agent)
        if damage is None:
            assert outcome['status'] == 'supported'
            assert service.story(job['story_id'])['facts'][0]['eligibility'] == 'eligible'
        else:
            assert outcome == {'status': 'unverified', 'reason': 'native_fact_source_not_owned_read'}
            assert not service.story(job['story_id'])['facts']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unknown', 'wrong_model', 'wrong_photo', 'unread_source',
    'changed_claim', 'foreign_chunk', 'reference_only', 'unpreserved_time', 'unrelated_subject'])
async def test_incomplete_foreign_or_unusable_basis_does_not_enter_ledger(tmp_path, change):
    service, job, reader, _, agent, receipt, save = await setup(tmp_path)
    fact = receipt['raw_result']['facts'][0]
    if change == 'unknown':
        receipt['phase'] = 'unknown'
    elif change == 'wrong_model':
        receipt['native_threads']['original-luna']['model_id'] = 'gpt-6.1-sol'
    elif change == 'wrong_photo':
        receipt['binding']['photo_sha256'] = 'other-photo'
    elif change == 'unread_source':
        receipt['native_tool_calls'] = {}
    elif change == 'changed_claim':
        pass  # Change the caller's claim after preserving the model's receipt.
    elif change == 'foreign_chunk':
        fact['sources'][0]['assessment']['chunk_ids'] = ['foreign-chunk']
    elif change == 'reference_only':
        fact['sources'][0]['assessment']['checks']['material_kind'] = 'reference_only'
    elif change == 'unpreserved_time':
        fact['sources'][0]['assessment']['checks']['temporal_preserved'] = False
    else:
        fact['sources'][0]['assessment']['checks']['object_relation'] = 'unrelated'
    save()
    try:
        candidate = copy.deepcopy(source_candidates(receipt['raw_result'])['facts'][0])
        if change == 'changed_claim':
            candidate['text'] = 'An unassessed different claim'
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert outcome['status'] == 'unverified' and service.story(job['story_id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_independent_complete_assessment_survives_mistaken_outer_grouping(tmp_path):
    service, job, reader, _, agent, receipt, save = await setup(tmp_path)
    try:
        fact = receipt['raw_result']['facts'][0]
        fact.update(text='Unassessed outer summary', claim_key='outer-summary', source_date='2026')
        save()
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        assert candidate['text'] == fact['sources'][0]['assessment']['text']
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert outcome['status'] == 'supported'
        admitted = service.story(job['story_id'])['facts'][0]
        assert admitted['text'] == 'В 2005 году в выбранных воротах открылся музей.'
        assert admitted['text'] != fact['text']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign_poi', [False, True])
async def test_closed_assessment_write_recovery_is_scoped_to_original_poi_and_current_worker(tmp_path, foreign_poi):
    from street_story.research_runs import begin_research_run
    service, old_job, reader, _, agent, receipt, save = await setup(tmp_path)
    receipt['frozen_prompt'] = 'Original\nINPUT=' + json.dumps({'run_id': 'headless-run'})
    save()
    try:
        with service.store.tx() as db:
            sid = old_job['story_id']
            story = service._story_row(db, sid)
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (old_job['id'],))
            jid = service._enqueue_job(db, sid, 'research', 'write-recovery', {
                'identity_generation': 0, 'photo_sha256': story['photo_sha256']})
            db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (jid,))
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
            begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='Write the original closed assessment',
                scope='write-recovery', expected_story_revision=story['revision'], identity_generation=0,
                run_id='recovery-run', now=service.store.now())
            if foreign_poi:
                db.execute("UPDATE research_runs SET poi_key='foreign' WHERE run_id='headless-run'")
        agent = {**agent, 'recovery_origin_run_id': 'headless-run'}
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        outcome = await service.confirm_agent_fact(job, 'recovery-run', candidate, agent)
        assert outcome['status'] == ('unverified' if foreign_poi else 'supported')
        if foreign_poi:
            assert outcome['reason'] == 'native_fact_recovery_origin_unverified'
        else:
            assert outcome['confirmation']['recovery_origin_run_id'] == 'headless-run'
        with service.store.connection() as db:
            original = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (receipt['binding'].get('attempt_id') or agent['attempt_id'],)).fetchone()[0])
            assert original == receipt
    finally:
        await reader.search_http.aclose()
