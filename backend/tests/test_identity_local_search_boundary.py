from types import SimpleNamespace

import pytest

from street_story import identity_discovery as discovery
from street_story.identity_candidate_policy import wikipedia_coordinate_context
from street_story.identity_subject_binding import bind_reference_subject
from street_story.mvp_research import MvpResearchStreetStoryService

CAPTURE = {'latitude': 54.7104733997, 'longitude': 20.5069561}
LOCAL = {'coordinates': [{'lat': 54.7106186, 'lon': 20.5064098, 'primary': True, 'globe': 'earth'}]}
REMOTE = {'coordinates': [{'lat': 53.01056, 'lon': 18.60444, 'primary': True, 'globe': 'earth'}]}


def test_authoritative_remote_coordinate_is_context_outside_existing_local_domain():
    result = wikipedia_coordinate_context(REMOTE, CAPTURE, 750)
    assert result['identity_eligible'] is False
    assert result['identity_ineligible_reason'] == 'outside_local_search_footprint'
    assert result['distance_m'] > 200_000
    assert result['coordinate_provenance'] == 'wikipedia.coordinates.primary'
    assert result['distance_provenance'] == 'capture_to_wikipedia_primary_coordinate'
    assert result['local_search_radius_m'] == 750


def test_local_coordinates_are_not_identity_confirmation():
    result = wikipedia_coordinate_context(LOCAL, CAPTURE, 750)
    assert 35 < result['distance_m'] < 45
    assert 'identity_eligible' not in result
    assert 'status' not in result


@pytest.mark.parametrize('page,story,radius', [
    ({}, CAPTURE, 750), (REMOTE, {}, 750), (REMOTE, CAPTURE, None),
    ({'coordinates': [{'lat': 53, 'lon': 18, 'primary': False}]}, CAPTURE, 750),
    ({'coordinates': [{'lat': 53, 'lon': 18, 'primary': True, 'globe': 'mars'}]}, CAPTURE, 750),
    ({'coordinates': [{'lat': float('nan'), 'lon': 18, 'primary': True}]}, CAPTURE, 750),
    (REMOTE, {'latitude': True, 'longitude': 20}, 750),
])
def test_missing_or_untrusted_geometry_does_not_invent_remote_exclusion(page, story, radius):
    assert wikipedia_coordinate_context(page, story, radius).get('identity_eligible') is not False


@pytest.mark.asyncio
async def test_retrieval_fetches_coordinates_without_new_classifier_and_preserves_reference_source(monkeypatch):
    calls = []

    async def api(service, client, endpoint, params):
        calls.append(params)
        if endpoint == discovery.WIKI:
            assert 'coordinates' in params['prop'].split('|')
            assert params['coprimary'] == 'primary'
            return [dict(REMOTE, pageid=308779, title='Remote tower', extract='A physical tower.',
                         original={'source': 'https://upload.wikimedia.org/remote.jpg'})]
        return []

    monkeypatch.setattr(discovery, 'api', api)
    service = SimpleNamespace(providers=SimpleNamespace(wikipedia=SimpleNamespace(search_radius_m=750)))
    candidates = await discovery.retrieve(service, ['visible landmark'], '', set(), story={
        **CAPTURE, '_identity_search_context': {'radius_m': 600}})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate['identity_eligible'] is False
    assert candidate['local_search_radius_m'] == 750
    assert candidate['reference_image_urls'] == ['https://upload.wikimedia.org/remote.jpg']
    assert candidate['candidate_id'] == 'wiki:308779'
    assert candidate['lat'] == 53.01056
    assert len(calls) == 2  # unchanged bounded Wikipedia + Commons-category calls


def test_cluster_representative_cannot_erase_context_or_geography_exclusion():
    shared = 'https://upload.wikimedia.org/shared.jpg'
    result = discovery.merge_candidates([
        {'candidate_id': 'commons:1', 'name': 'Exact target', 'reference_image_urls': [shared]},
        {'candidate_id': 'wiki:2', 'name': 'Remote documented subject', 'reference_image_urls': [shared],
         'identity_eligible': False, 'identity_ineligible_reason': 'outside_local_search_footprint',
         'discovery': 'wikipedia_text_search'},
    ], 'Exact target')
    assert len(result) == 1 and result[0]['candidate_id'] == 'commons:1'
    assert result[0]['identity_eligible'] is False
    assert result[0]['identity_ineligible_reason'] == 'outside_local_search_footprint'


