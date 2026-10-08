"""Address hints retain literal map provenance without becoming building proof."""
import json
from types import SimpleNamespace

import pytest

from street_story.identity_discovery import _map_query_context, suggest
from street_story.identity_source_selection import IDENTITY_SOURCE_POLICY, observed_address_context
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
        prompt = contents[1]
        captured.append(prompt)
        context = json.loads(prompt.split('Данные ниже — только контекст:\n')[1])
        grouped = context['location_search_context']['observed_address_context']['building_address_memberships']
        assert grouped[0]['physical_candidate_id'] == 'osm:way:4'
        assert grouped[0]['address_entries'][0]['address']['house_number'] == '7A'
        assert config.response_json_schema['properties']['observed_candidate_ids']['items']['enum'] == ['osm:way:4']
        assert 'первые два запроса' in prompt and 'Город обязателен в каждом запросе' in prompt
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': '', 'commons_query': '', 'article_queries': ['Model-owned hypothesis']}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    await suggest(svc, snapshot, '', [])
    assert snapshot['_identity_article_queries'] == ['Model-owned hypothesis']
    assert len(captured) == 1  # Host supplies evidence; it does not rewrite model semantic queries.


def test_source_policy_names_temporal_geographic_and_subject_checks_without_answer_seeds():
    assert 'demolished/destroyed' in IDENTITY_SOURCE_POLICY
    assert 'another locality' in IDENTITY_SOURCE_POLICY
    assert 'modern exterior' in IDENTITY_SOURCE_POLICY
    assert 'Empty selection is valid' in IDENTITY_SOURCE_POLICY
    assert 'http' not in IDENTITY_SOURCE_POLICY
