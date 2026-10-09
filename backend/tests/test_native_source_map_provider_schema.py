"""Native JSON schema subset is a provider transport, never geometry proof relaxation."""
from copy import deepcopy

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