def anonymous_catalog():
    raw = {'type': 'way', 'id': 133035113, 'center': {'lat': 54.7106186, 'lon': 20.5064098},
           'tags': {'building': 'yes'}, 'distance_m': 38.6, 'selection_bucket': 'nearby'}
    objects = [
        {'type': 'node', 'id': 1, 'tags': {'name': 'Restaurant'}, 'distance_m': 26.5, 'selection_bucket': 'nearby'},
        {'type': 'node', 'id': 2, 'tags': {'name': 'Shop'}, 'distance_m': 38.4, 'selection_bucket': 'nearby'}, raw,
        {'type': 'node', 'id': 3, 'tags': {'name': 'Office'}, 'distance_m': 46.6, 'selection_bucket': 'nearby'},
    ]
    return MvpResearchStreetStoryService._candidate_catalog({'nearby': objects}, []), raw


def test_nearest_anonymous_osm_building_survives_with_exact_physical_id_no_address_guess():
    catalog, raw = anonymous_catalog()
    assert [c['candidate_id'] for c in catalog][:3] == ['osm:node:1', 'osm:node:2', 'osm:way:133035113']
    candidate = catalog[2]
    assert candidate['identity_role'] == 'anonymous_physical_building'
    assert candidate['name'] == 'Здание без названия в OSM'
    assert candidate['reference_excerpt'] == '{"building":"yes"}'
    assert candidate['distance_m'] == 38.6
    assert candidate['center'] == {'lat': 54.7106186, 'lon': 20.5064098}
    assert candidate['url'] == 'https://www.openstreetmap.org/way/133035113'
    assert raw['tags'] == {'building': 'yes'}
    assert raw['center'] == {'lat': 54.7106186, 'lon': 20.5064098}


def test_anonymous_building_accepts_existing_article_subject_binding_only_after_explicit_model_selection():
    catalog, _ = anonymous_catalog()
    reference = {'candidate_id': 'web:article', 'identity_eligible': False,
                 'url': 'https://example.com/article', 'reference_id': 'article-ref',
                 'reference_image_urls': ['https://example.com/building.jpg']}
    evidence = {'candidate_id': 'web:article', 'article_url': reference['url'],
                'source_url': reference['reference_image_urls'][0], 'reference_id': 'article-ref'}
    raw = {'status': 'match', 'candidate_id': 'web:article', 'confidence': .99,
           '_references_sent': ['web:article'], '_reference_ids_sent': ['article-ref'],
           'reference_subject_candidate_id': 'osm:way:133035113'}
    result = bind_reference_subject(raw, [reference], catalog, [evidence])
    assert result['status'] == 'bound'
    assert result['result']['candidate_id'] == 'osm:way:133035113'
    assert bind_reference_subject({k: v for k, v in raw.items() if k != 'reference_subject_candidate_id'},
                                  [reference], catalog, [evidence])['status'] == 'unresolved'


@pytest.mark.parametrize('tags', [{'highway': 'residential'}, {}, {'building': 'no'}])
def test_unnamed_nonbuilding_is_not_fabricated_into_building(tags):
    assert MvpResearchStreetStoryService._candidate_catalog({'nearby': [
        {'type': 'way', 'id': 9, 'tags': tags, 'distance_m': 12, 'selection_bucket': 'nearby'}]}, []) == []


def test_named_road_is_not_fabricated_into_anonymous_building():
    assert MvpResearchStreetStoryService._candidate_catalog({'nearby': [
        {'type': 'way', 'id': 9, 'tags': {'name': 'Street', 'highway': 'residential'},
         'distance_m': 12, 'selection_bucket': 'nearby'}]}, []) == []
