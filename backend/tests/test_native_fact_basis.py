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
async def test_ledger_and_confirmation_outcome_rollback_together_then_recover_without_inference(tmp_path, monkeypatch):
    import street_story.agent_evidence as module
    service, job, reader, fetches, agent, receipt, _ = await setup(tmp_path)
    original = module._write_confirmation_checkpoint
    def fail_at_outcome(db, job_id, stage, value, now):
        if value.get('outcome', {}).get('fact_id'):
            raise RuntimeError('controlled post-ledger checkpoint failure')
        return original(db, job_id, stage, value, now)
    try:
        candidate = source_candidates(receipt['raw_result'])['facts'][0]
        monkeypatch.setattr(module, '_write_confirmation_checkpoint', fail_at_outcome)
        with pytest.raises(RuntimeError, match='post-ledger checkpoint failure'):
            await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM fact_assertions').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM poi_research_facts').fetchone()[0] == 0
        monkeypatch.setattr(module, '_write_confirmation_checkpoint', original)
        saved = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert saved['status'] == 'supported'
        with service.store.connection() as db:
            before = db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0]
        assert await service.confirm_agent_fact(job, 'headless-run', candidate, agent) == saved
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == before
        assert len(fetches) == 1 and len(service.story(job['story_id'])['facts']) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'missing_call', 'child_thread', 'wrong_turn', 'wrong_model', 'missing_context', 'unread_chunk', 'late_read', 'missing_declaration', 'changed_run', 'stop'])
async def test_incremental_native_assessment_requires_exact_root_delivery_and_owned_source(tmp_path, damage):
    from street_story.fact_agent_providers import native_tools
    service, job, reader, fetches, agent, receipt, save = await setup(tmp_path)
    fact = copy.deepcopy(receipt['raw_result']['facts'][0])
    receipt.update(phase='submitted', provider_send_state='possibly_sent', native_terminal_status=None,
        raw_result={}, provided_tools=native_tools(), frozen_prompt='Component instructions\nINPUT={"run_id":"headless-run"}')
    receipt['native_threads']['original-luna'].pop('execution_contexts')
    receipt['native_tool_calls']['original-read']['completed_at'] = 1
    call = {'phase': 'submitted', 'submitted_at': 2, 'input': {'thread_id': 'original-luna', 'turn_id': 'original-turn',
        'name': 'save_facts', 'arguments': {'facts': [fact]},
        'execution_contexts': [{'model': 'gpt-6-luna', 'effort': 'medium', 'turn_id': 'original-turn'}]}}
    receipt['native_tool_calls']['save-current'] = call
    candidate = source_candidates({'facts': [fact]})['facts'][0]
    candidate['native_checkpoint_call_id'] = 'save-current'
    if damage == 'missing_call':
        candidate['native_checkpoint_call_id'] = 'foreign'
    elif damage == 'child_thread':
        call['input']['thread_id'] = 'child-sol'
    elif damage == 'wrong_turn':
        call['input']['turn_id'] = 'old-turn'
    elif damage == 'wrong_model':
        call['input']['execution_contexts'][0]['model'] = 'gpt-6.1-sol'
    elif damage == 'missing_context':
        call['input']['execution_contexts'] = []
    elif damage == 'unread_chunk':
        receipt['native_tool_calls']['original-read']['phase'] = 'submitted'
    elif damage == 'late_read':
        receipt['native_tool_calls']['original-read']['completed_at'] = 3
    elif damage == 'missing_declaration':
        receipt['provided_tools'] = []
    elif damage == 'changed_run':
        receipt['frozen_prompt'] = 'INPUT={"run_id":"other-run"}'
    save()
    if damage == 'stop':
        with service.store.tx() as db:
            db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job['id'],))
    try:
        outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        if damage:
            assert outcome['status'] in {'unverified', 'stale'}
            assert not service.story(job['story_id'])['facts']
        else:
            assert outcome['status'] == 'supported'
            assert receipt['phase'] == 'submitted' and receipt['provider_send_state'] == 'possibly_sent'
            assert outcome['confirmation']['original_native_checkpoint_call_id'] == 'save-current'
            assert outcome['native_save_receipt']['state'] == 'completed'
            assert outcome['native_save_receipt']['call_id'] == 'save-current'
            assert service.story(job['story_id'])['facts'][0]['eligibility'] == 'eligible'
            # Overall UNKNOWN does not undo a completed own assessment or turn
            # it into final research sufficiency. Exact local replay stays one fact.
            receipt['phase'] = 'unknown'
            save()
            assert await service.confirm_agent_fact(job, 'headless-run', candidate, agent) == outcome
            assert len(service.story(job['story_id'])['facts']) == 1
        assert len(fetches) == 1
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
@pytest.mark.parametrize('damage', [None, 'wrong_url', 'wrong_hash', 'late_read', 'changed_chunk',
    'wrong_photo', 'foreign_read'])
