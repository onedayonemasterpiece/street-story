from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from street_story.product import VibePublishBoundary
from street_story.providers import VibePublishClient
from test_backend import config


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [VibePublishClient, VibePublishBoundary])
async def test_vibepublish_loopback_uses_oauth_public_host(client_type, tmp_path) -> None:
    seen: list[tuple[str, str]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host or "", request.headers.get("host", "")))
        return httpx.Response(200, json={"destinations": [], "capabilities": []})

    settings = replace(
        config(tmp_path),
        vibepublish_base_url="http://127.0.0.1:18765",
        vibepublish_http_host="mcp-vibepublish.kenigevents.ru",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await client_type(settings, http=http).bootstrap()

    assert seen == [("127.0.0.1", "mcp-vibepublish.kenigevents.ru")]


def test_vibepublish_http_host_env_is_validated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STREET_STORY_DEVICE_TOKEN", "d" * 40)
    monkeypatch.setenv("VIBEPUBLISH_BASE_URL", "http://127.0.0.1:18765")
    monkeypatch.setenv("VIBEPUBLISH_BEARER_TOKEN", "v" * 40)
    monkeypatch.setenv("VIBEPUBLISH_HTTP_HOST", "mcp-vibepublish.kenigevents.ru")
    from street_story.config import Settings

    assert Settings.from_env().vibepublish_http_host == "mcp-vibepublish.kenigevents.ru"

    monkeypatch.setenv("VIBEPUBLISH_HTTP_HOST", "http://bad-host/")
    with pytest.raises(ValueError, match="lowercase DNS hostname"):
        Settings.from_env()
