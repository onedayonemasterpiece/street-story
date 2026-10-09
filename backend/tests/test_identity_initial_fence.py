"""A possibly sent SOURCE+MAP joint1 is observed rather than repeated."""
import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from street_story import identity_discovery
from street_story.gemini import GeminiExecutor, GeminiKeyPool
from street_story.identity_plan_diagnostics import joint_operation_marker, provider_outcome, joint_route_reassignable
from street_story.providers import RetryableProviderError, PermanentProviderError
from test_observed_address_search_context import observed
from test_structured_identity_first_wave import choice, payload
from test_visual_search_continuation import prepared


def valid_plan():
    return payload([choice('address', 'osm:node:1'), choice('address', 'osm:node:3')])


def snapshot(service, story):
    return {**service._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}


def pool(service):
    return GeminiExecutor(GeminiKeyPool(service.store,
        (SecretStr('fixture-a'), SecretStr('fixture-b')), 'fixture-model'))


@pytest.mark.parametrize('phase,status,old_model,new_model,allowed', [
    ('closed_failure', 503, 'preferred', 'reserve', True),
    ('closed_failure', 429, 'preferred', 'reserve', True),
    ('closed_failure', 404, 'preferred', 'reserve', True),
    ('closed_failure', 503, 'preferred', 'preferred', False),
    ('closed_failure', 400, 'preferred', 'reserve', False),
    ('closed_failure', None, 'preferred', 'reserve', False),
    ('unknown', 503, 'preferred', 'reserve', False),
    ('response_closed', 503, 'preferred', 'reserve', False),
])
def test_only_received_availability_error_allows_different_model(phase, status, old_model, new_model, allowed):
    marker = {'phase': phase, 'status_code': status, 'model_id': old_model}
    assert joint_route_reassignable(marker, new_model) is allowed
    assert not joint_route_reassignable({**marker, 'response_sha256': 'semantic-response'}, new_model)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'cancelled'])
async def test_initial_lost_outcome_blocks_executor_keys_and_new_service_native(tmp_path, failure):
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        assert joint_operation_marker(service, snapshot(service, story), stage='initial')['phase'] == 'send_intent'
        raise TimeoutError if failure == 'timeout' else asyncio.CancelledError
    async def forbidden(*args):
        pytest.fail('A lost initial joint cannot authorize a fresh independent inference')
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(RetryableProviderError, match='identity_joint_initial_outcome_unknown'):
        await identity_discovery.suggest(service, snapshot(service, story), '', [])
    assert calls == ['google']
    marker = joint_operation_marker(service, snapshot(service, story), stage='initial')
    assert marker['phase'] == 'unknown' and marker['scope']['photo_sha256'] == story['photo_sha256']
    assert len(marker['binding']['input_sha256']) == len(marker['binding']['schema_sha256']) == 64
    fresh = type(service)(service.settings, providers=service.providers)
    with pytest.raises(RetryableProviderError, match='identity_joint_initial_outcome_unknown'):
        await identity_discovery.suggest(fresh, snapshot(fresh, story), '', [])
    assert calls == ['google']


@pytest.mark.asyncio
async def test_authoritative_unsent_initial_allows_existing_key_assignment(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        if len(calls) == 1:
            error = RetryableProviderError('fixture_local_admission_denied')
            error.receipt = {'provider_send_state': 'not_sent'}
            raise error
        return SimpleNamespace(text=json.dumps(valid_plan()), response_id='received-after-unsent')
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=[])
    result = snapshot(service, story)
    await identity_discovery.suggest(service, result, '', [])
    assert calls == ['google', 'google']
    assert joint_operation_marker(service, result, stage='initial')['phase'] == 'response_closed'