async def test_reference_feedback_requires_owned_intact_prior_read_and_excludes_other_source(tmp_path, damage):
    from street_story.fact_agent_providers import native_tools
    from street_story.native_fact_contract import owned_assessment, NativeFactSourceReferenceInvalid
    service, job, reader, _, agent, receipt, save = await setup(tmp_path, two_sources=True)
    try:
        fact = copy.deepcopy(receipt['raw_result']['facts'][0])
        first, second = fact['sources']
        exact = first['assessment']['chunk_ids'][0]
        foreign = second['assessment']['chunk_ids'][0]
        first['assessment']['chunk_ids'] = [exact[:-1], foreign]
        first['assessment']['checks']['relation_basis']['chunk_ids'] = [exact[:-1]]
        original_fact = copy.deepcopy(fact)
        receipt.update(phase='submitted', provider_send_state='possibly_sent', native_terminal_status=None,
            raw_result={}, provided_tools=native_tools(), frozen_prompt='Instructions\nINPUT={"run_id":"headless-run"}')
        read = receipt['native_tool_calls']['original-read']
        read['completed_at'] = 1
        receipt['native_tool_calls']['save-bad-reference'] = {'phase': 'submitted', 'submitted_at': 2,
            'input': {'thread_id': 'original-luna', 'turn_id': 'original-turn', 'name': 'save_facts',
                'arguments': {'facts': [fact]}, 'execution_contexts': [
                    {'model': 'gpt-6-luna', 'turn_id': 'original-turn'}]}}
        if damage == 'wrong_url':
            first['url'] = 'https://unread.example/other'
        elif damage == 'wrong_hash':
            first['content_sha256'] = '0' * 64
        elif damage == 'late_read':
            read['completed_at'] = 3
        elif damage == 'changed_chunk':
            read['result']['documents'][0]['normalized_text'] = 'Changed source bytes.'
        elif damage == 'wrong_photo':
            receipt['binding']['photo_sha256'] = 'foreign-photo'
        elif damage == 'foreign_read':
            read['input']['name'] = 'unrecognized-tool'
        save()
        candidate = source_candidates({'facts': [fact]})['facts'][0]
        candidate['native_checkpoint_call_id'] = 'save-bad-reference'
        story = HeadlessFacts(service)._snapshot(job, 'headless-run')[0]
        with service.store.connection() as db:
            with pytest.raises(ValueError) as error:
                owned_assessment(db, candidate, agent, story=story, run_id='headless-run')
        if damage:
            assert not isinstance(error.value, NativeFactSourceReferenceInvalid)
            outcome = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
            assert outcome['status'] == 'unverified' and 'reference_diagnostics' not in outcome
        else:
            assert isinstance(error.value, NativeFactSourceReferenceInvalid)
            diagnostic = error.value.reference_diagnostics
            assert diagnostic['source_version_id'] == first['source_version_id']
            assert diagnostic['content_sha256'] == first['content_sha256']
            assert exact in diagnostic['received_chunk_ids'] and foreign not in diagnostic['received_chunk_ids']
            assert diagnostic['invalid_references'] == [
                {'path': ['assessment', 'chunk_ids', 0], 'reference': exact[:-1]},
                {'path': ['assessment', 'chunk_ids', 1], 'reference': foreign},
                {'path': ['assessment', 'checks', 'relation_basis', 'chunk_ids', 0], 'reference': exact[:-1]}]
            assert fact == original_fact
        assert not service.story(job['story_id'])['facts']
    finally:
        await reader.search_http.aclose()


def test_reference_feedback_is_bounded_and_never_echoes_arbitrary_input():
    from street_story.native_fact_contract import NativeFactSourceReferenceInvalid
    assessment = {'chunk_ids': ['private arbitrary text', 'chunk_' + 'f' * 65]
            + ['chunk_' + 'a' * index for index in range(1, 32)],
        'checks': {'relation_basis': {'chunk_ids': ['chunk_123']}}}
    received = {'chunk_' + str(index).zfill(24) for index in range(32)}
    error = NativeFactSourceReferenceInvalid(assessment, version='srcv-controlled',
        content_sha256='0' * 64, received=received)
    diagnostic = error.reference_diagnostics
    assert str(error) == 'native_fact_source_not_owned_read'
    assert len(diagnostic['received_chunk_ids']) == 16 and diagnostic['received_chunk_ids_truncated'] is True
    assert len(diagnostic['invalid_references']) == 16 and diagnostic['invalid_references_truncated'] is True
    assert diagnostic['invalid_references'][:2] == [
        {'path': ['assessment', 'chunk_ids', 0], 'reference_redacted': True},
        {'path': ['assessment', 'chunk_ids', 1], 'reference_redacted': True}]
    assert 'private arbitrary text' not in json.dumps(diagnostic)
    assert diagnostic['invalid_references'][2]['reference'] == 'chunk_a'


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
@pytest.mark.parametrize('damage', [None, 'changed_chunk', 'missing_chunk', 'unmarked', 'navigation_only_chunk'])
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
    elif damage == 'navigation_only_chunk':
        document['chunk_navigation'] = {'chunks': document['chunks']}
        document['chunks'] = []
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
