"""Direct image acquisition fails closed; DOM remains article metadata only."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from street_story import article_media, identity_references

D = {'image_url':'https://article.example/file-image','article_url':'https://article.example/gate',
     'kind':'article_image_link','figcaption':'Archive view'}

async def public_dns(_):
    return '93.184.216.34'

@pytest.mark.asyncio
async def test_http_failure_remains_known_transport_failure_without_browser(monkeypatch):
    async def forbidden(*args):
        raise AssertionError('No screenshot fallback')
    monkeypatch.setattr(article_media,'browser_reference',forbidden)
    candidate={'article_media':[D],'_browser_budget':{'remaining':1}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(403))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await article_media.load_article_reference(client,candidate,D['image_url'],resolver=public_dns)
    assert candidate['_browser_budget']['remaining'] == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[asyncio.CancelledError(),RuntimeError('programming failure')])
async def test_cancel_and_programming_errors_are_not_relabelled(monkeypatch,failure):
    async def fetch(*args,**kwargs):
        raise failure
    monkeypatch.setattr(article_media,'fetch_public',fetch)
    async with httpx.AsyncClient() as client:
        with pytest.raises(type(failure)):
            await article_media.load_article_reference(client,{'article_media':[D]},D['image_url'])

@pytest.mark.asyncio
async def test_browser_reference_never_creates_reference_image_from_dom():
    with pytest.raises(article_media.ArticleMediaBrowserError,match='direct_public_reference_required'):
        await article_media.browser_reference(D)

@pytest.mark.asyncio
async def test_selector_skips_unsafe_address_and_keeps_next_article_metadata(monkeypatch):
    broken={'candidate_id':'web:broken','url':D['article_url'],'reference_image_urls':['https://127.0.0.1/private.jpg']}
    good={'candidate_id':'web:next','url':D['article_url'],'reference_image_urls':[D['image_url']], 'article_media':[D]}
    async def forbidden(*args,**kwargs):
        raise AssertionError('Selection does not read pixels')
    monkeypatch.setattr(article_media,'fetch_public',forbidden)
    evidence=[]
    images=await identity_references.reference_images(SimpleNamespace(),[broken,good],limit=1,evidence=evidence)
    assert images==[('web:next','image/jpeg',D['image_url'])]
    assert evidence[0]['article_url']==D['article_url'] and evidence[0]['figcaption']=='Archive view'
    assert evidence[0]['source_url']==D['image_url']
    assert not any('sha' in key or 'cache' in key for key in evidence[0])