@pytest.mark.asyncio
async def test_closed503_is_provider_failure_and_one_independent_route_not_unknown(tmp_path):
    from google.genai.errors import ServerError
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        raise ServerError(503, {'error': {'code': 503, 'status': 'UNAVAILABLE', 'message': 'Fixture closed response'}})
    async def independent(*args):
        calls.append('independent')
        marker = joint_operation_marker(service, snapshot(service, story), stage='initial')
        assert marker['phase'] == 'closed_failure' and marker['status_code'] == 503
        return {'result': valid_plan()}
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=independent)
    await identity_discovery.suggest(service, snapshot(service, story), '', [])
    assert calls == ['google', 'independent']


@pytest.mark.asyncio
async def test_followup_closed503_stays_known_failure_without_fresh_third_route(tmp_path):
    from google.genai.errors import ServerError
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        if len(calls) == 1:
            return SimpleNamespace(text='{}')
        raise ServerError(503, {'error': {'code': 503, 'status': 'UNAVAILABLE', 'message': 'Fixture closed response'}})
    async def forbidden(*args):
        pytest.fail('A closed joint2 failure cannot authorize a third fresh semantic route')
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(ServerError):
        await identity_discovery.suggest(service, snapshot(service, story), '', [])
    marker = joint_operation_marker(service, snapshot(service, story), stage='followup')
    assert marker['phase'] == 'closed_failure' and marker['status_code'] == 503
    fresh = type(service)(service.settings, providers=service.providers)
    with pytest.raises(PermanentProviderError, match='identity_joint_followup_closed_failure'):
        await identity_discovery.suggest(fresh, snapshot(fresh, story), '', [])
    assert calls == ['google', 'google']


@pytest.mark.asyncio
async def test_existing_addressed_readback_precedes_lost_initial_fence(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    current = snapshot(service, story)
    joint_operation_marker(service, current, stage='initial', phase='send_intent',
        binding={'input_sha256': 'a' * 64, 'schema_sha256': 'b' * 64})
    async def forbidden(*args, **kwargs):
        pytest.fail('Original addressed readback must precede another SOURCE send')
    old = {key: value for key, value in valid_plan().items() if key != 'first_wave_hypotheses'}
    old['article_queries'] = ['Frozen original readback']
    frozen = {'type': 'object', 'properties': {key: {'type': 'array' if isinstance(value, list) else 'string'}
        for key, value in old.items()}, 'required': list(old), 'additionalProperties': False}
    async def readback(*args):
        return {'result': old, 'original_schema_readback': True, 'original_schema': frozen}
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=forbidden)
    service.providers.research = SimpleNamespace(has_identity_search_plan_readback=lambda _: True,
        plan_identity_search=readback)
    await identity_discovery.suggest(service, current, '', [])
    assert current['_identity_article_queries'] == ['Frozen original readback']
    assert joint_operation_marker(service, current, stage='initial')['phase'] == 'send_intent'


def test_error_prose_does_not_invent_closed_or_unsent_transport_evidence():
    error = RetryableProviderError('503 not_sent API unavailable in a prose message')
    assert provider_outcome(error) == ('unknown', None)


@pytest.mark.asyncio
@pytest.mark.parametrize('preference,first', [('configured-fast', 'configured-fast'), ('missing', 'configured-original')])
async def test_only_existing_configured_joint_route_is_stably_preferred(tmp_path, preference, first):
    service, _, story, _ = prepared(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model=preference)
    calls = []
    quotas = {'configured-original': object(), 'configured-fast': object()}
    class Executor:
        def __init__(self, model):
            self.model = model
        async def execute(self, operation, call):
            calls.append(('executor', self.model))
            return await call('fixture', 5)
    async def generate(*args, **kwargs):
        model = kwargs['model']
        assert kwargs['quota'] is quotas[model]
        calls.append(('send', model))
        return SimpleNamespace(text=json.dumps(valid_plan()))
    routes = [(name, object(), quotas[name], Executor(name)) for name in quotas]
    original_order = list(routes)
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=routes)
    await identity_discovery.suggest(service, snapshot(service, story), '', [])
    assert calls == [('executor', first), ('send', first)]
    assert routes == original_order


