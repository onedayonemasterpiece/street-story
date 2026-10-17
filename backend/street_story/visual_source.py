"""Mechanical EXIF orientation for a separate visual input; original stays intact."""
from __future__ import annotations

import hashlib
import io
import math
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import PermanentProviderError

CONTRACT = 'visual-source-exif-orientation-v1'
# Posts end up in Telegram/VK at a few thousand pixels at most and the image
# model returns ~1.5 MP; a 4000x3000 PNG (15-19 MB) only slows the request.
VISUAL_SOURCE_LONG_SIDE = 2048


def photo_transform(data, mime, mode, crop_rect=None, *, contract='owner-photo-transform-v2'):
    """EXIF transpose, a uniform downscale and only an explicitly requested crop.

    crop_4_5 without an explicit rectangle sends the whole frame: the image model
    composes the 4:5 picture, so no deterministic cut can remove the building.
    """
    from .service import ConflictError
    if contract not in {'owner-photo-transform-v1', 'owner-photo-transform-v2'}:
        raise PermanentProviderError('visual_source_transform_contract_unknown')
    legacy = contract == 'owner-photo-transform-v1'
    if mode not in {'original', 'crop_4_5'}:
        raise ConflictError('photo_format_invalid', 'Choose original or crop_4_5.')
    oriented, oriented_mime, orientation = oriented_visual_source(data, mime)
    width, height = orientation['oriented_size']
    rect = [0, 0, width, height]
    if legacy and mode == 'crop_4_5' and crop_rect is None:
        unit = min(width // 4, height // 5)
        crop_rect = [(width - 4 * unit) // 2, (height - 5 * unit) // 2, 4 * unit, 5 * unit]
    if mode == 'original' or crop_rect is None:
        if mode == 'original' and crop_rect is not None:
            raise ConflictError('photo_crop_not_selected', 'Original preserves the complete oriented frame.')
        output, output_mime = oriented, oriented_mime
    else:
        rect = crop_rect
        if (not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int or v < 0 for v in rect)
                or rect[2] <= 0 or rect[3] <= 0 or rect[0]+rect[2] > width or rect[1]+rect[3] > height
                or 5*rect[2] != 4*rect[3]):
            raise ConflictError('photo_crop_invalid', 'Supply an in-bounds exact 4:5 rectangle of oriented source pixels.')
        with Image.open(io.BytesIO(oriented)) as image:
            cropped = image.crop((rect[0], rect[1], rect[0]+rect[2], rect[1]+rect[3])).convert('RGB')
            target = rect[2:] if legacy else _target(rect[2:], exact_4_5=True)
            stream = io.BytesIO()
            if target == rect[2:]:
                cropped.save(stream, format='PNG')
                output, output_mime = stream.getvalue(), 'image/png'
            else:
                cropped.resize(tuple(target), Image.Resampling.LANCZOS).save(stream, format='JPEG', quality=92, optimize=True)
                output, output_mime = stream.getvalue(), 'image/jpeg'
    cropped_frame = mode == 'crop_4_5' and crop_rect is not None
    size = rect[2:] if legacy else _target(rect[2:], exact_4_5=True)
    if not cropped_frame and not legacy:
        output, output_mime, size = _bounded(output, output_mime, rect[2:], exact_4_5=False)
    return output, output_mime, {'contract': contract, 'mode': mode,
        'original_sha256': hashlib.sha256(data).hexdigest(), 'orientation': orientation,
        'oriented_size': [width, height], 'crop_rect': rect if cropped_frame else None if mode == 'crop_4_5' else rect,
        'output_size': size, 'output_sha256': hashlib.sha256(output).hexdigest(), 'original_preserved': True,
        **({'composition': 'model_4_5_from_whole_frame'} if mode == 'crop_4_5' and not cropped_frame else {}),
        'pixel_operations': ['exif_orientation'] + (['crop'] if cropped_frame else [])
            + (['downscale'] if size != rect[2:] else [])}


def _target(size, *, exact_4_5):
    width, height = size
    if max(width, height) <= VISUAL_SOURCE_LONG_SIDE:
        return list(size)
    if exact_4_5:
        unit = min(width // 4, height // 5, VISUAL_SOURCE_LONG_SIDE // 5)
        return [4 * unit, 5 * unit]
    scale = VISUAL_SOURCE_LONG_SIDE / max(width, height)
    return [max(1, round(width * scale)), max(1, round(height * scale))]


def _bounded(data, mime, size, *, exact_4_5):
    """Uniform downscale to the visual input bound; never stretch or change the frame."""
    target = _target(size, exact_4_5=exact_4_5)
    if target == list(size):
        return data, mime, list(size)
    with Image.open(io.BytesIO(data)) as image:
        resized = image.convert('RGB').resize(tuple(target), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        resized.save(stream, format='JPEG', quality=92, optimize=True)
    return stream.getvalue(), 'image/jpeg', target


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
