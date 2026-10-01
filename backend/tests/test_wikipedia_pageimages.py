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
        if calls == 1:
            return httpx.Response(
                200,
                json={"query": {"geosearch": [{"pageid": 77, "title": "Тестовые ворота"}]}},
            )
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "pageid": 77,
                            "title": "Тестовые ворота",
                            "extract": "Описание.",
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

    assert calls == 2
    assert pages[0]["image_url"].startswith("https://upload.wikimedia.org/")
    assert pages[0]["thumbnail_url"].startswith("https://upload.wikimedia.org/")
