"""Native JSON schema subset is a provider transport, never geometry proof relaxation."""
from copy import deepcopy

import pytest

from jsonschema import Draft202012Validator

from street_story.identity_source_selection import geometry_decision_schema
from street_story.native_vision import native_source_map_provider_schema


def key_set(node):
    if isinstance(node, list):
        return set().union(*(key_set(x) for x in node))
    if isinstance(node, dict):
        return set(node) | set().union(*(key_set(x) for x in node.values()))
    return set()


def test_source_map_schema_strips_unsupported_native_format_keywords():
    original = {
        'type': 'object', 'properties': {
            'regional_lookup': {
                'type': 'object', 'properties': {
                    'candidate_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                      'items': {'type': 'string', 'maxLength': 100}}},
                'required': ['candidate_ids'], 'additionalProperties': False},
            'accepted_geometry': geometry_decision_schema(['osm:way:1', 'osm:way:2'], structured=True)},
        'required': ['accepted_geometry'], 'additionalProperties': False}
    before = deepcopy(original)
    projected = native_source_map_provider_schema(original)
    assert original == before
    assert projected['required'] == ['regional_lookup', 'accepted_geometry']
    assert projected['additionalProperties'] is False
    assert projected['properties']['regional_lookup']['properties']['candidate_ids']['type'] == 'array'
    assert projected['properties']['accepted_geometry']['properties']['spatial_correspondence']['type'] == 'object'
    assert not key_set(projected).intersection({
        'uniqueItems', 'allOf', 'not', 'if', 'then', 'else',
        'minItems', 'maxItems', 'minLength', 'maxLength', 'minimum', 'maximum', 'pattern',
        'oneOf', 'const'})
    assert 'uniqueItems' in key_set(original)
    assert 'allOf' in key_set(original)
    Draft202012Validator.check_schema(projected)


def test_host_still_rejects_missing_accepted_geometry_structure():
    original = geometry_decision_schema(['osm:way:1'], structured=True)
    provider = native_source_map_provider_schema(original)
    # The provider may allow this coarse JSON shape; original host remains
    # authoritative and rejects an accepted decision without spatial proof.
    candidate = {'decision': 'accepted_geometry', 'candidate_id': 'osm:way:1',
        'scope': 'A candidate building', 'decisive_relations': [], 'rejected_alternatives': [],
        'assumptions': [], 'bounded_coverage': {'scope': 'local', 'limitations': [],
            'material_alternatives_resolved': True},
        'camera_pose': {'position_basis': 'GPS', 'yaw_basis': 'guess', 'sensitivity': 'unknown'},
        'next_action': {'kind': 'none', 'reason': '', 'target_candidate_ids': []}}
    assert not Draft202012Validator(original).is_valid(candidate)
    assert provider['type'] == 'object'


@pytest.mark.asyncio
async def test_completed_native_invalid_uncertain_plan_recovers_through_text_only(tmp_path):
    """Wrong address pointer cannot strand a real closed SOURCE/MAP attempt."""
    from dataclasses import replace
    from types import SimpleNamespace
    from street_story import identity_discovery
    from street_story.gemini import GeminiUnavailable
    from test_geometry_identity_plan import geometry_setup, payload, geometry_decision

    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='text',
                               gemini_web_search_tertiary_model='visual')
    calls = []
    class QuotaUnavailable:
        async def execute(self, operation, call):
            calls.append('google_not_sent')
            raise GeminiUnavailable(service.store.now() + 3600, 'fixture_rpd')
    async def forbidden(*args, **kwargs):
        pytest.fail('Native closed: no duplicate Google send')
    async def native(s, prompt, schema, images, host_context):
        calls.append('native_received_source_map')
        assert [label for label, _mime, _raw in images] == ['SOURCE', 'MAP']
        invalid = payload(geometry_decision())
        invalid['accepted_geometry'].update(decision='uncertain', candidate_id='',
                                            candidate_label=0)
        invalid['first_wave_hypotheses'] = [
            {'kind': 'address', 'subject_id': 'osm:way:2', 'query': '',
             'reason': 'Wrong kind of address subject returned by the model.'}]
        return {'result': invalid, 'host_context': host_context,
            'receipt': {'phase': 'completed', 'turn_id': 'native-durable-turn'}}
    async def text_fallback(s, prompt, schema):
        calls.append('text_after_invalid_native')
        assert 'SOURCE and MAP images are unavailable' in prompt
        result = {k: v for k, v in payload(geometry_decision()).items()
                  if k != 'accepted_geometry'}
        return {'result': result}
    google = QuotaUnavailable()
    service.providers.gemini = SimpleNamespace(executor=google, _generate=forbidden,
        web_search_routes=[('visual', object(), object(), google)], research_routes=[])
    service.providers.research = SimpleNamespace(
        source_map_available=True, plan_source_map=native,
        source_map_receipt=lambda s: None, plan_identity_search=text_fallback)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['google_not_sent', 'native_received_source_map',
                     'text_after_invalid_native']
    assert story['_identity_search_plan_route'] == 'qualified_text_fallback'
    assert '_identity_geometry_result' not in story
