"""Mechanical EXIF orientation for a separate visual input; original stays intact."""
from __future__ import annotations

import hashlib
import io
import math
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import PermanentProviderError

CONTRACT = 'visual-source-exif-orientation-v1'


def photo_transform(data, mime, mode, crop_rect=None):
    """Only EXIF transpose and a declared pixel crop; never outpaint/stretch."""
    from .service import ConflictError
    if mode not in {'original', 'crop_4_5'}:
        raise ConflictError('photo_format_invalid', 'Choose original or crop_4_5.')
    oriented, oriented_mime, orientation = oriented_visual_source(data, mime)
    width, height = orientation['oriented_size']
    rect = [0, 0, width, height]
    if mode == 'original':
        if crop_rect is not None:
            raise ConflictError('photo_crop_not_selected', 'Original preserves the complete oriented frame.')
        output, output_mime = oriented, oriented_mime
    else:
        if crop_rect is None:
            unit = min(width//4, height//5)
            rect = [(width-4*unit)//2, (height-5*unit)//2, 4*unit, 5*unit]
        else:
            rect = crop_rect
        if (not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int or v < 0 for v in rect)
                or rect[2] <= 0 or rect[3] <= 0 or rect[0]+rect[2] > width or rect[1]+rect[3] > height
                or 5*rect[2] != 4*rect[3]):
            raise ConflictError('photo_crop_invalid', 'Supply an in-bounds exact 4:5 rectangle of oriented source pixels.')
        with Image.open(io.BytesIO(oriented)) as image:
            cropped = image.crop((rect[0], rect[1], rect[0]+rect[2], rect[1]+rect[3])).convert('RGB')
            stream = io.BytesIO()
            cropped.save(stream, format='PNG')
            output, output_mime = stream.getvalue(), 'image/png'
    return output, output_mime, {'contract': 'owner-photo-transform-v1', 'mode': mode,
        'original_sha256': hashlib.sha256(data).hexdigest(), 'orientation': orientation,
        'oriented_size': [width, height], 'crop_rect': rect, 'output_size': rect[2:],
        'output_sha256': hashlib.sha256(output).hexdigest(), 'original_preserved': True,
        'pixel_operations': ['exif_orientation'] + (['crop'] if mode == 'crop_4_5' else [])}


def source_frame_input(body):
    from .service import ConflictError
    if body.get('preserve_source_frame') is not True:
        if body.get('visual_copy') is not None or body.get('visual_layout') is not None:
            raise ConflictError('visual_source_frame_required', 'Explicit copy/layout requires source-frame mode.')
        return None
    copy, layout = body.get('visual_copy'), body.get('visual_layout')
    limits = {'title': 160, 'subtitle': 240, 'body': 1200, 'date_line': 160,
        'location_line': 240, 'source_line': 300}
    if (not isinstance(copy, dict) or not copy or set(copy)-limits.keys()
            or any(not isinstance(v, str) or not v.strip() or len(v) > limits[k] for k, v in copy.items())):
        raise ConflictError('visual_copy_invalid', 'Supply explicit concise copy for the chosen eligible facts.')
    box = layout.get('text_box') if isinstance(layout, dict) else None
    if (not isinstance(layout, dict) or set(layout)-{'text_box', 'title_size', 'body_size', 'text_color', 'backplate_opacity'}
            or not isinstance(box, list) or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
            or box[2] <= 0 or box[3] <= 0 or box[0]+box[2] > 1 or box[1]+box[3] > 1):
        raise ConflictError('visual_layout_invalid', 'Choose a normalized text area after viewing the original photo.')
    for key, low, high in [('title_size', 28, 68), ('body_size', 22, 42), ('backplate_opacity', 0, .65)]:
        if key in layout and (type(layout[key]) not in (int, float) or not low <= layout[key] <= high):
            raise ConflictError('visual_layout_invalid', 'Keep text readable and the contrast plate modest.')
    color = layout.get('text_color', '#ffffff')
    if (not isinstance(color, str) or len(color) != 7 or color[0] != '#'
            or any(c not in 'abcdefABCDEF0123456789' for c in color[1:])):
        raise ConflictError('visual_layout_invalid', 'Use a six-digit RGB text color.')
    return {'contract': 'immutable-source-frame-svg-v1', 'copy': copy, 'layout': layout}


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
