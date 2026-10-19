"""MIME transport and permitted rotation/downscaling, entirely in RAM."""
from __future__ import annotations
from io import BytesIO

from PIL import Image, ImageOps

MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_MODEL_BYTES = 2 * 1024 * 1024
MAX_LIVE_BYTES = 480 * 1024
MAX_PIXELS = 40_000_000
MAX_EDGE = 1280
MIN_REFERENCE_EDGE = 160
MODEL_PREPARATION = 'exif_rgb_longedge1280_v1'
SOURCE_MODEL_PREPARATION = 'exif_rgb_original_resolution_v1'


def normalize_source(data: bytes) -> tuple[str, bytes]:
    """Orient SOURCE without discarding small facade/address details.

    Original uploads remain unchanged. Bound pixels and transport bytes, but
    never apply the Live illustration thumbnail to a SOURCE/MAP decision.
    """
    reference_mime(data)
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('source_pixel_limit')
            image = ImageOps.exif_transpose(source).convert('RGB')
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError('source_format') from exc
    output = BytesIO()
    for quality in (90, 82, 70, 58, 46, 34):
        output.seek(0)
        output.truncate()
        image.save(output, format='JPEG', quality=quality, optimize=True)
        if output.tell() <= MAX_MODEL_BYTES:
            return 'image/jpeg', output.getvalue()
    raise ValueError('source_model_size')


def validate_reference_resolution(data: bytes) -> None:
    """Apply the article illustration floor to actual pixels, not HTML hints."""
    reference_mime(data)
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('reference_pixel_limit')
            if min(image.size) < MIN_REFERENCE_EDGE:
                raise ValueError('reference_resolution_insufficient')
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError('reference_format') from exc


def reference_mime(data: bytes) -> str:
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError('reference_download_size')
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('reference_format')


def normalize_reference(data: bytes) -> tuple[str, bytes]:
    """Prepare one original image for Live's budget; never compare or persist it."""
    reference_mime(data)
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('reference_pixel_limit')
            source.draft('RGB', (MAX_EDGE, MAX_EDGE))
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError('reference_format') from exc
    output = BytesIO()
    for quality in (82, 70, 58, 46, 34):
        output.seek(0)
        output.truncate()
        image.save(output, format='JPEG', quality=quality, optimize=True)
        if output.tell() <= MAX_LIVE_BYTES:
            return 'image/jpeg', output.getvalue()
    raise ValueError('reference_model_size')


THUMBNAIL_EDGE = 320


def topic_thumbnail(data: bytes) -> bytes:
    """Small JPEG of the retained original for the phone's topic list.

    The phone keeps no image copies; after an app update it can lose access to
    a shared photo, so the list falls back to this server-side preview.
    """
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('thumbnail_pixel_limit')
            source.draft('RGB', (THUMBNAIL_EDGE * 2, THUMBNAIL_EDGE * 2))
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError('thumbnail_format') from exc
    output = BytesIO()
    image.save(output, format='JPEG', quality=80, optimize=True)
    return output.getvalue()
