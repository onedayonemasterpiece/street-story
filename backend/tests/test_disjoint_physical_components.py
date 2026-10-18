"""Saved OSM outer geometry supplies physical hypotheses, never identity proof."""
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx
import pytest

from street_story.db import Store
from street_story.identity_candidate_policy import promote_observed_candidates
from street_story.identity_map_context import expand_disjoint_building_components
from street_story.identity_subject_binding import bind_reference_subject, subject_aliases
from street_story.identity_lifecycle import visual_match
from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.providers import OSMClient
from test_identity_subject_binding import article, image_evidence, subject, verdict


def saved_relation(name):
    root = ET.parse(Path(__file__).parent / 'fixtures' / name).getroot()
    nodes = {e.attrib['id']: {'lat': float(e.attrib['lat']), 'lon': float(e.attrib['lon'])}
             for e in root.findall('node')}
    ways = {e.attrib['id']: [nodes[nd.attrib['ref']] for nd in e.findall('nd')]
            for e in root.findall('way')}
    relation = root.find('relation')
    return {'type': 'relation', 'id': int(relation.attrib['id']), 'selection_bucket': 'nearby',
        'tags': {e.attrib['k']: e.attrib['v'] for e in relation.findall('tag')},
        'members': [{'type': e.attrib['type'], 'ref': int(e.attrib['ref']), 'role': e.attrib['role'],
                     'geometry': ways[e.attrib['ref']]} for e in relation.findall('member')]}


def catalog(relation, **kwargs):
    return MvpResearchStreetStoryService._candidate_catalog({'nearby': [relation]}, [], **kwargs)


def test_saved_disjoint_relation_retains_context_without_aliasing_or_copying_parent_identity():
    relation = saved_relation('osm-two-exterior-components.osm')
    relation['tags'].update(wikidata='Q123', wikipedia='en:Parent complex', historic='yes')
    observed = catalog(relation, observed_pool=True)
    parent = next(c for c in observed if c['candidate_id'].startswith('osm:relation:'))
    members = [c for c in observed if c['candidate_id'].startswith('osm:way:')]
    assert parent['identity_eligible'] is False
    assert parent['physical_components']['component_count'] == 2
    assert len(parent['map_geometry']['lines']) == len(members) == 2
    for member in members:
        assert member['physical_component']['parent_candidate_id'] == parent['candidate_id']
        assert member['map_object']['provenance'] == 'osm.relation_outer_geometry'
        assert member['map_object']['tags'] == {'building': 'apartments'}
        assert member['parent_relation_context']['scope'] == 'parent_relation_only'
        assert member['parent_relation_context']['tags']['addr:housenumber'] == '19А'
        assert 'map_address' not in member and 'wikidata' not in member and 'wikipedia_url' not in member
    aliases = subject_aliases(observed)
    assert all(aliases[c['candidate_id']] == {c['candidate_id']} for c in observed)


def test_saved_connected_outer_fragments_stay_one_physical_building():
    relation = saved_relation('osm-one-fragmented-exterior.osm')
    observed = catalog(relation, observed_pool=True)
    assert len(observed) == 1
    assert observed[0].get('identity_eligible') is not False
    assert 'physical_components' not in observed[0]


def test_exact_wikipedia_alias_cannot_turn_whole_disjoint_aggregate_into_one_building():
    relation = saved_relation('osm-two-exterior-components.osm')
    relation['tags'].update(wikidata='Q123', wikipedia='en:Parent complex')
    pages = [{'pageid': 777, 'title': 'Parent complex', 'wikidata': 'Q123',
              'url': 'https://en.wikipedia.org/wiki/Parent_complex'}]
    for full in (False, True):
        candidates = MvpResearchStreetStoryService._candidate_catalog({'nearby': [relation]}, pages, observed_pool=full)
        wiki = next(c for c in candidates if c['candidate_id'] == 'wiki:777')
        assert wiki['identity_eligible'] is False
        assert wiki['physical_components']['component_count'] == 2
        assert not visual_match(verdict(wiki['candidate_id']), [wiki], candidates)


