"""Article identity must survive a Wikimedia derivative and the shared pixel cache."""
import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from street_story.identity_lifecycle import visual_match
from street_story.identity_references import reference_images, thumbnail_reference
from street_story.identity_subject_binding import bind_reference_subject
from test_reference_image_codec import jpeg


ARTICLE = 'https://de.wikipedia.org/wiki/Example_gate'
ORIGINAL = 'https://upload.wikimedia.org/wikipedia/commons/e/e0/Example_gate.jpg'
THUMBNAIL = thumbnail_reference(ORIGINAL)
PHYSICAL = {'candidate_id': 'osm:way:123', 'name': 'Physical gate'}


def candidate(article=ARTICLE, **descriptor):
    return {'candidate_id': 'web:illustrated-article', 'url': article,
            'discovery': 'wikipedia_article_media', 'reference_image_urls': [ORIGINAL],
            'article_media': [{'article_url': article, 'image_url': ORIGINAL,
                               'kind': 'img', **descriptor}]}


def completed_verdict(**values):
    return {'status': 'match', 'candidate_id': 'web:illustrated-article', 'confidence': .91,
            'reference_subject_candidate_id': PHYSICAL['candidate_id'],
            'observations': ['Same arch and distinctive brick openings.'],
            'alternative_candidate_ids': [], '_references_sent': ['web:illustrated-article'], **values}


def cached_article(body=b'<html>Immutable article illustration</html>', **values):
    return {'final_url': ARTICLE, 'body': base64.b64encode(body).decode(),
            'sha256': hashlib.sha256(body).hexdigest(), **values}


@pytest.mark.asyncio
async def test_wikipedia_derivative_retains_immutable_article_and_reference_binding():
    body = b'<html>Immutable article illustration</html>'
    article = cached_article(body)
    keys, requests, evidence = [], [], []

    def cache_get(key):
        keys.append(key)
        return article

    async def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    service = SimpleNamespace(store=SimpleNamespace(cache_get=cache_get))
    selected = candidate(candidate_id='spoofed', source_url='https://evil.invalid/image',
                         model_image_sha256='f' * 64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        images = await reference_images(service, [selected], http=http, evidence=evidence)
    assert requests == [THUMBNAIL]
    receipt = evidence[0]
    assert receipt['candidate_id'] == selected['candidate_id']
    assert receipt['source_url'] == ORIGINAL
    assert receipt['article_url'] == ARTICLE
    assert receipt['requested_image_url'] == receipt['resolved_image_url'] == THUMBNAIL
    assert receipt['model_image_sha256'] == hashlib.sha256(images[0][2]).hexdigest()
    assert receipt['model_image_sha256'] != 'f' * 64
    assert receipt['model_image_bytes'] == len(images[0][2])
    assert receipt['article_source_sha256'] == hashlib.sha256(body).hexdigest()
    assert keys == ['public-article-acquisition-v1:' + hashlib.sha256(ARTICLE.encode()).hexdigest()]
    bound = bind_reference_subject(completed_verdict(), [selected], [PHYSICAL], evidence)
    assert bound['status'] == 'bound'
    assert visual_match(bound['result'], [selected], [PHYSICAL])
    competitor = {'candidate_id': 'osm:way:456', 'name': 'Other gate'}
    conflicting = bind_reference_subject(completed_verdict(alternative_candidate_ids=['osm:way:456']),
                                         [selected], [PHYSICAL, competitor], evidence)
    assert not visual_match(conflicting['result'], [selected], [PHYSICAL, competitor])


@pytest.mark.asyncio
async def test_pixel_cache_hit_uses_current_article_provenance_after_physical_fetch():
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    service = SimpleNamespace()
    physical = {**PHYSICAL, 'reference_image_urls': [ORIGINAL]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        physical_images = await reference_images(service, [physical], http=http)
        for article in [ARTICLE, 'https://de.wikipedia.org/wiki/Another_article']:
            evidence = []
            selected = candidate(article)
            images = await reference_images(service, [selected], http=http, evidence=evidence)
            assert images[0][2] == physical_images[0][2]
            assert evidence[0]['cache_hit'] is True
            assert evidence[0]['article_url'] == article
            assert evidence[0]['source_url'] == ORIGINAL
            assert evidence[0]['resolved_image_url'] == THUMBNAIL
            assert evidence[0]['model_image_sha256'] == hashlib.sha256(images[0][2]).hexdigest()
            assert bind_reference_subject(completed_verdict(), [selected], [PHYSICAL], evidence)['status'] == 'bound'
    assert requests == [THUMBNAIL]


@pytest.mark.asyncio
@pytest.mark.parametrize('alteration', [{'article_url': 'https://de.wikipedia.org/wiki/Other'},
                                       {'image_url': 'https://upload.wikimedia.org/other.jpg'}])
async def test_unmatched_extraction_cannot_fabricate_article_binding(alteration):
    selected, evidence = candidate(**alteration), []

    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await reference_images(SimpleNamespace(), [selected], http=http, evidence=evidence)
    assert 'article_url' not in evidence[0]
    assert evidence[0]['source_url'] == THUMBNAIL
    assert bind_reference_subject(completed_verdict(), [selected], [PHYSICAL], evidence)['reason'] == 'reference_provenance_missing'


@pytest.mark.asyncio
async def test_failed_thumbnail_original_fallback_keeps_actual_retrieval_and_article_source():
    requests, evidence = [], []

    async def handler(request):
        requests.append(str(request.url))
        if str(request.url) == THUMBNAIL:
            return httpx.Response(404)
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await reference_images(SimpleNamespace(), [candidate()], http=http, evidence=evidence)
    assert requests == [THUMBNAIL, ORIGINAL]
    assert evidence[0]['source_url'] == evidence[0]['resolved_image_url'] == ORIGINAL
    assert evidence[0]['requested_image_url'] == ORIGINAL
    assert bind_reference_subject(completed_verdict(), [candidate()], [PHYSICAL], evidence)['status'] == 'bound'


@pytest.mark.asyncio
async def test_corrupted_article_cache_does_not_claim_verified_article_sha():
    service = SimpleNamespace(store=SimpleNamespace(cache_get=lambda key: cached_article(sha256='a' * 64)))
    evidence = []

    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await reference_images(service, [candidate()], http=http, evidence=evidence)
    assert 'article_source_sha256' not in evidence[0]


@pytest.mark.asyncio
async def test_safe_wikimedia_redirect_receipt_distinguishes_source_and_resolved_derivative():
    resolved = THUMBNAIL.replace('1280px-', '1024px-')
    evidence = []

    async def handler(request):
        if str(request.url) == THUMBNAIL:
            return httpx.Response(302, headers={'location': resolved})
        assert str(request.url) == resolved
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await reference_images(SimpleNamespace(), [candidate()], http=http, evidence=evidence)
    assert evidence[0]['source_url'] == ORIGINAL
    assert evidence[0]['requested_image_url'] == THUMBNAIL
    assert evidence[0]['resolved_image_url'] == resolved
    assert bind_reference_subject(completed_verdict(), [candidate()], [PHYSICAL], evidence)['status'] == 'bound'
