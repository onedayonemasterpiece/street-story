"""Mechanical EXIF orientation for a separate visual input; original stays intact."""
from __future__ import annotations

import hashlib
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import PermanentProviderError

CONTRACT = 'visual-source-exif-orientation-v1'


def oriented_visual_source(data, mime):
    try:
        with warnings.catch_warnings(), Image.open(io.BytesIO(data)) as source:
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            if (source.width * source.height > 25_000_000 or getattr(source, 'n_frames', 1) != 1
                    or {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(source.format) != mime):
                raise PermanentProviderError('visual_source_format_or_dimensions')
            orientation = source.getexif().get(274)
            if orientation is not None and (type(orientation) is not int or orientation not in range(1, 9)):
                raise PermanentProviderError('visual_source_orientation_invalid')
            original_size = list(source.size)
            if orientation in {None, 1}:
                result, result_mime, oriented_size = data, mime, original_size
            else:
                oriented = ImageOps.exif_transpose(source)
                clean = Image.new('RGB', oriented.size)
                clean.paste(oriented.convert('RGB'))
                stream = io.BytesIO()
                clean.save(stream, format='PNG')
                result, result_mime, oriented_size = stream.getvalue(), 'image/png', list(clean.size)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PermanentProviderError('visual_source_image_invalid') from exc
    return result, result_mime, {'contract': CONTRACT, 'original_sha256': hashlib.sha256(data).hexdigest(),
        'original_size': original_size, 'exif_orientation': orientation, 'oriented_size': oriented_size,
        'oriented_sha256': hashlib.sha256(result).hexdigest(), 'oriented_mime': result_mime,
        'original_preserved': True}
