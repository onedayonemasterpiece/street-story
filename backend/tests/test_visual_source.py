"""A metadata transpose preserves the complete decoded source, never scene geometry."""
import hashlib
import io

from PIL import Image, ImageChops, ImageOps
import pytest

from street_story.visual_source import oriented_visual_source


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