@pytest.mark.asyncio
async def test_preferred_route_unknown_cannot_send_next_configured_model(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='configured-fast')
    calls = []
    async def generate(*args, **kwargs):
        calls.append(kwargs['model'])
        raise TimeoutError
    async def forbidden(*args):
        pytest.fail('Preferred initial UNKNOWN cannot authorize another model or Native')
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate,
        research_routes=[('configured-original', None, None, pool(service)),
            ('configured-fast', None, None, pool(service))])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(RetryableProviderError, match='identity_joint_initial_outcome_unknown'):
        await identity_discovery.suggest(service, snapshot(service, story), '', [])
    assert calls == ['configured-fast']


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed_first', [False, True])
async def test_exact_frozen_map_id_joins_precede_validation_and_do_not_add_semantic_call(tmp_path, malformed_first):
    from street_story import identity_source_selection
    if not callable(getattr(identity_source_selection, 'resolve_identity_response_ids', None)):
        pytest.skip('Exact resolver prerequisite belongs to the independently integrated selector lane')
    from test_geometry_identity_plan import geometry_setup, geometry_decision, payload as geometry_payload
    service, current, active = geometry_setup(tmp_path)
    from street_story.identity_scene import render_scene
    frozen_map = render_scene(current, active)['manifest']['objects']
    current['_identity_owner_hint'] = {'text': 'x' * 49000}
    calls, raw_outputs = [], []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        packet = json.loads(contents[-1].split('Данные ниже — только контекст:\n', 1)[1].split('\nThe previous response', 1)[0])
        assert packet['encoding'] == 'lossless-literals-and-map-labels-v1'
        plain = identity_source_selection.expand_planner_packet(packet)
        assert {row[1] for row in plain['map_scene']['objects']['rows']} == {'osm:way:2', 'osm:way:3'}
        # A MAP-label road pointer resolves against the full frozen dictionary,
        # not the compact textual physical-body rows.
        table = frozen_map
        objects = [dict(zip(table['columns'], row)) for row in table['rows']]
        labels = {row['candidate_id']: row['label'] for row in objects}
        decision = copy.deepcopy(geometry_decision())
        decision['candidate_id'] = '@' + str(labels['osm:way:2'])
        decision['decisive_relations'][0]['map_features'][0]['candidate_id'] = '@' + str(labels['osm:way:2'])
        decision['decisive_relations'][0]['map_features'][1]['candidate_id'] = '@' + str(labels['osm:way:9'])
        decision['rejected_alternatives'][0]['candidate_id'] = '@' + str(labels['osm:way:3'])
        decision['assumptions'] = ['Prose @1/$1 is kept exactly, not interpreted as an ID.']
        result = geometry_payload(decision)
        if malformed_first and len(calls) == 1:
            result.pop('first_wave_hypotheses')
        raw = json.dumps(result, ensure_ascii=False)
        raw_outputs.append(raw)
        return SimpleNamespace(text=raw, response_id=f'fixture-{len(calls)}')
    async def forbidden(*args):
        pytest.fail('Exact transport joins do not authorize another identity judge')
    service.providers.gemini = SimpleNamespace(executor=pool(service), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, current, '', active)
    result = current['_identity_geometry_result']
    assert result['proof_kind'] == 'geometry' and result['candidate_id'] == 'osm:way:2'
    assert result['geometry_proof']['decision']['assumptions'] == ['Prose @1/$1 is kept exactly, not interpreted as an ID.']
    assert len(calls) == (2 if malformed_first else 1)
    receipts = current['_identity_search_plan_payload']['identity_response_id_resolutions']
    assert [item['joint_stage'] for item in receipts] == (['initial', 'followup'] if malformed_first else ['initial'])
    assert [item['raw_json_sha256'] for item in receipts] == [hashlib.sha256(raw.encode()).hexdigest() for raw in raw_outputs]
    assert all(item['policy'] == 'exact-context-id-references-v1' and item['resolved_count'] == 4 for item in receipts)
