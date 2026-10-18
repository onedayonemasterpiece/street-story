"""Large observed ID pools need not be repeated in the model contract."""
import copy
import json

from jsonschema import Draft202012Validator

from street_story.identity_architectural_context import regional_selection_schema
from street_story.identity_source_selection import identity_transport_schema


def test_received_cards_keep_strict_host_membership_without_repeated_osm_enum():
    ids = [f'osm:way:{number}' for number in range(1000)]
    schema = {'type': 'object', 'properties': {'regional_article_selections':
        regional_selection_schema(ids, {'results': [{'article_id': 'prussia39:sid:42'}]})}}
    original = copy.deepcopy(schema)
    transport = identity_transport_schema(schema)
    assert schema == original
    assert len(json.dumps(transport)) < 1500
    assert len(json.dumps(schema)) > 15000
    choice = {'article_id': 'prussia39:sid:42', 'candidate_id': ids[-1],
        'scope': 'One physical body.', 'binding_basis': 'Received address and footprint.',
        'physical_binding_resolved': True}
    validator = Draft202012Validator(schema)
    assert validator.is_valid({'regional_article_selections': [choice]})
    foreign = {**choice, 'candidate_id': 'osm:way:unreceived'}
    assert Draft202012Validator(transport).is_valid({'regional_article_selections': [foreign]})
    assert not validator.is_valid({'regional_article_selections': [foreign]})
    assert not Draft202012Validator(transport).is_valid({'regional_article_selections':
        [{**choice, 'article_id': 'prussia39:sid:unreceived'}]})
