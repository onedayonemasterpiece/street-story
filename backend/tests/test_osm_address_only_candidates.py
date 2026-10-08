from urllib.parse import parse_qs

import httpx
import pytest

from street_story.providers import OSMClient
from street_story.identity_map_context import map_entry_context
from street_story.providers import _stable_cache_key


class Cache:
    def __init__(self):
        self.values = {}
        self.reads = []

    def cache_get(self, key):
        self.reads.append(key)
        return self.values.get(key)

    def cache_put(self, key, value, ttl):
        self.values[key] = value


@pytest.mark.asyncio
async def test_address_only_entrance_survives_query_normalization_and_old_cache():
    store = Cache()
    store.values[_stable_cache_key("osm-visible-nearby-v4", [54.7, 20.5])] = {"nearby": [], "legacy": True}
    queries = []
    address = {
        "type": "node",
        "id": 900,
        "lat": 54.7001,
        "lon": 20.5,
        "tags": {"addr:street": "Тестовая улица", "addr:housenumber": "7", "entrance": "staircase"},
    }

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        query = parse_qs(request.content.decode())["data"][0]
        queries.append(query)
        if "[historic]" in query:
            return httpx.Response(200, json={"elements": []})
        return httpx.Response(200, json={"elements": [address] if '["addr:housenumber"]' in query else []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await OSMClient(store, "StreetStory test", http).lookup(54.7, 20.5)
    assert "legacy" not in result and store.reads[0].startswith("osm-visible-nearby-v6:")
    assert len(queries) == 2 and '["addr:housenumber"]' in queries[0] and "around:160" not in queries[0]
    assert '["addr:housenumber"="7"]' not in queries[0]
    item = result["nearby"][0]
    assert result["lookup_policy_version"] == OSMClient.LOOKUP_POLICY_VERSION == 6
    assert item["id"] == 900 and item["selection_bucket"] == "nearby" and item["distance_m"] < 20
    context = map_entry_context(item)
    assert context["map_address"]["house_number"] == "7"
    assert context["map_address"]["provenance"] == "osm.tags"
    assert context["map_address"]["scope"] == "mapped_entry_only"
    assert context["map_coordinates"]["source_url"] == "https://www.openstreetmap.org/node/900"


@pytest.mark.asyncio
async def test_address_points_share_existing_twenty_nearest_slots():
    nodes = [{"type": "node", "id": n, "lat": 54.7 + n * 0.00001, "lon": 20.5, "tags": {"name": f"Nearby{n}"}} for n in range(1, 26)]
    addresses = [
        {
            "type": "node",
            "id": 100 + n,
            "lat": 54.7 + n * 0.00001,
            "lon": 20.5,
            "tags": {"addr:street": "Тестовая улица", "addr:housenumber": str(n)},
        }
        for n in (3, 8)
    ]

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        query = parse_qs(request.content.decode())["data"][0]
        return httpx.Response(200, json={"elements": [] if "[historic]" in query else nodes + addresses})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await OSMClient(Cache(), "StreetStory test", http).lookup(54.7, 20.5)
    assert len(result["nearby"]) == 20
    assert {103, 108}.issubset({item["id"] for item in result["nearby"]})
    assert [item["distance_m"] for item in result["nearby"]] == sorted(item["distance_m"] for item in result["nearby"])
