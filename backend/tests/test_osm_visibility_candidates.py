from __future__ import annotations

import httpx
import pytest
from urllib.parse import parse_qs

from street_story.db import Store
from street_story.providers import OSMClient
from street_story.errors import RetryableProviderError


@pytest.mark.asyncio
async def test_osm_lookup_uses_visibility_radius_and_orders_nearby_candidates(tmp_path):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "osm_type": "way",
                    "osm_id": 1,
                    "display_name": "Точка съёмки",
                    "lat": "54.7000",
                    "lon": "20.5000",
                    "address": {"road": "Тестовая улица"},
                },
            )
        body = parse_qs(request.content.decode("utf-8"))["data"][0]
        if "[historic]" in body:
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "type": "way",
                            "id": 300,
                            "center": {"lat": 54.7035, "lon": 20.5000},
                            "tags": {"name": "Дальний памятник", "historic": "monument"},
                        },
                        {
                            "type": "node",
                            "id": 100,
                            "lat": 54.7004,
                            "lon": 20.5000,
                            "tags": {"name": "Ближние ворота", "historic": "city_gate"},
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 100,
                        "lat": 54.7004,
                        "lon": 20.5000,
                        "tags": {"name": "Ближние ворота", "historic": "city_gate"},
                    },
                    {
                        "type": "way",
                        "id": 200,
                        "center": {"lat": 54.7014, "lon": 20.5000},
                        "tags": {"name": "Средний объект"},
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    osm = OSMClient(Store(tmp_path / "street-story.sqlite3"), "Street Story test", client)
    try:
        result = await osm.lookup(54.7000, 20.5000)
    finally:
        await client.aclose()

    assert result["radius_m"] == 600
    ids = [item["id"] for item in result["nearby"]]
    assert ids[:2] == [100, 300]
    assert 200 in ids
    assert result["candidate_pool_counts"] == {"landmark": 2, "nearby": 2}
    nearby_body = parse_qs([r for r in requests if r.method == "POST"][0].content.decode("utf-8"))["data"][0]
    landmark_body = parse_qs([r for r in requests if r.method == "POST"][1].content.decode("utf-8"))["data"][0]
    assert "around:600" in landmark_body
    assert "[historic]" in landmark_body
    assert "[name]" in nearby_body

@pytest.mark.asyncio
async def test_dense_city_noise_does_not_displace_farther_historic_landmark(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "osm_type": "way",
                    "osm_id": 1,
                    "display_name": "Точка съёмки",
                    "lat": "54.7000",
                    "lon": "20.5000",
                    "address": {"road": "Тестовая улица"},
                },
            )
        body = parse_qs(request.content.decode("utf-8"))["data"][0]
        if "[historic]" in body:
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "type": "way",
                            "id": 9999,
                            "center": {"lat": 54.7030, "lon": 20.5000},
                            "tags": {"name": "Исторические ворота", "historic": "city_gate"},
                        }
                    ]
                },
            )
        noise = [
            {
                "type": "node",
                "id": index,
                "lat": 54.7000 + index * 0.000002,
                "lon": 20.5000,
                "tags": {"name": f"Шумовой POI {index}"},
            }
            for index in range(1, 121)
        ]
        return httpx.Response(200, json={"elements": noise})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    osm = OSMClient(Store(tmp_path / "street-story.sqlite3"), "Street Story dense test", client)
    try:
        result = await osm.lookup(54.7000, 20.5000)
    finally:
        await client.aclose()

    ids = [item["id"] for item in result["nearby"]]
    assert 9999 in ids
    landmark = next(item for item in result["nearby"] if item["id"] == 9999)
    assert landmark["selection_bucket"] == "landmark"
    assert len(ids) <= 32


def reverse_response():
    return httpx.Response(200, json={"osm_type": "way", "osm_id": 1, "lat": "54.7000", "lon": "20.5000"})


def object_response(object_id):
    return httpx.Response(200, json={"elements": [{
        "type": "node", "id": object_id, "lat": 54.7004, "lon": 20.5000,
        "tags": {"name": "Объект", "historic": "city_gate"},
    }]})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["504", "timeout", "remark", "malformed"])
async def test_overpass_failure_uses_spare_and_keeps_it_for_next_query(tmp_path, caplog, failure):
    calls = []

    async def handler(request):
        if request.method == "GET":
            return reverse_response()
        calls.append(request)
        if request.url.host == "overpass-api.de":
            if failure == "timeout":
                raise httpx.ReadTimeout("test timeout", request=request)
            if failure == "remark":
                return httpx.Response(200, json={"elements": [], "remark": "runtime error: timeout"})
            if failure == "malformed":
                return httpx.Response(200, json={"unexpected": []})
            return httpx.Response(504)
        query = parse_qs(request.content.decode())["data"][0]
        return object_response(100 if "[historic]" in query else 200)

    caplog.set_level("INFO", logger="uvicorn.error")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / "db.sqlite3"), "StreetStory tests", client)
        result = await osm.lookup(54.7000, 20.5000)
        assert await osm.lookup(54.7000, 20.5000) == result
    assert [request.url.host for request in calls] == ["overpass-api.de", "maps.mail.ru", "maps.mail.ru"]
    assert [item["id"] for item in result["nearby"]] == [100, 200]
    assert not result["partial"]
    assert '"outcome": "failed"' in caplog.text and '"outcome": "success"' in caplog.text
    assert "54.7" not in caplog.text and "20.5" not in caplog.text


