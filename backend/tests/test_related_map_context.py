"""Related organizations survive map selection without becoming building identity."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.fact_agent_providers import AgentRoute, run_tool_agent
from street_story.fact_agents import native_agent_context
from street_story.place_selection import confirm_place, place_map
from test_identity_lifecycle import create, make_service
from test_owner_map_selection import MapOSM, command


@pytest.mark.asyncio
async def test_related_organization_without_city_reaches_frozen_input_with_own_provenance(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)

    class Organizations(MapOSM):
        async def _map_nearby(self, client, lat, lon, radius):
            raw = await super()._map_nearby(client, lat, lon, radius)
            raw['map_observation'] = {'source_url': 'https://api.openstreetmap.org/api/0.6/map.json?bbox=fixture',
                'observed_at': service.store.now(), 'response_sha256': 'a' * 64, 'http_status': 200}
            raw['elements'] += [
                {'type': 'node', 'id': 11, 'lat': lat+.00007, 'lon': lon+.00002,
                 'version': 7, 'timestamp': '2026-05-23T12:00:00Z', 'changeset': 123,
                 'tags': {'name': 'Mapped restaurant', 'amenity': 'restaurant',
                          'website': 'https://example.org/menu', 'wikipedia': 'ru:Example',
                          'wikidata': 'Q123', 'check_date': '2026-05-23'}},
                {'type': 'node', 'id': 12, 'lat': lat+.001, 'lon': lon+.001,
                 'tags': {'name': 'Former shop', 'disused:shop': 'books',
                          'addr:street': 'Street', 'addr:housenumber': '1'}},
                {'type': 'node', 'id': 13, 'lat': lat+.001, 'lon': lon+.001,
                 'tags': {'name': 'Unrelated office', 'office': 'company'}}]
            return raw

    service.providers.osm = Organizations()
    snapshot = await place_map(service, story['id'])
    # No addr:city on the organization: this used to drop out of every bucket.
    assert {c['candidate_id'] for c in snapshot['organization_objects']} == {'osm:node:11', 'osm:node:12', 'osm:node:13'}
    result = confirm_place(service, story['id'], 'selected-house', command(story, snapshot, 'osm:way:1', 'selected-house'))
    identity = result['visual_identity']
    assert identity['candidate_id'] == 'osm:way:1'
    with service.store.connection() as db:
        row = dict(service._story_row(db, story['id']))
    row['_identity_generation'] = result['identity_generation']
    context = native_agent_context(service, row, identity, 'Research this house', 'component-run')
    related = context['related_map_observations']
    assert [c['candidate_id'] for c in related] == ['osm:node:11', 'osm:node:12']
    assert related[1]['relationship']['basis'] == ['matching_observed_address']
    assert related[1]['map_object']['tags']['disused:shop'] == 'books'
    restaurant = related[0]
    assert restaurant['type'] == 'mapped_organization'
    assert restaurant['relationship']['basis'] == ['observed_point_inside_building_polygon']
    assert restaurant['relationship']['point_kind'] == 'osm_node'
    assert restaurant['osm_observation']['version'] == 7
    assert restaurant['osm_observation']['tags']['check_date'] == '2026-05-23'
    assert restaurant['map_object']['tags']['wikipedia'] == 'ru:Example'
    assert restaurant['map_object']['tags']['website'] == 'https://example.org/menu'
    source = context['retained_map_sources'][0]
    assert source['response_sha256'] == 'a' * 64
    assert json.loads(source['normalized_text'])['related_objects'][0] == restaurant['osm_observation']
    assert service.story(story['id'])['visual_identity']['candidate_id'] == 'osm:way:1'
    assert context['related_map_context_status'] == 'observed'


@pytest.mark.asyncio
async def test_map_webfetch_delivers_owned_chunks_without_another_network_read(tmp_path):
    from test_native_fact_basis import setup

    service, job, reader, _, agent, original, _ = await setup(tmp_path)
    url = 'https://api.openstreetmap.org/api/0.6/map.json?bbox=owned'
    text = json.dumps({'node': {'type': 'node', 'id': 11, 'tags': {'amenity': 'restaurant'}}})
    context = {'run_id': 'headless-run', 'selected_object': {'candidate_id': 'wiki:77'},
        'retained_map_sources': [{'source_url': url, 'selected_candidate_id': 'wiki:77',
            'http_status': 200, 'normalized_text': text, 'response_sha256': hashlib.sha256(b'actual map response').hexdigest()}]}
    original_context = copy.deepcopy(context)
    reads = []

    async def forbidden(*_args, **_kwargs):
        raise AssertionError('Already acquired map bytes must not be fetched again')

    service.providers.gemini._fetch_page_documents = forbidden

    class Native:
        async def run(self, **kwargs):
            first = await kwargs['callback']('webfetch', {'url': url})
            second = await kwargs['callback']('webfetch', {'url': url})
            assert second == first
            document = first['documents'][0]
            assert document['source_version_id'] and document['content_sha256']
            assert ''.join(chunk['text'] for chunk in document['chunks']) == '[core]\n' + text
            reads.append(document)
            return {'receipt': kwargs['receipt'], 'text': json.dumps({'facts': [], 'completion': {
                'research_sufficient': False, 'reason': 'Read component only; no semantic assessment.',
                'remaining_gaps': ['Model assessment'], 'stop_basis': 'unresolved_gap', 'next_action': None, 'strongest_candidate': None}})}

    route = AgentRoute('codex_native', 'gpt-6-luna', 'native', Native())
    adapter = SimpleNamespace(service=service, _checkpoint_receipt=lambda *_: None)
    try:
        await run_tool_agent(adapter, route, {'id': job['story_id']}, context,
            binding={**original['binding'], 'attempt_id': agent['attempt_id']}, guard=lambda: None)
        assert len(reads) == 1 and context == original_context
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM research_run_sources WHERE run_id=? AND source_version_id=?',
                ('headless-run', reads[0]['source_version_id'])).fetchone()[0] == 1
    finally:
        await reader.search_http.aclose()
