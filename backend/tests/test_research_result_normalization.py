from street_story.research_result_normalization import normalize_result
from jsonschema import Draft202012Validator


def schema(numbers):
    return {'type': 'object', 'properties': {'decisions': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'fact': {'type': 'integer', 'enum': numbers},
            'verdict': {'type': 'string'}, 'equivalent_to_existing': {'type': 'string'},
            'support_complete': {'type': 'boolean'}},
        'required': ['fact', 'verdict', 'support_complete'], 'additionalProperties': False}}}}


def test_optional_null_and_singleton_address_preserve_raw_semantics():
    raw = {'decisions': [{'verdict': 'insufficient', 'support_complete': False, 'equivalent_to_existing': None}]}
    normalized = normalize_result(raw, schema([4]))
    assert normalized == {'decisions': [{'fact': 4, 'verdict': 'insufficient', 'support_complete': False}]}
    assert raw['decisions'][0]['equivalent_to_existing'] is None and 'fact' not in raw['decisions'][0]
    assert Draft202012Validator(schema([4])).is_valid(normalized)


def test_ambiguous_address_and_required_flags_are_never_invented():
    raw = {'decisions': [{'verdict': 'supported', 'support_complete': None}]}
    assert normalize_result(raw, schema([0, 1])) == raw
    assert not Draft202012Validator(schema([0])).is_valid(normalize_result(raw, schema([0])))


def test_nullable_union_and_foreign_fields_are_preserved_for_normal_validation():
    contract = schema([0])
    properties = contract['properties']['decisions']['items']['properties']
    properties['equivalent_to_existing'] = {'anyOf': [{'type': 'string'}, {'type': 'null'}]}
    raw = {'decisions': [{'fact': 0, 'verdict': 'supported', 'support_complete': True,
                         'equivalent_to_existing': None, 'foreign': None}]}
    normalized = normalize_result(raw, contract)
    assert normalized == raw
    assert not Draft202012Validator(contract).is_valid(normalized)
