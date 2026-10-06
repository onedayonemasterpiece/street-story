import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from playwright.async_api import Error as BrowserError

from street_story import article_media, identity_references
from test_reference_image_codec import jpeg


D = {'image_url': 'https://article.example/file-image',
     'article_url': 'https://article.example/gate', 'kind': 'article_image_link'}


async def public_dns(_):
    return '93.184.216.34'


@pytest.mark.asyncio
async def test_browser_dom_failure_becomes_known_reference_failure(monkeypatch):
    async def detached(_):
        raise BrowserError('Locator.evaluate: detached element with private page text')
    monkeypatch.setattr(article_media, 'browser_reference', detached)
    candidate = {'article_media': [D], '_browser_budget': {'remaining': 1}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(article_media.ArticleMediaBrowserError, match='^article_media_browser_unavailable$') as err:
            await article_media.load_article_reference(client, candidate, D['image_url'], resolver=public_dns)
    assert candidate['_browser_budget']['remaining'] == 0
    assert 'private' not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [asyncio.CancelledError(), RuntimeError('programming failure')])
async def test_cancel_and_programming_errors_are_not_relabelled(monkeypatch, failure):
    async def browser(_):
        raise failure
    monkeypatch.setattr(article_media, 'browser_reference', browser)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(type(failure)):
            await article_media.load_article_reference(client, {'article_media': [D], '_browser_budget': {'remaining': 1}}, D['image_url'], resolver=public_dns)


@pytest.mark.asyncio
async def test_bad_decode_can_advance_to_another_authorized_matching_element(monkeypatch):
    class Image:
        def __init__(self, good):
            self.good = good
            self.screenshots = 0
        async def evaluate(self, expression, *args):
            if args:
                return args[0] == D['image_url']
            if 'decode()' in expression:
                if not self.good and 'catch' not in expression:
                    raise BrowserError('EncodingError: The source image cannot be decoded.')
                return self.good
            return self.good
        async def scroll_into_view_if_needed(self, **_):
            pass
        async def screenshot(self, **_):
            self.screenshots += 1
            return jpeg((320, 240))
    bad, good = Image(False), Image(True)
    class Images:
        async def count(self):
            return 2
        def nth(self, index):
            return [bad, good][index]
    @asynccontextmanager
    async def browser(_):
        yield SimpleNamespace(locator=lambda _: Images())
    async def media(*_, **__):
        return 'Gate', [D]
    monkeypatch.setattr(article_media, 'article_browser', browser)
    monkeypatch.setattr(article_media, 'rendered_media', media)
    data = await article_media.browser_reference(D)
    assert data and bad.screenshots == 0 and good.screenshots == 1


@pytest.mark.asyncio
async def test_common_reference_loader_skips_broken_browser_then_keeps_next_provenance(monkeypatch):
    broken = {'candidate_id': 'web:broken', 'discovery': 'web_article_media',
              'reference_image_urls': [D['image_url']], 'article_media': [D], '_browser_budget': {'remaining': 1}}
    good_d = {**D, 'image_url': 'https://article.example/good.jpg'}
    good = {**broken, 'candidate_id': 'web:next', 'reference_image_urls': [good_d['image_url']],
            'article_media': [good_d]}
    async def fetch(_client, raw, _maximum, **_):
        if raw == D['image_url']:
            return raw, 'text/html', b'<html>file page</html>'
        return raw, 'image/jpeg', jpeg((320, 240))
    async def decode(_):
        raise BrowserError('EncodingError: The source image cannot be decoded.')
    monkeypatch.setattr(article_media, 'fetch_public', fetch)
    monkeypatch.setattr(article_media, 'browser_reference', decode)
    events = []
    monkeypatch.setattr(identity_references, 'record_identity_event', lambda _s, _sid, event, fields: events.append((event, fields)))
    evidence = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        images = await identity_references.reference_images(SimpleNamespace(), [broken, good], limit=1,
            story_id='story_fixture', http=client, evidence=evidence)
    assert [image[0] for image in images] == ['web:next']
    assert len(evidence) == 1
    assert evidence[0]['candidate_id'] == 'web:next'
    assert evidence[0]['article_url'] == D['article_url']
    assert evidence[0]['source_url'] == good_d['image_url']
    assert evidence[0]['retrieval_method'] == 'http'
    assert evidence[0]['model_image_sha256']
    assert ('identity_reference_unavailable', {'candidate_id': 'web:broken', 'reason': 'ArticleMediaBrowserError'}) in events
