from __future__ import annotations

import httpx
import pytest

from street_story.db import Store
from street_story.providers import WIKIPEDIA_USER_AGENT, WikipediaClient


@pytest.mark.asyncio
async def test_wikipedia_requests_have_contact_user_agent(tmp_path):
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json={"query": {"geosearch": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    wiki = WikipediaClient(Store(tmp_path / "street-story.sqlite3"), client)
    try:
        assert await wiki.nearby(54.697111, 20.494111) == []
    finally:
        await client.aclose()

    assert seen == [WIKIPEDIA_USER_AGENT]
    assert "bot" in WIKIPEDIA_USER_AGENT.lower()
    assert "https://github.com/onedayonemasterpiece/street-story" in WIKIPEDIA_USER_AGENT
    assert "nearby research" in WIKIPEDIA_USER_AGENT
