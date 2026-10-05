"""A venue is separate from its building, but may name a physical alternative."""
import copy

import pytest

from street_story.identity_entity_aliases import enrich_entity_links
from street_story.identity_lifecycle import visual_match
from street_story.identity_subject_binding import subject_aliases
from test_identity_entity_aliases import inputs, relation


def catalog_at_other_building():
    candidates, osm, wiki = inputs()
    other = copy.deepcopy(candidates[0])
    other.update(candidate_id='osm:way:10', name='Other physical gate', osm_id='osm:way:10')
    other.pop('wikidata', None)
    other.pop('wikipedia_url', None)
    other['url'] = 'https://www.openstreetmap.org/way/10'
    candidates.append(other)
    other_osm = copy.deepcopy(osm['nearby'][0])
    other_osm['id'] = 10
    other_osm['tags'].pop('wikidata', None)
    other_osm['tags'].pop('wikipedia', None)
    osm['nearby'].append(other_osm)
    return enrich_entity_links(candidates, osm, wiki,
        institutional_locations=[relation(physical_subject_candidate_id='osm:way:10')])


def verdict(**values):
    return {'status': 'match', 'candidate_id': 'osm:way:5', 'confidence': .98,
            'observations': ['Same distinctive arch and windows.'],
            '_references_sent': ['osm:way:5'], 'alternative_candidate_ids': ['osm:node:6'], **values}


def test_documented_venue_at_another_building_is_a_real_physical_alternative():
    candidates = catalog_at_other_building()
    venue = next(candidate for candidate in candidates if candidate['candidate_id'] == 'osm:node:6')
    assert venue['identity_eligible'] is False
    assert venue['physical_subject_candidate_id'] == 'osm:way:10'
    assert subject_aliases(candidates)['osm:node:6'] == {'osm:node:6'}
    assert not visual_match(verdict(), [candidates[0]], candidates)


def test_documented_venue_inside_selected_building_does_not_create_false_competition():
    candidates, osm, wiki = inputs()
    candidates = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])
    assert visual_match(verdict(), [candidates[0]], candidates)
    assert subject_aliases(candidates)['osm:node:6'] == {'osm:node:6'}


def test_plain_similar_venue_name_and_proximity_do_not_discard_real_alternative():
    candidates, osm, wiki = inputs()
    candidates[-1]['name'] = candidates[0]['name']
    candidates = enrich_entity_links(candidates, osm, wiki)
    assert not visual_match(verdict(), [candidates[0]], candidates)


@pytest.mark.parametrize('field,value', [
    ('proof', 'model_says_same_building'),
    ('relation', 'near_physical_object'),
    ('institution_candidate_id', 'osm:node:unknown'),
    ('physical_subject_candidate_id', 'osm:way:10'),
    ('source_url', 'https://[invalid'),
    ('source_url', 'file:///official-claim'),
    ('source_quote', ''),
    ('source_sha256', 'not-a-document-hash'),
])
def test_incomplete_or_invalid_location_receipt_cannot_discard_venue_alternative(field, value):
    candidates, osm, wiki = inputs()
    candidates = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])
    candidates[-1]['physical_subject_evidence'][field] = value
    assert not visual_match(verdict(), [candidates[0]], candidates)


def test_documented_venue_is_not_entity_alias_even_with_building_metadata_or_old_registry_owner():
    candidates, osm, wiki = inputs()
    candidates = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])
    venue = candidates[-1]
    venue.update(wikidata=candidates[0]['wikidata'], wikipedia_url=candidates[0]['wikipedia_url'])
    registry = {item['candidate_id']: 'old-shared-owner' for item in candidates}
    aliases = subject_aliases(candidates, poi_aliases=registry)
    assert aliases[venue['candidate_id']] == {venue['candidate_id']}
    assert aliases[candidates[0]['candidate_id']] == {'osm:way:5', 'wiki:9'}
    candidates[0].update(discovery='wikimedia_entity_cluster', alias_candidate_ids=['osm:node:6'])
    assert subject_aliases(candidates)['osm:node:6'] == {'osm:node:6'}


def test_venue_same_building_still_blocks_other_explicit_physical_alternative_without_rewriting_model_result():
    candidates, osm, wiki = inputs()
    candidates = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])
    result = verdict(alternative_candidate_ids=['osm:node:6', 'osm:way:unknown'])
    original = copy.deepcopy(result)
    assert not visual_match(result, [candidates[0]], candidates)
    assert result == original


def test_unavailable_or_ineligible_building_keeps_venue_alternative_fail_closed():
    candidates = catalog_at_other_building()
    other = next(item for item in candidates if item['candidate_id'] == 'osm:way:10')
    other['identity_eligible'] = False
    assert not visual_match(verdict(), [candidates[0]], candidates)
    candidates.remove(other)
    assert not visual_match(verdict(), [candidates[0]], candidates)
