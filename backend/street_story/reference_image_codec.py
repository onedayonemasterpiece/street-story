"""Decode a bounded public reference and fit it for visual comparison, without crop."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_MODEL_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_EDGE = 1280


def normalize_reference(data: bytes) -> tuple[str, bytes]:
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("reference_download_size")
    try:
        source = Image.open(BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ValueError("reference_pixel_limit") from exc
    with source:
        if source.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("reference_format")
        if source.width * source.height > MAX_PIXELS:
            raise ValueError("reference_pixel_limit")
        # JPEG draft decoding reduces working memory before loading a large original.
        source.draft("RGB", (MAX_EDGE, MAX_EDGE))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=82)
        result = output.getvalue()
    if len(result) > MAX_MODEL_BYTES:
        raise ValueError("reference_model_size")
    return "image/jpeg", result
