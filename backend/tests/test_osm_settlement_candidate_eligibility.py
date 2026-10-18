import pytest

from street_story.identity_candidate_policy import osm_identity_eligible
from street_story.identity_lifecycle import visual_match
from street_story.mvp_research import MvpResearchStreetStoryService


@pytest.mark.parametrize('place', ['city', 'town', 'village'])
def test_explicit_osm_settlement_stays_context_before_dispatch(place):
    tags = {'place': place, 'name': 'Arbitrary untranslated label', 'wikidata': 'Q1829'}
    assert not osm_identity_eligible(tags)
    catalog = MvpResearchStreetStoryService._candidate_catalog({'nearby': [{
        'type': 'node', 'id': 27048976, 'tags': tags, 'selection_bucket': 'landmark',
        'distance_m': 236.2}]}, [])
    assert len(catalog) == 1  # retained context, not an invented physical object
    candidate = catalog[0]
    assert candidate['identity_eligible'] is False
    assert candidate['identity_ineligible_reason'] == 'osm_settlement_context'
    assert candidate['candidate_id'] == 'osm:node:27048976'
    raw = {'status': 'match', 'candidate_id': candidate['candidate_id'], 'confidence': 1,
           'observations': ['Model sees a city illustration'], 'alternative_candidate_ids': [],
           '_references_sent': [candidate['candidate_id']]}
    assert not visual_match(raw, catalog)


@pytest.mark.parametrize('tags', [
    {'place': 'square', 'name': 'Public square'},
    {'building': 'yes', 'name': 'Actual building'},
    {'historic': 'monument', 'name': 'Actual monument'},
    {'place': 'city', 'building': 'yes', 'name': 'Actual building'},
    {'place': 'town', 'historic': 'monument', 'name': 'Actual monument'},
    {'place': 'village', 'man_made': 'tower', 'name': 'Actual tower'},
])
def test_physical_square_building_and_monument_remain_eligible(tags):
    assert osm_identity_eligible(tags)
    candidate = MvpResearchStreetStoryService._candidate_catalog({'nearby': [{
        'type': 'way', 'id': 2, 'tags': tags, 'selection_bucket': 'nearby', 'distance_m': 30}]}, [])[0]
    assert candidate.get('identity_eligible') is not False


@pytest.mark.parametrize('tags', [
    {'place': 'city', 'building': 'no'}, {'place': 'town', 'historic': 'no'},
    {'place': 'village', 'man_made': 'no'},
])
def test_explicit_negative_object_tag_does_not_make_settlement_physical(tags):
    assert not osm_identity_eligible(tags)


def test_name_city_word_does_not_drive_mechanical_classification():
    assert osm_identity_eligible({'name': 'City tower', 'building': 'yes'})
    assert osm_identity_eligible({'name': 'Калининград', 'place': 'square'})
    assert not osm_identity_eligible({'name': 'No locality word at all', 'place': 'city'})
