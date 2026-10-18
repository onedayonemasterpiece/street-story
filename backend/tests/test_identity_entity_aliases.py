import hashlib

import pytest

from street_story.identity_entity_aliases import enrich_entity_links, wikipedia_entity_url
from street_story.identity_lifecycle import visual_match
from street_story.identity_subject_binding import subject_aliases
from street_story.mvp_research import MvpResearchStreetStoryService


def inputs():
    page = {'pageid': 9, 'title': 'A historic gate', 'url': 'https://en.wikipedia.org/wiki/A_historic_gate',
            'pageprops': {'wikibase_item': 'Q123'}}
    building = {'id': 5, 'type': 'way', 'tags': {'name': 'Historic gate', 'historic': 'city_gate',
                'building': 'gatehouse', 'wikidata': 'Q123', 'wikipedia': 'en:A historic gate',
                'addr:city': 'City', 'addr:street': 'Street', 'addr:housenumber': '61'}}
    institution = {'id': 6, 'type': 'node', 'tags': {'name': 'A cultural platform', 'amenity': 'arts_centre',
                   'website': 'https://official.example/', 'addr:city': 'City', 'addr:street': 'Street',
                   'addr:housenumber': '61'}}
    reverse = {'osm_type': 'way', 'osm_id': 5, 'display_name': 'Gate, 61, Street, City'}
    candidates = [{'candidate_id': 'osm:way:5', 'name': reverse['display_name'], 'selection_bucket': 'reverse',
                   'url': 'https://www.openstreetmap.org/way/5'},
                  {'candidate_id': 'wiki:9', 'name': page['title'], 'url': page['url']},
                  {'candidate_id': 'osm:node:6', 'name': institution['tags']['name'],
                   'url': 'https://www.openstreetmap.org/node/6'}]
    return candidates, {'reverse': reverse, 'nearby': [building, institution]}, [page]


def relation(**changes):
    text = 'Official cultural institution website. Our institution is housed in the historic gate.'
    return {'institution_candidate_id': 'osm:node:6', 'physical_subject_candidate_id': 'osm:way:5',
            'proof': 'host_reviewed_official_location', 'relation': 'institution_housed_in_physical_object',
            'source_url': 'https://official.example/', 'source_text': text,
            'source_quote': 'Our institution is housed in the historic gate.',
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(), **changes}


def test_reverse_duplicate_reuses_exact_rich_osm_tags_and_wikipedia_pageprops():
    candidates, osm, wiki = inputs()
    enriched = enrich_entity_links(candidates, osm, wiki)
    assert 'wikidata' not in candidates[0]
    assert candidates[0]['name'] == 'Gate, 61, Street, City'
    assert enriched[0]['name'] == 'Historic gate'
    assert enriched[0]['display_name'] == candidates[0]['name']
    assert enriched[0]['wikidata'] == enriched[1]['wikidata'] == 'Q123'
    assert enriched[0]['wikipedia_url'] == enriched[1]['wikipedia_url']
    groups = subject_aliases(enriched)
    assert groups['osm:way:5'] == {'osm:way:5', 'wiki:9'}
    assert groups['osm:node:6'] == {'osm:node:6'}


def test_explicit_osm_wikipedia_link_does_not_need_pageprops_or_name_equivalence():
    candidates, osm, wiki = inputs()
    wiki[0].pop('pageprops')
    osm['nearby'][0]['tags'].pop('wikidata')
    candidates[0]['name'] = 'Unknown alias with a different name'
    enriched = enrich_entity_links(candidates, osm, wiki)
    assert subject_aliases(enriched)['wiki:9'] == {'wiki:9', 'osm:way:5'}


def test_normal_catalog_automatically_recovers_reverse_duplicate_entity_links():
    _, osm, wiki = inputs()
    osm['reverse']['selection_bucket'] = 'reverse'
    wiki[0].pop('pageprops')
    catalog = MvpResearchStreetStoryService._candidate_catalog(osm, wiki)
    by_id = {item['candidate_id']: item for item in catalog}
    assert by_id['osm:way:5']['wikidata'] == 'Q123'
    assert by_id['osm:way:5']['wikipedia_url'] == wiki[0]['url']
    assert by_id['osm:way:5']['name'] == 'Historic gate'
    assert subject_aliases(catalog)['osm:way:5'] == {'osm:way:5', 'wiki:9'}
    assert by_id['osm:node:6'].get('identity_eligible') is not False
    result = {'status': 'match', 'candidate_id': 'osm:way:5', 'confidence': .98,
              'observations': ['Distinctive stone entrance corresponds.'],
              '_references_sent': ['osm:way:5'], 'alternative_candidate_ids': ['wiki:9']}
    assert visual_match(result, [by_id['osm:way:5']], catalog)
    assert not visual_match({**result, 'alternative_candidate_ids': ['osm:node:6']},
                            [by_id['osm:way:5']], catalog)


