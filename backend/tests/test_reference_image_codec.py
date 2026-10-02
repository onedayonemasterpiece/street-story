from io import BytesIO
import random
from types import SimpleNamespace

import httpx
from PIL import Image, ImageDraw
import pytest

from street_story.identity_references import reference_images, thumbnail_reference
from street_story.reference_image_codec import MAX_DOWNLOAD_BYTES, MAX_MODEL_BYTES, normalize_reference


def jpeg(size=(80, 120), *, orientation=1):
    image = Image.new('RGB', size, 'white')
    exif = Image.Exif()
    exif[274] = orientation
    exif[315] = 'private-metadata-not-for-model'
    output = BytesIO()
    image.save(output, format='JPEG', exif=exif)
    return output.getvalue()


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


def test_invalid_or_oversized_download_is_not_sent_to_model():
    with pytest.raises(OSError):
        normalize_reference(b'not-an-image')
    with pytest.raises(ValueError, match='download_size'):
        normalize_reference(b'x' * (MAX_DOWNLOAD_BYTES + 1))


def test_decompression_bomb_is_a_bounded_reference_failure(monkeypatch):
    source = jpeg()
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10)
    with pytest.raises(ValueError, match='pixel_limit'):
        normalize_reference(source)


@pytest.mark.asyncio
async def test_real_original_larger_than_two_mib_is_normalized_and_cached():
    rng = random.Random(0)
    source = Image.frombytes('RGB', (2200, 1600), rng.randbytes(2200 * 1600 * 3))
    output = BytesIO()
    source.save(output, format='JPEG', quality=96)
    original = output.getvalue()
    assert 2 * 1024 * 1024 < len(original) < MAX_DOWNLOAD_BYTES
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=original)
    candidates = [{'candidate_id': 'wiki:1', 'reference_image_urls': ['https://upload.wikimedia.org/test.jpg']}]
    service = SimpleNamespace()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(service, candidates, http=client)
        cached = await reference_images(service, candidates, http=client)
    assert len(calls) == 1 and cached == images
    assert images[0][:2] == ('wiki:1', 'image/jpeg')
    assert len(images[0][2]) < MAX_MODEL_BYTES
    with Image.open(BytesIO(images[0][2])) as image:
        assert max(image.size) == 1280


@pytest.mark.asyncio
async def test_too_large_header_and_broken_body_fail_without_model_image():
    async def handler(request):
        if request.url.path == '/huge.jpg':
            return httpx.Response(200, headers={'content-type': 'image/jpeg', 'content-length': str(MAX_DOWNLOAD_BYTES + 1)})
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'broken')
    candidates = [{'candidate_id': 'wiki:1', 'reference_image_urls': [
        'https://upload.wikimedia.org/huge.jpg', 'https://upload.wikimedia.org/broken.jpg']}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await reference_images(SimpleNamespace(), candidates, http=client) == []

def test_wikimedia_original_has_same_host_bounded_thumbnail():
    original = 'https://upload.wikimedia.org/wikipedia/commons/1/1b/Water_Tower.jpg'
    assert thumbnail_reference(original) == (
        'https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/Water_Tower.jpg/1280px-Water_Tower.jpg')
    assert thumbnail_reference('https://evil.invalid/a.jpg') is None


@pytest.mark.asyncio
async def test_oversized_wikimedia_original_falls_back_to_thumbnail():
    small = jpeg((640, 480))
    calls = []
    original = 'https://upload.wikimedia.org/wikipedia/commons/1/1b/Tower.jpg'
    async def handler(request):
        calls.append(request.url.path)
        if '/thumb/' not in request.url.path:
            return httpx.Response(200, headers={'content-type':'image/jpeg',
                'content-length': str(MAX_DOWNLOAD_BYTES + 1)})
        return httpx.Response(200, headers={'content-type':'image/jpeg'}, content=small)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(SimpleNamespace(), [
            {'candidate_id':'commons:1','reference_image_urls':[original]}], http=client)
    assert len(images) == 1
    assert calls == ['/wikipedia/commons/thumb/1/1b/Tower.jpg/1280px-Tower.jpg']


@pytest.mark.asyncio
async def test_multiview_candidate_can_send_two_actual_images_under_one_total_budget():
    first, second = jpeg((100,80)), jpeg((120,90))
    async def handler(request):
        return httpx.Response(200, headers={'content-type':'image/jpeg'},
            content=first if request.url.path.endswith('a.jpg') else second)
    candidate = {'candidate_id':'entity:1', 'multi_view':True,
        'reference_image_urls':['https://upload.wikimedia.org/a.jpg','https://upload.wikimedia.org/b.jpg']}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(SimpleNamespace(), [candidate], limit=2, http=client)
    assert [item[0] for item in images] == ['entity:1','entity:1']