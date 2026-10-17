"""A metadata transpose preserves the complete decoded source, never scene geometry."""
import hashlib
import io

from PIL import Image, ImageChops, ImageOps
import pytest

from street_story.visual_source import oriented_visual_source


def test_source_frame_freezes_explicit_model_copy_without_dropping_qualifiers():
    from street_story.visual_source import source_frame_input
    body = {'preserve_source_frame': True,
        'visual_copy': {'title': 'Выбранный дом', 'body': 'В статье 2021 года сообщалось о планах, не о завершении.'},
        'visual_layout': {'text_box': [.05, .02, .9, .14], 'title_size': 42, 'body_size': 28}}
    result = source_frame_input(body)
    assert result['copy'] == body['visual_copy'] and result['layout'] == body['visual_layout']
    assert result['contract'] == 'immutable-source-frame-svg-v1'


@pytest.mark.parametrize('change', [
    {'preserve_source_frame': False}, {'visual_copy': {}},
    {'visual_layout': {'text_box': [.9, .1, .5, .2]}},
    {'visual_layout': {'text_box': [.05, .02, .9, .14], 'body_size': 12}},
    {'visual_layout': {'text_box': [.05, .02, .9, .14], 'backplate_opacity': 1}},
])
def test_source_frame_invalid_layout_or_implicit_mode_cannot_silently_fall_back(change):
    from street_story.visual_source import source_frame_input
    from street_story.service import ConflictError
    body = {'preserve_source_frame': True, 'visual_copy': {'title': 'Дом'},
        'visual_layout': {'text_box': [.05, .02, .9, .14]}, **change}
    with pytest.raises(ConflictError):
        source_frame_input(body)


@pytest.mark.parametrize('orientation', range(1, 9))
def test_exif_orientations_preserve_all_source_pixels_and_leave_original_unchanged(orientation):
    source = Image.new('RGB', (40, 30))
    for x in range(40):
        for y in range(30):
            source.putpixel((x,y), (x*5,y*7,(x+y)*3))
    exif = Image.Exif()
    exif[274] = orientation
    stream = io.BytesIO()
    source.save(stream, format='JPEG', exif=exif)
    raw = stream.getvalue()
    original_hash = hashlib.sha256(raw).hexdigest()
    data, mime, receipt = oriented_visual_source(raw, 'image/jpeg')
    with Image.open(io.BytesIO(raw)) as original:
        expected = ImageOps.exif_transpose(original).convert('RGB')
    with Image.open(io.BytesIO(data)) as result:
        assert ImageChops.difference(expected, result.convert('RGB')).getbbox() is None
        assert list(result.size) == receipt['oriented_size']
        if orientation != 1:
            assert mime == 'image/png' and not result.getexif()
    assert receipt['original_sha256'] == original_hash == hashlib.sha256(raw).hexdigest()
    assert receipt['original_preserved']
    if orientation == 1:
        assert data == raw and mime == 'image/jpeg'


@pytest.mark.parametrize('orientation', [1, 6, 8])
def test_photo_choice_original_and_actual_crop_have_exact_oriented_pixels(orientation):
    from street_story.visual_source import photo_transform
    source = Image.new('RGB', (80, 60))
    for x in range(80):
        for y in range(60):
            source.putpixel((x, y), (x * 3, y * 4, (x + y) % 255))
    exif = Image.Exif()
    exif[274] = orientation
    stream = io.BytesIO()
    source.save(stream, format='JPEG', exif=exif)
    raw = stream.getvalue()
    with Image.open(io.BytesIO(raw)) as image:
        expected = ImageOps.exif_transpose(image).convert('RGB')
    full, _, original = photo_transform(raw, 'image/jpeg', 'original')
    with Image.open(io.BytesIO(full)) as result:
        assert result.size == expected.size
        assert ImageChops.difference(expected, result.convert('RGB')).getbbox() is None
    # crop_4_5 alone sends the whole frame; the image model composes the portrait.
    composed, _, whole = photo_transform(raw, 'image/jpeg', 'crop_4_5')
    assert whole['crop_rect'] is None and whole['output_size'] == list(expected.size)
    with Image.open(io.BytesIO(composed)) as result:
        assert ImageChops.difference(expected, result.convert('RGB')).getbbox() is None
    # Only an explicitly requested rectangle cuts pixels, exactly.
    w, h = expected.size
    unit = min(w // 4, h // 5)
    rect = [(w - 4 * unit) // 2, (h - 5 * unit) // 2, 4 * unit, 5 * unit]
    crop, _, metadata = photo_transform(raw, 'image/jpeg', 'crop_4_5', rect)
    x, y, width, height = metadata['crop_rect']
    assert width * 5 == height * 4 and metadata['output_size'] == [width, height]
    with Image.open(io.BytesIO(crop)) as result:
        assert result.tobytes() == expected.crop((x, y, x + width, y + height)).tobytes()
    assert original['original_sha256'] == metadata['original_sha256'] == hashlib.sha256(raw).hexdigest()
    assert metadata['output_sha256'] == hashlib.sha256(crop).hexdigest()


@pytest.mark.parametrize('rect', [[0, 0, 80, 100], [0, 0, 30, 30], [-1, 0, 24, 30], [0, 0, 24.0, 30]])
def test_photo_crop_rejects_expansion_stretch_and_noninteger_window(rect):
    from street_story.visual_source import photo_transform
    from street_story.service import ConflictError
    stream = io.BytesIO()
    Image.new('RGB', (40, 30)).save(stream, format='PNG')
    with pytest.raises(ConflictError):
        photo_transform(stream.getvalue(), 'image/png', 'crop_4_5', rect)


@pytest.mark.parametrize('mode', ['original', 'crop_4_5'])
def test_frozen_v1_transform_keeps_legacy_pixels_and_hash_after_downscale_upgrade(mode):
    from street_story.visual_source import photo_transform
    raw = io.BytesIO()
    Image.new('RGB', (2400, 3000), (120, 80, 40)).save(raw, format='JPEG')
    data = raw.getvalue()
    with Image.open(io.BytesIO(data)) as image:
        expected = image.convert('RGB')
        if mode == 'crop_4_5':
            stream = io.BytesIO()
            expected.save(stream, format='PNG')
            expected_bytes, expected_mime = stream.getvalue(), 'image/png'
        else:
            expected_bytes, expected_mime = data, 'image/jpeg'
    old, mime, frozen = photo_transform(data, 'image/jpeg', mode, contract='owner-photo-transform-v1')
    assert old == expected_bytes and mime == expected_mime
    assert frozen['output_size'] == [2400, 3000]
    assert frozen['output_sha256'] == hashlib.sha256(expected_bytes).hexdigest()
    assert frozen['pixel_operations'] == ['exif_orientation'] + (['crop'] if mode == 'crop_4_5' else [])
    current, _, new = photo_transform(data, 'image/jpeg', mode)
    assert new['contract'] == 'owner-photo-transform-v2'
    assert max(new['output_size']) <= 2048 and current != old
    assert hashlib.sha256(data).hexdigest() == frozen['original_sha256'] == new['original_sha256']
