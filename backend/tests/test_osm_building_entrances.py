import httpx
import pytest

from street_story.db import Store
from street_story.providers import OSMClient
from street_story.identity_map_context import map_entry_context
from street_story.identity_subject_binding import subject_aliases


@pytest.mark.asyncio
async def test_map_topology_groups_two_entrances_without_tenant_or_adjacent_building(tmp_path):
    nodes = [
        {'type': 'node', 'id': 1, 'lat': 54.7, 'lon': 20.5, 'tags': {'entrance': 'staircase', 'addr:housenumber': '7'}},
        {'type': 'node', 'id': 2, 'lat': 54.7001, 'lon': 20.5, 'tags': {'entrance': 'staircase', 'addr:housenumber': '9'}},
        {'type': 'node', 'id': 3, 'lat': 54.7001, 'lon': 20.5001},
        {'type': 'node', 'id': 4, 'lat': 54.7, 'lon': 20.5001, 'tags': {'entrance': 'yes', 'amenity': 'restaurant', 'name': 'Tenant'}},
        {'type': 'node', 'id': 5, 'lat': 54.7, 'lon': 20.5002, 'tags': {'entrance': 'yes', 'addr:housenumber': '11'}},
    ]
    building = {'type': 'way', 'id': 10, 'nodes': [1, 2, 3, 4, 1], 'tags': {'building': 'yes'}}
    requests = []
    async def handle(request):
        requests.append(request)
        if request.url.host == 'nominatim.openstreetmap.org':
            return httpx.Response(200, json={})
        if request.url.host == 'api.openstreetmap.org':
            return httpx.Response(200, json={'elements': [*nodes, building]})
        return httpx.Response(200, json={'elements': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        found = await OSMClient(Store(tmp_path / 'db.sqlite3'), 'test', http).lookup(54.7, 20.5)
    candidates = [{**map_entry_context(item), 'url': f"https://www.openstreetmap.org/{item['type']}/{item['id']}"}
                  for item in found['nearby']]
    grouped = subject_aliases(candidates)
    assert grouped['osm:way:10'] == {'osm:way:10', 'osm:node:1', 'osm:node:2'}
    assert grouped['osm:node:4'] == {'osm:node:4'}
    assert grouped['osm:node:5'] == {'osm:node:5'}
    assert len([r for r in requests if r.method == 'POST']) == 1  # Landmark query only.
    from street_story.mvp_research import MvpResearchStreetStoryService
    catalog = MvpResearchStreetStoryService._candidate_catalog(found, [])
    assert subject_aliases(catalog)['osm:way:10'] == {'osm:way:10', 'osm:node:1', 'osm:node:2'}
    # An address or nearby position without the original way membership cannot group objects.
    for candidate in candidates:
        (candidate.get('map_object') or {}).pop('building_entrances', None)
    assert subject_aliases(candidates)['osm:way:10'] == {'osm:way:10'}


def test_shared_or_unproved_entrance_membership_does_not_merge_buildings():
    def way(value, proof='osm_closed_way_node_membership'):
        url = f'https://www.openstreetmap.org/way/{value}'
        return {'candidate_id': f'osm:way:{value}', 'url': url,
                'map_object': {'provenance': 'osm.tags', 'source_url': url, 'tags': {'building': 'yes'},
                    'building_entrances': {'proof': proof, 'source_url': url, 'candidate_ids': ['osm:node:1']}}}
    entrance = {'candidate_id': 'osm:node:1', 'map_object': {'provenance': 'osm.tags', 'tags': {'entrance': 'yes'}}}
    assert subject_aliases([way(10), way(11), entrance])['osm:node:1'] == {'osm:node:1'}
    assert subject_aliases([way(10, 'model_claim'), entrance])['osm:node:1'] == {'osm:node:1'}
