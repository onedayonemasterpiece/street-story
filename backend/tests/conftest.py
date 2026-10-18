"""Network fixture support without changing public-acquisition security checks."""
from functools import wraps

import httpx
import pytest

from street_story import article_media


@pytest.fixture(autouse=True)
def reserved_example_dns_for_mock_articles(monkeypatch):
    """Resolve reserved .example fixture hosts only behind MockTransport.

    Explicit resolver arguments remain authoritative, including private-DNS
    regressions. Real transports and ordinary public hosts use production DNS.
    Requests still pass through the production address/redirect checks and pin
    the fixture address while preserving Host and TLS SNI.
    """
    original = article_media.cached_public_page
    original_resolver = article_media.resolve_public

    async def fixture_resolver(host):
        if host.endswith('.example'):
            return '93.184.216.34'
        return await original_resolver(host)

    @wraps(original)
    async def cached_page(store, client, raw, **kwargs):
        if 'resolver' not in kwargs and isinstance(getattr(client, '_transport', None), httpx.MockTransport):
            kwargs['resolver'] = fixture_resolver
        return await original(store, client, raw, **kwargs)

    monkeypatch.setattr(article_media, 'cached_public_page', cached_page)
