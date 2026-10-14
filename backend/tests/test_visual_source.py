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
    crop, _, metadata = photo_transform(raw, 'image/jpeg', 'crop_4_5')
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
