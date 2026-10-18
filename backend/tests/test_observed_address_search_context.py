"""Address hints retain literal map provenance without becoming building proof."""
import json
from types import SimpleNamespace

import pytest

from street_story.identity_discovery import _map_query_context, suggest
from street_story.identity_source_selection import (IDENTITY_SOURCE_POLICY, observed_address_context,
    compact_candidate_catalog, model_identity_context)
from test_visual_search_continuation import prepared


def observed():
    nodes = [{'candidate_id': 'osm:node:' + str(index), 'distance_m': distance,
        'map_address': {'city': 'Observed City', 'street': 'Exact avenue', 'house_number': number},
        'map_object': {'tags': {'entrance': 'staircase'}}}
        for index, number, distance in [(1, '7A', 20), (2, '9-11', 30), (3, '13', 1)]]
    building = {'candidate_id': 'osm:way:4', 'boundary_distance_m': 12,
        'footprint_bearing_interval': {'start_degrees': 200, 'end_degrees': 220},
        'map_object': {'tags': {'building': 'yes'}, 'building_entrances': {
            'candidate_ids': ['osm:node:1', 'osm:node:2'], 'proof': 'osm_closed_way_node_membership'}}}
    return [building, *nodes]


def test_exact_membership_keeps_full_addresses_and_does_not_join_nearest_unrelated_entrance():
    story = {'_identity_observed_candidates': observed()}
    context = observed_address_context(story)
    assert context['address_anchors'][0]['mapped_entry_id'] == 'osm:node:3'
    assert context['observed_localities'] == ['Observed City']
    linked = context['building_address_memberships'][0]
    assert linked['physical_candidate_id'] == 'osm:way:4'
    assert [item['address']['house_number'] for item in linked['address_entries']] == ['7A', '9-11']
    assert linked['footprint_bearing_interval'] == {'start_degrees': 200, 'end_degrees': 220}
    assert 'house_number' not in linked  # No manufactured combined range/building address.
    assert 'candidate_id' not in context and 'identity' not in context
    assert _map_query_context(story, [])['observed_address_context'] == context


def test_raw_observed_address_node_is_kept_without_inventing_city_or_membership():
    story = {'research_json': json.dumps({'osm': {'observed_pool': [{
        'type': 'node', 'id': 5, 'distance_m': 8,
        'tags': {'addr:street': 'Literal road', 'addr:housenumber': '2Б', 'entrance': 'yes'}}]}})}
    context = observed_address_context(story)
    anchor = context['address_anchors'][0]
    assert anchor['address'] == {'street': 'Literal road', 'house_number': '2Б'}
    assert anchor['entry_kind'] == 'entrance'
    assert context['observed_localities'] == [] and context['building_address_memberships'] == []


@pytest.mark.asyncio
async def test_planner_receives_exact_address_geometry_and_nomination_excludes_entrances(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    captured = []
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)
    async def generate(key, timeout, contents, config, **kwargs):
        prompt = contents[-1]
        captured.append(prompt)
        context = json.loads(prompt.split('Данные ниже — только контекст:\n')[1])
        grouped = context['location_search_context']['building_address_memberships']
        assert grouped[0]['physical_candidate_id'] == 'osm:way:4'
        assert grouped[0]['address_entry_ids'] == ['osm:node:1', 'osm:node:2']
        anchors = context['location_search_context']['address_anchors']['rows']
        assert next(row for row in anchors if row[0] == 'osm:node:1')[3] == '7A'
        contract = json.loads(config.system_instruction.split('\n', 1)[1].split('\n', 1)[0])
        assert contract['properties']['observed_candidate_ids']['items'] == {'type': 'string', 'maxLength': 100}
        assert 'первые два запроса' in prompt and 'Город обязателен в каждом запросе' in prompt
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': '', 'commons_query': '', 'article_queries': ['Model-owned hypothesis'],
            'first_wave_hypotheses': [{'kind': 'address', 'subject_id': f'osm:node:{index}',
                'query': '', 'reason': 'Plausible observed address'} for index in (1, 3)]}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    await suggest(svc, snapshot, '', [])
    assert snapshot['_identity_article_queries'] == ['Observed City Exact avenue 7A',
        'Observed City Exact avenue 13', 'Model-owned hypothesis']
    assert len(captured) == 1  # Host supplies evidence; it does not rewrite model semantic queries.


