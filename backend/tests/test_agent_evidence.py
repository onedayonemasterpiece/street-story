"""Real frozen-reader/ledger boundaries with controlled semantic responses."""
import json

import pytest

from street_story.agent_evidence import AgentEvidenceMixin, selected_object_context
from street_story.service import ConflictError
from test_headless_facts import URL, fixture

CANDIDATE = {'text': 'The gate opened as a museum in 2005.', 'url': URL,
    'quote': 'A museum has welcomed visitors since 2005.',
    'subject_scope': 'selected_building', 'source_date': None}
AGENT = {'route_id': 'controlled-google', 'model_id': 'agent-model',
         'provider_id': 'google', 'attempt_id': 'original-agent-attempt'}


def test_judge_receives_selected_building_kind_and_position_not_neighbor_or_camera():
    identity = {'candidate_id': 'selected-house', 'physical_identity_accepted': True,
        'search_anchor': {'street': 'Same street', 'house_number': '3'},
        'camera': {'latitude': 50.0, 'longitude': 10.0},
        'candidates': [
            {'candidate_id': 'neighbor-monument', 'type': 'monument',
                'map_object': {'tags': {'historic': 'monument'}},
                'map_coordinates': {'latitude': 51.0, 'longitude': 11.0}},
            {'candidate_id': 'selected-house', 'type': 'building',
                'map_object': {'tags': {'building': 'yes'}, 'provenance': 'osm.tags'},
                'map_coordinates': {'latitude': 52.0, 'longitude': 12.0}}]}
    context = selected_object_context(identity)
    observed = context['selected_map_observations']
    assert observed['candidate_id'] == 'selected-house'
    assert observed['object_type'] == 'building'
    assert observed['map_object']['tags'] == {'building': 'yes'}
    assert observed['map_coordinates'] == {'latitude': 52.0, 'longitude': 12.0}
    assert 'camera' not in context and 'neighbor-monument' not in json.dumps(context)


def test_judge_does_not_borrow_observations_when_selected_candidate_is_absent():
    context = selected_object_context({'candidate_id': 'selected-house',
        'physical_identity_accepted': True, 'candidates': [{'candidate_id': 'other', 'type': 'building'}]})
    assert 'selected_map_observations' not in context
    assert context['candidate_id'] == 'selected-house'


async def setup(tmp_path, verdict='supported', after=None):
    service, job, _, reader, fetches = await fixture(tmp_path)
    service.__class__ = type('ConfirmedService', (AgentEvidenceMixin, type(service)), {})
    calls = []
    async def verify(payload, *, checkpoint, guard):
        calls.append(payload)
        guard()
        chunk = payload['source']['chunks'][0]
        assert CANDIDATE['quote'] not in chunk['text']
        result = {'verdict': verdict, 'text': CANDIDATE['text'], 'claim_key': 'museum-opening',
            'subject_scope': 'selected_building', 'confirmed_fragment': CANDIDATE['quote'],
            'source_date': None, 'chunk_ids': [chunk['chunk_id']], 'reason': 'Controlled model assessment.',
            'existing_fact_id': '', 'conflicts_with_existing': []}
        receipt = {'attempt_id': 'verification-original', 'phase': 'completed',
            'provider_id': 'google', 'model_id': 'verifier-model', 'result': result}
        checkpoint(receipt)
        if after:
            after(service, job)
        return {'result': result, 'receipt': receipt}
    service.providers.gemini.verify_agent_evidence = verify
    return service, job, reader, fetches, calls


@pytest.mark.asyncio
async def test_model_confirmation_accepts_paraphrase_and_freezes_original_source(tmp_path):
    service, job, reader, fetches, calls = await setup(tmp_path)
    try:
        first = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert first['status'] == 'supported'
        fact = service.story(job['story_id'])['facts'][0]
        assert fact['eligibility'] == 'eligible'
        assert fact['sources'][0]['confirmed_fragment'] == CANDIDATE['quote']
        assert fact['sources'][0]['subject_scope'] == 'selected_building'
        with service.store.connection() as db:
            own = db.execute('SELECT e.*,v.normalized_text FROM fact_evidence_spans e JOIN source_versions v ON v.source_version_id=e.source_version_id').fetchone()
            assert own['span_text'] in own['normalized_text']
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            assert basis['operation']['confirmation_model'] == 'verifier-model'
            assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'eligible'
        second = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert second == first and len(calls) == 1 and len(fetches) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['insufficient', 'contradicted', 'possible_conflict'])
