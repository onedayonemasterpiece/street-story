"""Reserved article fixtures keep the production network security boundary."""
import socket

import httpx
import pytest

from street_story import article_media
from street_story.db import Store


@pytest.mark.asyncio
async def test_reserved_mock_article_is_pinned_with_original_host(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='Frozen test article.')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await article_media.cached_public_page(Store(tmp_path / 'store.sqlite3'), client, 'https://archive.example/article')
    assert result == ('https://archive.example/article', 'text/plain', b'Frozen test article.')
    assert str(requests[0].url) == 'https://93.184.216.34/article'
    assert requests[0].headers['host'] == 'archive.example'
    assert requests[0].extensions['sni_hostname'] == 'archive.example'


@pytest.mark.asyncio
async def test_explicit_private_resolver_still_rejects_reserved_mock_article(tmp_path):
    requests = []

    async def private(host):
        return '127.0.0.1'

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request))) as client:
        with pytest.raises(ValueError, match='private_address'):
            await article_media.cached_public_page(Store(tmp_path / 'store.sqlite3'), client, 'https://archive.example/article', resolver=private)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mock,host', [(False, 'archive.example'), (True, 'public.example.com')])
async def test_fixture_keeps_dns_validation_for_real_transport_and_nonreserved_hosts(tmp_path, monkeypatch, mock, host):
    import asyncio

    resolved = []

    async def private_dns(host, port, **kwargs):
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', private_dns)
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request)) if mock else httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match='private_address'):
            await article_media.cached_public_page(Store(tmp_path / 'store.sqlite3'), client, 'https://' + host + '/article')
    assert resolved == [host]
    assert requests == []