def test_source_policy_names_temporal_geographic_and_subject_checks_without_answer_seeds():
    assert 'demolished/destroyed' in IDENTITY_SOURCE_POLICY
    assert 'another locality' in IDENTITY_SOURCE_POLICY
    assert 'modern exterior' in IDENTITY_SOURCE_POLICY
    assert 'Empty selection is valid' in IDENTITY_SOURCE_POLICY
    assert 'http' not in IDENTITY_SOURCE_POLICY


def test_model_packet_retains_full_catalog_without_raw_map_payload_or_mutation():
    import copy
    candidates = []
    for index in range(193):
        candidates.append({'candidate_id': f'osm:way:{index}', 'name': 'Observed building',
            'map_address': {'city': 'Observed City', 'street': 'Full avenue', 'house_number': str(index) + 'A'},
            'map_coordinates': {'latitude': 54.123456789, 'longitude': 20.123456789},
            'map_geometry': {'lines': [[{'lat': 54, 'lon': 20}] * 1000]},
            'boundary_distance_m': index + .12, 'distance_m': index + .45,
            'footprint_bearing_interval': {'start_degrees': 282.12345, 'end_degrees': 319.9876, 'angular_span_degrees': 37.864},
            'camera_alignment': 'ahead', 'camera_direction_difference_deg': 17.234,
            'map_object': {'tags': {'building': 'yes', 'description': 'unbounded raw description ' * 1000},
                'building_entrances': {'candidate_ids': ['osm:node:1'], 'proof': 'osm_closed_way_node_membership'}}})
    story = {'_identity_observed_candidates': candidates, '_identity_search_context': {
        'nearby': candidates, 'raw_osm': 'raw map data ' * 100000}}
    original = copy.deepcopy(story)
    packet = model_identity_context(story)
    rows = packet['observed_physical_candidates']['rows']
    assert [row[0] for row in rows] == [item['candidate_id'] for item in candidates]
    assert len(json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode()) < 65536
    assert 'map_geometry' not in json.dumps(packet) and 'unbounded raw' not in json.dumps(packet)
    assert rows[-1][3] == [54.123457, 20.123457]
    assert rows[-1][5] == 192.1 and rows[-1][6] == [282.1, 320.0, 37.9]
    assert rows[-1][7] == {'status': 'ahead', 'heading_difference_degrees': 17.2}
    assert story == original  # Durable full geometry remains untouched.


def test_catalog_keeps_unknown_coordinates_and_geometry_unknown():
    catalog = compact_candidate_catalog([{'candidate_id': 'osm:way:1'}])
    assert catalog['rows'][0][2:8] == [None] * 6


def test_selector_keeps_active_and_exact_membership_geometry_with_all_address_anchors():
    pool = [*observed(), {'candidate_id': 'osm:way:999', 'map_object': {'tags': {'building': 'yes'}}}]
    story = {'_identity_observed_candidates': pool,
        'research_json': json.dumps({'visual_identity': {'candidates': [pool[-1]]}}),
        '_identity_search_context': {'nearby': [{'candidate_id': 'osm:node:777', 'name': 'Unaddressed nearby shop'}]}}
    packet = model_identity_context(story, include_observed=False)
    assert {row[0] for row in packet['observed_physical_candidates']['rows']} == {'osm:way:4', 'osm:way:999'}
    assert {row[0] for row in packet['address_anchors']['rows']} == {'osm:node:1', 'osm:node:2', 'osm:node:3'}
    assert len(packet['building_address_memberships']) == 1


def test_large_model_catalog_preserves_all_ids_without_local_refusal():
    candidates = [{'candidate_id': f'osm:way:{index}', 'name': 'Long observed name ' * 10}
        for index in range(1000)]
    packet = model_identity_context({'_identity_observed_candidates': candidates})
    assert len(json.dumps(packet).encode()) > 65536
    assert [row[0] for row in packet['observed_physical_candidates']['rows']] == [
        candidate['candidate_id'] for candidate in candidates]
    assert len(candidates) == 1000
