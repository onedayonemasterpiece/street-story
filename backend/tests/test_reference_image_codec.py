from io import BytesIO
from types import SimpleNamespace

import httpx
from PIL import Image, ImageDraw
import pytest

from street_story.identity_references import reference_images, thumbnail_reference
from street_story.reference_image_codec import MAX_DOWNLOAD_BYTES, normalize_reference


def jpeg(size=(80, 120), *, orientation=1):
    image = Image.new('RGB', size, 'white')
    exif = Image.Exif()
    exif[274] = orientation
    exif[315] = 'private-metadata-not-for-model'
    output = BytesIO()
    image.save(output, format='JPEG', exif=exif)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['google', 'native', 'opencode'])
@pytest.mark.parametrize('size', [(60, 60), (400, 100), (160, 320)])
async def test_real_download_dimensions_protect_all_public_vision_loaders(monkeypatch, route, size):
    from street_story.headless_vision import HeadlessVisionProvider
    from street_story.native_vision import native_public_image
    from street_story.opencode_research import OpenCodeResearch
    from street_story.errors import PermanentProviderError
    raw = jpeg(size)
    async def fetch(*args, **kwargs):
        return 'https://example.org/illustration.jpg', 'image/jpeg', raw
    monkeypatch.setattr('street_story.article_media.fetch_public', fetch)
    loaders = {'google': lambda: HeadlessVisionProvider._load_public_reference(None, 'https://example.org/illustration.jpg'),
               'native': lambda: native_public_image('https://example.org/illustration.jpg'),
               'opencode': lambda: OpenCodeResearch._load_public_image('https://example.org/illustration.jpg')}
    if min(size) < 160:
        with pytest.raises((ValueError, PermanentProviderError), match='reference_resolution_insufficient'):
            await loaders[route]()
    else:
        assert await loaders[route]() == ('image/jpeg', raw)


def test_normalization_honors_orientation_and_removes_metadata():
    mime, data = normalize_reference(jpeg(orientation=6))
    assert mime == 'image/jpeg'
    with Image.open(BytesIO(data)) as image:
        assert image.size == (120, 80)
        assert not image.getexif()
    assert b'private-metadata-not-for-model' not in data


def test_wide_reference_keeps_both_edges_without_crop():
    source = Image.new('RGB', (4000, 1000), 'white')
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 200, 999), fill='red')
    draw.rectangle((3799, 0, 3999, 999), fill='blue')
    output = BytesIO()
    source.save(output, format='JPEG')
    _, data = normalize_reference(output.getvalue())
    with Image.open(BytesIO(data)) as image:
        assert image.size == (1280, 320)
        assert image.getpixel((10, 160))[0] > 200
        assert image.getpixel((1270, 160))[2] > 200


def test_invalid_or_oversized_input_is_rejected_before_budget_preparation():
    with pytest.raises(ValueError):
        normalize_reference(b'not-an-image')
    with pytest.raises(ValueError, match='download_size'):
        normalize_reference(b'x' * (MAX_DOWNLOAD_BYTES + 1))


def test_budget_preparation_limits_working_pixels(monkeypatch):
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10)
    with pytest.raises(ValueError):
        normalize_reference(jpeg())


@pytest.mark.asyncio
async def test_reference_selector_retains_addresses_without_downloading():
    async def handler(request):
        pytest.fail('URL selection must not fetch image bytes')
    candidates = [{'candidate_id': 'wiki:1', 'reference_image_urls': ['https://upload.wikimedia.org/test.jpg']}]
    evidence = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(SimpleNamespace(), candidates, http=client, evidence=evidence)
    assert images == [('wiki:1', 'image/jpeg', 'https://upload.wikimedia.org/test.jpg')]
    assert evidence[0]['source_url'] == images[0][2]
    assert not any('sha' in key for key in evidence[0])


def test_wikimedia_thumbnail_is_only_a_public_address():
    original = 'https://upload.wikimedia.org/wikipedia/commons/1/1b/Water_Tower.jpg'
    assert thumbnail_reference(original) == (
        'https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Water_Tower.jpg/1280px-Water_Tower.jpg')
    assert thumbnail_reference('https://evil.invalid/a.jpg') is None


@pytest.mark.asyncio
async def test_multi_view_urls_preserve_distinct_references_for_same_candidate():
    candidate = {'candidate_id':'entity:1', 'multi_view':True,
        'reference_image_urls':['https://upload.wikimedia.org/a.jpg','https://upload.wikimedia.org/b.jpg']}
    images = await reference_images(SimpleNamespace(), [candidate], limit=2)
    assert [item[0] for item in images] == ['entity:1','entity:1']
    assert images[0][2] != images[1][2]
