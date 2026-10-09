"""MAP context membership does not broaden the physical nomination contract."""
import copy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from street_story import identity_discovery
from street_story.providers import PermanentProviderError
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload


def test_exact_role_diagnostics_preserve_choices_and_nomination_enum():
    manifest = {'objects': {'columns': ['label', 'candidate_id'],
        'rows': [[1, 'osm:way:2'], [3, 'osm:way:9']]}}
    plan = {'observed_candidate_ids': ['osm:way:9', 'osm:way:2', 'osm:way:999', 'osm:way:9']}
    original = copy.deepcopy(plan)
    issues = identity_discovery._nomination_binding_issues(plan, ['osm:way:2'], manifest)
    assert issues == [
        {'field': 'observed_candidate_ids', 'candidate_id': 'osm:way:9',
            'nomination_allowed': False, 'input_role': 'received_map_context'},
        {'field': 'observed_candidate_ids', 'candidate_id': 'osm:way:999',
            'nomination_allowed': False, 'input_role': 'outside_nomination_catalog'}]
    schema = {'type': 'object', 'properties': {'observed_candidate_ids': {
        'type': 'array', 'items': {'type': 'string', 'enum': ['osm:way:2']}}}}
    assert not Draft202012Validator(schema).is_valid(plan)
    assert plan == original
    assert identity_discovery._nomination_binding_issues({'observed_candidate_ids': ['osm:way:2']},
        ['osm:way:2'], manifest) == []


@pytest.mark.parametrize('plan', [None, {}, {'observed_candidate_ids': None},
    {'observed_candidate_ids': 'osm:way:9'}, {'observed_candidate_ids': [None, {}]}])
def test_malformed_shapes_remain_original_validator_responsibility(plan):
    assert identity_discovery._nomination_binding_issues(plan, [], None) == []


@pytest.mark.asyncio
async def test_one_role_aware_repair_retains_road_as_actual_feature_and_source_map_bytes(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    initial = payload(geometry_decision())
    initial['observed_candidate_ids'] = ['osm:way:9']
    repaired = payload(geometry_decision())
    repaired['observed_candidate_ids'] = ['osm:way:2']
    calls = []

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert 'received MAP context ID' in contents[-1]
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert len(calls) == 2
        assert 'nomination_roles' in contents[-1]
        assert 'received_map_context' in contents[-1] and 'osm:way:9' in contents[-1]
        assert 'does not authorize physical nomination' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        return SimpleNamespace(text=json.dumps(repaired))

    async def forbidden(*args, **kwargs):
        pytest.fail('Corrected joint2 must not require a third planner or REF')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(calls) == 2
    proof = story['_identity_geometry_result']['geometry_proof']
    assert proof['decision']['candidate_id'] == 'osm:way:2'
    assert proof['decision']['decisive_relations'][0]['map_features'][1] == {
        'candidate_id': 'osm:way:9', 'kind': 'road_axis'}
    assert history['search_plan']['payload']['observed_candidate_ids'] == ['osm:way:2']
    diagnostic = service._identity_snapshot(story['id'])[1]['identity_closed_invalid_plan']
    assert json.loads(diagnostic['raw_json'])['observed_candidate_ids'] == ['osm:way:9']


@pytest.mark.asyncio
async def test_repeated_context_nomination_stays_rejected_without_third_send_or_restart_resend(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    invalid = payload(geometry_decision())
    invalid['observed_candidate_ids'] = ['osm:way:9']
    calls = []

    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=json.dumps(invalid))

    async def forbidden(*args, **kwargs):
        pytest.fail('Closed invalid joint2 must not authorize another planner')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['joint', 'joint'] and '_identity_geometry_result' not in story
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, story, '', active)
    assert calls == ['joint', 'joint']