@pytest.mark.parametrize('change', ['missing', 'open', 'inner', 'touching'])
def test_incomplete_inner_or_touching_rings_do_not_invent_disjoint_members(change):
    relation = saved_relation('osm-two-exterior-components.osm')
    if change == 'missing':
        relation['members'][0].pop('geometry')
    elif change == 'open':
        relation['members'][0]['geometry'] = relation['members'][0]['geometry'][:-1]
    elif change == 'inner':
        relation['members'][0]['role'] = 'inner'
    else:
        relation['members'][1]['geometry'] = relation['members'][0]['geometry']
    assert len(expand_disjoint_building_components([relation])) == 1


def test_member_outside_active_shortlist_promotes_only_with_actual_reference_proof():
    relation = saved_relation('osm-two-exterior-components.osm')
    observed = catalog(relation, observed_pool=True)
    parent = next(c for c in observed if c['candidate_id'].startswith('osm:relation:'))
    member = next(c for c in observed if c['candidate_id'].startswith('osm:way:'))
    active = [subject(f'wiki:{i}') for i in range(1, 17)]
    assert promote_observed_candidates(active, observed, [parent['candidate_id']]) == active
    raw = verdict('web:article', reference_subject_candidate_id=parent['candidate_id'], source_subject_scope='building')
    assert bind_reference_subject(raw, [article()], active, [image_evidence()], observed_candidates=observed)['status'] == 'unresolved'
    raw['reference_subject_candidate_id'] = member['candidate_id']
    no_proof = bind_reference_subject(raw, [article()], active, [], observed_candidates=observed)
    assert no_proof['reason'] == 'reference_provenance_missing' and len(active) == 16
    bound = bind_reference_subject(raw, [article()], active, [image_evidence()], observed_candidates=observed)
    assert bound['status'] == 'bound' and active[-1]['candidate_id'] == member['candidate_id']
    assert visual_match(bound['result'], [article()], active)
    assert not visual_match({**bound['result'], 'alternative_candidate_ids': [
        c['candidate_id'] for c in observed if c['candidate_id'].startswith('osm:way:') and c != member]}, [article()], active)
    assert not visual_match(verdict(parent['candidate_id']), [parent])


@pytest.mark.asyncio
async def test_real_provider_full_pool_and_active_catalog_preserve_disjoint_member_geometry(tmp_path):
    relation = saved_relation('osm-two-exterior-components.osm')
    async def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'osm_type': 'relation', 'osm_id': relation['id'],
                'lat': '54.7129658', 'lon': '20.5178254', 'category': 'building', 'type': 'apartments',
                'display_name': 'Parent address'})
        return httpx.Response(200, json={'elements': [relation]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OSMClient(Store(tmp_path / 'db.sqlite3'), 'fixture', client).lookup(54.713394, 20.5166867)
    assert len(result['observed_pool']) == 3
    active = MvpResearchStreetStoryService._candidate_catalog(result, [])
    full = MvpResearchStreetStoryService._candidate_catalog(result, [], observed_pool=True)
    parent = next(c for c in active if c['candidate_id'].startswith('osm:relation:'))
    assert parent['identity_eligible'] is False and len(parent['map_geometry']['lines']) == 2
    nearest = min((c for c in full if c['candidate_id'].startswith('osm:way:')), key=lambda c: c['distance_m'])
    assert nearest['candidate_id'] == 'osm:way:903713161' and nearest['camera_inside_footprint'] is True
    assert nearest['distance_m'] == 0 and len(active) <= 16
    from street_story.live_visual_comparison import LiveVisualComparisonMixin
    reply = LiveVisualComparisonMixin._visual_reply('fixture', [], {'candidates': full}, 0)
    assert {c['candidate_id'] for c in reply['physical_candidates']} == set(parent['physical_components']['exact_member_candidate_ids'])
    assert all(c['physical_component']['parent_candidate_id'] == parent['candidate_id'] for c in reply['physical_candidates'])