def test_sparse_reverse_entry_cannot_erase_exact_received_physical_contour_for_t():
    from street_story.identity_architectural_evidence import literal_evidence_inventory
    reverse = {'osm_type': 'way', 'osm_id': 5, 'category': 'building', 'type': 'yes',
        'display_name': 'Reverse address entry', 'selection_bucket': 'reverse'}
    body = {'type': 'way', 'id': 5, 'tags': {'building': 'yes', 'addr:street': 'Literal street',
        'addr:housenumber': '7'}, 'building_entrance_node_ids': [6],
        'geometry': [{'lat': 54.7, 'lon': 20.5}, {'lat': 54.701, 'lon': 20.5},
            {'lat': 54.701, 'lon': 20.501}, {'lat': 54.7, 'lon': 20.5}]}
    other = {'type': 'way', 'id': 8, 'tags': {'building': 'yes', 'name': 'Other observed body'}}
    osm = {'reverse': reverse, 'observed_pool': [body, other], 'nearby': [body, other]}
    candidates = MvpResearchStreetStoryService._candidate_catalog(osm, [], observed_pool=True)
    received = next(c for c in candidates if c['candidate_id'] == 'osm:way:5')
    assert received['map_object']['tags']['building'] == 'yes'
    assert received['map_geometry'] and received['map_address']['provenance'] == 'osm.tags'
    assert received['map_object']['building_entrances']['candidate_ids'] == ['osm:node:6']
    inventory = literal_evidence_inventory({}, candidates, [], candidate_ids=['osm:way:5'])
    assert inventory['physical_subjects'][0]['candidate_id'] == 'osm:way:5'
    assert len(candidates) == 2 and 'tags' not in reverse


def test_same_names_addresses_and_distance_cannot_merge_institution_or_other_building():
    candidates, osm, wiki = inputs()
    candidates[-1]['name'] = candidates[0]['name']
    enriched = enrich_entity_links(candidates, osm, wiki)
    assert enriched[-1].get('identity_eligible') is not False
    assert subject_aliases(enriched)['osm:node:6'] == {'osm:node:6'}


def test_documented_institution_location_remains_distinct_context_not_canonical_alias():
    candidates, osm, wiki = inputs()
    enriched = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])
    assert enriched[-1]['identity_role'] == 'institution_at_physical_subject'
    assert enriched[-1]['physical_subject_candidate_id'] == 'osm:way:5'
    assert enriched[-1]['identity_eligible'] is False
    assert subject_aliases(enriched)['osm:node:6'] == {'osm:node:6'}
    assert 'source_text' not in enriched[-1]['physical_subject_evidence']
    verdict = {'status': 'match', 'candidate_id': 'osm:way:5', 'confidence': .98,
               'observations': ['Same distinguishing brick arch and windows.'],
               '_references_sent': ['osm:way:5'], 'alternative_candidate_ids': ['wiki:9', 'osm:node:6']}
    assert visual_match(verdict, [enriched[0]], enriched)


@pytest.mark.parametrize('changes', [{'proof': 'model_claim'}, {'source_url': 'https://other.example/'},
                                   {'source_sha256': 'a' * 64}, {'source_quote': 'Not in the source'},
                                   {'physical_subject_candidate_id': 'outside-shortlist'}])
def test_unproved_or_unbound_institution_relation_never_disqualifies_an_alternative(changes):
    candidates, osm, wiki = inputs()
    enriched = enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation(**changes)])
    assert enriched[-1].get('identity_eligible') is not False


def test_explicit_relation_also_requires_matching_address_and_physical_building_type():
    candidates, osm, wiki = inputs()
    osm['nearby'][1]['tags']['addr:housenumber'] = '63'
    assert enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])[-1].get('identity_eligible') is not False
    osm['nearby'][1]['tags']['addr:housenumber'] = '61'
    osm['nearby'][0]['tags'].pop('building')
    assert enrich_entity_links(candidates, osm, wiki, institutional_locations=[relation()])[-1].get('identity_eligible') is not False


def test_conflicting_entity_links_are_reported_and_never_new_alias_evidence():
    candidates, osm, wiki = inputs()
    conflicting = {**osm['nearby'][0], 'tags': {**osm['nearby'][0]['tags'], 'wikidata': 'Q999'}}
    osm['nearby'].append(conflicting)
    enriched = enrich_entity_links(candidates, osm, wiki)
    assert enriched[0]['entity_link_conflicts'] == ['wikidata']
    assert 'wikidata' not in enriched[0] and 'wikipedia_url' not in enriched[0]
    assert subject_aliases(enriched)['osm:way:5'] == {'osm:way:5'}


def test_wikipedia_entity_url_only_normalizes_exact_canonical_page_addresses():
    assert wikipedia_entity_url('https://ru.wikipedia.org/wiki/Название страницы') == 'https://ru.wikipedia.org/wiki/%D0%9D%D0%B0%D0%B7%D0%B2%D0%B0%D0%BD%D0%B8%D0%B5_%D1%81%D1%82%D1%80%D0%B0%D0%BD%D0%B8%D1%86%D1%8B'
    assert wikipedia_entity_url('https://en.m.wikipedia.org/wiki/A_gate') == 'https://en.wikipedia.org/wiki/A_gate'
    assert wikipedia_entity_url('https://evil.example/wiki/A_gate') is None
    assert wikipedia_entity_url('https://en.wikipedia.org/wiki/City#Gate') is None
