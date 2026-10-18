from __future__ import annotations

import httpx
import pytest

from street_story.db import Store
from street_story.providers import WikipediaClient


@pytest.mark.asyncio
async def test_nearby_exposes_wikimedia_reference_image_urls(tmp_path):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "pageid": 77,
                            "title": "Тестовые ворота",
                            "extract": "Описание.",
                            "coordinates": [{"lat":54.7005,"lon":20.5,"primary":True,"globe":"earth"}],
                            "pageprops": {"wikibase_item":"Q77"},
                            "fullurl": "https://ru.wikipedia.org/wiki/Test",
                            "original": {
                                "source": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Test.jpg"
                            },
                            "thumbnail": {
                                "source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.jpg/640px-Test.jpg"
                            },
                        }
                    ]
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    wikipedia = WikipediaClient(Store(tmp_path / "street-story.sqlite3"), client)
    try:
        pages = await wikipedia.nearby(54.7, 20.5)
    finally:
        await client.aclose()

    assert calls == 1
    assert pages[0]["image_url"].startswith("https://upload.wikimedia.org/")
    assert pages[0]["thumbnail_url"].startswith("https://upload.wikimedia.org/")
    assert pages[0]["distance_m"] == pytest.approx(55.6, abs=0.1)
    assert pages[0]["wikidata_id"] == "Q77"
    assert pages[0]["metadata_only"] is True
    assert pages[0]["lat"] == 54.7005
    assert pages[0]["lon"] == 20.5


@pytest.mark.asyncio
async def test_metadata_batch_is_bounded_and_coordinates_are_never_capture_position(tmp_path):
    calls = []
    async def handler(request):
        calls.append(request)
        assert request.url.params['generator'] == 'geosearch'
        assert request.url.params['prop'] == 'extracts|info|pageimages|pageprops|coordinates'
        assert request.url.params['ggslimit'] == '20'
        assert request.url.params['exchars'] == '700'
        assert request.url.params['ppprop'] == 'wikibase_item'
        return httpx.Response(200, json={'query': {'pages': [
            {'pageid': i + 1, 'title': str(i), 'extract': 'x' * 900,
             **({'coordinates': [{'lat': 54.701, 'lon': 20.5, 'primary': True}]} if i == 0 else {})}
            for i in range(23)]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wiki = WikipediaClient(Store(tmp_path / 'wiki.sqlite3'), client)
        first = await wiki.nearby(54.7, 20.5)
        cached = await wiki.nearby(54.7000001, 20.5)
    assert len(calls) == 1 and len(first) == 20
    assert all(len(page['extract']) == 700 for page in first)
    assert first[0]['distance_m'] != cached[0]['distance_m']
    assert first[0]['lat'] == 54.701
    assert all(page['lat'] is None and page['distance_m'] is None for page in first[1:])


@pytest.mark.asyncio
async def test_retry_after_prevents_repeated_metadata_get_until_due(tmp_path):
    from street_story.errors import RetryableProviderError
    store = Store(tmp_path / 'wiki.sqlite3')
    clock = [1000.]
    store.now = lambda: clock[0]
    calls = []
    def handler(request):
        calls.append(request)
        return (httpx.Response(429, headers={'Retry-After': '30'}) if len(calls) == 1 else
            httpx.Response(200, json={'query': {'pages': []}}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wiki = WikipediaClient(store, client)
        with pytest.raises(RetryableProviderError) as initial:
            await wiki.nearby(54.7, 20.5)
        assert initial.value.retry_at == 1030.
        clock[0] = 1010.
        with pytest.raises(RetryableProviderError) as paused:
            await wiki.nearby(54.701, 20.5)
        assert paused.value.retry_at == 1030. and len(calls) == 1
        clock[0] = 1031.
        assert await wiki.nearby(54.7, 20.5) == []
        assert await wiki.nearby(54.7, 20.5) == []
    assert len(calls) == 2