async def test_nonpositive_model_result_does_not_admit_candidate(tmp_path, verdict):
    service, job, reader, _, calls = await setup(tmp_path, verdict)
    try:
        result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert result['status'] == verdict and len(calls) == 1
        assert service.story(job['story_id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_late_verification_after_owner_B_does_not_enter_ledger(tmp_path):
    def change(service, job):
        with service.store.tx() as db:
            row = service._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            research['identity_generation'] = 1
            research['visual_identity']['candidate_id'] = 'selected-B'
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
    service, job, reader, _, _ = await setup(tmp_path, after=change)
    try:
        with pytest.raises(ConflictError, match='Owner selection'):
            await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert service.story(job['story_id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unknown_confirmation_is_not_resent_on_recovery(tmp_path):
    service, job, reader, _, _ = await setup(tmp_path)
    calls = []
    async def unknown(payload, *, checkpoint, guard):
        calls.append(payload)
        checkpoint({'attempt_id': 'uncertain-request', 'phase': 'unknown',
            'provider_id': 'google', 'model_id': 'verifier-model', 'provider_send_state': 'possibly_sent'})
        return {'status': 'unverified'}
    service.providers.gemini.verify_agent_evidence = unknown
    try:
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT))['status'] == 'unverified'
        assert (await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT))['reason'] == 'original_confirmation_pending'
        assert len(calls) == 1 and service.story(job['story_id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_closed_confirmation_recovered_without_another_inference(tmp_path):
    def interrupted(_service, _job):
        raise RuntimeError('worker interrupted after closed response')
    service, job, reader, _, calls = await setup(tmp_path, after=interrupted)
    try:
        with pytest.raises(RuntimeError, match='worker interrupted'):
            await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert service.story(job['story_id'])['facts'] == []
        async def disappeared(*_args, **_kwargs):
            raise AssertionError('Recovery must use the retained source, without network reading')
        reader._fetch_page_documents = disappeared
        result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert result['status'] == 'supported' and len(calls) == 1
        assert service.story(job['story_id'])['facts'][0]['eligibility'] == 'eligible'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_supported_semantic_conflict_blocks_both_facts_in_existing_journal(tmp_path):
    service, job, reader, _, calls = await setup(tmp_path)
    try:
        first = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        async def conflict(payload, *, checkpoint, guard):
            guard()
            result = {'verdict': 'supported', 'text': 'The gate museum opened in 2006.',
                'claim_key': 'museum-opening-other', 'subject_scope': 'selected_building',
                'confirmed_fragment': 'Museum opening was in 2006.', 'source_date': None,
                'chunk_ids': [payload['source']['chunks'][0]['chunk_id']],
                'reason': 'The two opening years conflict.', 'existing_fact_id': '',
                'conflicts_with_existing': [first['fact_id']]}
            receipt = {'attempt_id': 'conflicting-assessment', 'phase': 'completed',
                'provider_id': 'google', 'model_id': 'verifier-model', 'result': result}
            checkpoint(receipt)
            return {'result': result, 'receipt': receipt}
        service.providers.gemini.verify_agent_evidence = conflict
        second = await service.confirm_agent_fact(job, 'headless-run',
            {**CANDIDATE, 'text': 'The museum opened in 2006.'}, AGENT)
        assert second['status'] == 'possible_conflict'
        assert all(fact['eligibility'] != 'eligible' for fact in service.story(job['story_id'])['facts'])
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM fact_conflicts').fetchone()[0] == 1
        assert len(calls) == 1
    finally:
        await reader.search_http.aclose()