@pytest.mark.asyncio
async def test_failed_nearby_query_preserves_landmarks_and_does_not_cache_partial(tmp_path):
    calls = []
    fail_nearby = True

    async def handler(request):
        calls.append(request)
        if request.method == "GET":
            return reverse_response()
        query = parse_qs(request.content.decode())["data"][0]
        if "[historic]" in query:
            return object_response(100)
        return httpx.Response(504) if fail_nearby else object_response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / "db.sqlite3"), "StreetStory tests", client)
        partial = await osm.lookup(54.7000, 20.5000)
        assert partial["partial"] and partial["unavailable_buckets"] == ["nearby"]
        assert [item["id"] for item in partial["nearby"]] == [100]
        fail_nearby = False
        complete = await osm.lookup(54.7000, 20.5000)
        assert not complete["partial"]
        assert [item["id"] for item in complete["nearby"]] == [100, 200]
        completed_call_count = len(calls)
        assert await osm.lookup(54.7000, 20.5000) == complete
        assert len(calls) == completed_call_count


@pytest.mark.asyncio
async def test_all_overpass_routes_fail_bounded_without_empty_success(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request)
        return reverse_response() if request.method == "GET" else httpx.Response(504)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / "db.sqlite3"), "StreetStory tests", client)
        with pytest.raises(RetryableProviderError):
            await osm.lookup(54.7000, 20.5000)
    assert len([request for request in calls if request.method == 'POST']) == 4
    assert len([request for request in calls if request.url.path.endswith('/map.json')]) == 1
    # An optional parallel reverse may remain unsent when every map route fails synchronously.
    assert len([request for request in calls if request.url.path.endswith('/reverse')]) <= 1
    assert sum(r.url.path == '/api/0.6/map.json' for r in calls) == 1


@pytest.mark.asyncio
async def test_rate_limited_host_is_not_retried_for_other_bucket(tmp_path):
    calls = []

    async def handler(request):
        if request.method == "GET":
            return reverse_response()
        calls.append(request.url.host)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / "db.sqlite3"), "StreetStory tests", client)
        with pytest.raises(RetryableProviderError):
            await osm.lookup(54.7000, 20.5000)
    assert calls == ["overpass-api.de", "maps.mail.ru"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "timeout", "malformed"])
async def test_reverse_failure_does_not_block_objects_and_recovers_without_sticky_cache(tmp_path, failure):
    reverse_down = True
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == "GET":
            if reverse_down:
                if failure == "timeout":
                    raise httpx.ReadTimeout("reverse offline", request=request)
                return httpx.Response(503) if failure == "503" else httpx.Response(200, json=[])
            return reverse_response()
        return object_response(100)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / "db.sqlite3"), "StreetStory tests", client)
        partial = await osm.lookup(54.7000, 20.5000)
        assert [item["id"] for item in partial["nearby"]] == [100]
        assert partial["partial"] and partial["unavailable_buckets"] == ["reverse"]
        assert len(calls) == 4
        reverse_down = False
        complete = await osm.lookup(54.7000, 20.5000)
        assert not complete["partial"] and complete["reverse"]["osm_id"] == 1
        assert len(calls) == 8

@pytest.mark.asyncio
async def test_local_map_api_recovers_addresses_and_real_geometry_after_overpass_failure(tmp_path):
    map_calls = []

    async def handler(request):
        if request.method == 'POST':
            query = parse_qs(request.content.decode())['data'][0]
            return object_response(100) if '[historic]' in query else httpx.Response(504)
        if request.url.path != '/api/0.6/map.json':
            return reverse_response()
        map_calls.append(request)
        return httpx.Response(200, json={'elements': [
            {'type': 'node', 'id': 1, 'lat': 54.7001, 'lon': 20.5},
            {'type': 'node', 'id': 2, 'lat': 54.7002, 'lon': 20.5001},
            {'type': 'node', 'id': 3, 'lat': 54.7001, 'lon': 20.5,
             'tags': {'addr:street': 'Local street', 'addr:housenumber': '17'}},
            {'type': 'node', 'id': 4, 'lat': 54.7014, 'lon': 20.5024,
             'tags': {'addr:housenumber': 'outside-circle'}},
            {'type': 'way', 'id': 5, 'nodes': [1, 2, 1], 'tags': {'building': 'yes'}},
            {'type': 'relation', 'id': 6, 'members': [{'type': 'way', 'ref': 99}],
             'tags': {'building': 'yes'}},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        osm = OSMClient(Store(tmp_path / 'db.sqlite3'), 'StreetStory tests', client)
        result = await osm.lookup(54.7, 20.5)
        assert await osm.lookup(54.7, 20.5) == result
    assert not result['partial'] and len(map_calls) == 1
    objects = {x['id']: x for x in result['nearby']}
    assert objects[3]['tags']['addr:housenumber'] == '17'
    assert objects[5]['center'] == {'lat': 54.70015, 'lon': 20.50005}
    assert 4 not in objects and 6 not in objects
    assert objects[5]['distance_m'] < 30
