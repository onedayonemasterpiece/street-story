from __future__ import annotations

import httpx
import pytest

from street_story.db import Store
from street_story.providers import OSMClient


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
        body = request.content.decode("utf-8")
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
    landmark_body = requests[1].content.decode("utf-8")
    nearby_body = requests[2].content.decode("utf-8")
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
        body = request.content.decode("utf-8")
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
