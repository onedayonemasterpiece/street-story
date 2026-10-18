"""Read selected-photo GPS; never substitute the phone's current position or (0, 0)."""
from __future__ import annotations
import io
import math
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from PIL import Image


def inspect_capture_time(source: bytes) -> dict[str, Any]:
    """Describe only original EXIF capture time, independently of upload/event time."""
    result: dict[str, Any] = {'status': 'unknown', 'provenance': 'source_exif',
                              'source_sha256': hashlib.sha256(source).hexdigest()}
    try:
        with Image.open(io.BytesIO(source)) as image:
            exif = image.getexif()
            containers = [exif, exif.get_ifd(34665)]
        tags = {}
        for key, tag in (('datetime_original', 36867), ('offset_time_original', 36881),
                         ('subsec_time_original', 37521)):
            values = set()
            for container in containers:
                value = container.get(tag)
                if value is None:
                    continue
                if isinstance(value, bytes):
                    value = value.decode('ascii')
                if not isinstance(value, str) or len(value) > 64:
                    raise ValueError('Invalid EXIF time tag')
                value = value.rstrip('\x00').strip()
                if value:
                    values.add(value)
            if len(values) > 1:
                return {**result, 'reason': 'conflicting_original_time_tags'}
            tags[key] = next(iter(values), None)
        raw = tags['datetime_original']
        if not raw:
            return {**result, 'reason': 'datetime_original_missing'}
        if not re.fullmatch(r'\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', raw):
            raise ValueError('Invalid DateTimeOriginal')
        local = datetime.strptime(raw, '%Y:%m:%d %H:%M:%S')
        subsec = tags['subsec_time_original']
        if subsec and not re.fullmatch(r'\d{1,9}', subsec):
            raise ValueError('Invalid SubsecTimeOriginal')
        local_text = local.isoformat() + ('.' + subsec if subsec else '')
        result.update(datetime_original=raw, local_datetime=local_text)
        if subsec:
            result['subsec_time_original'] = subsec
        offset = tags['offset_time_original']
        if not offset:
            return {**result, 'status': 'timezone_unknown', 'reason': 'offset_time_original_missing'}
        if not re.fullmatch(r'[+-]\d{2}:\d{2}', offset) or int(offset[1:3]) > 23 or int(offset[4:]) > 59:
            return {**result, 'status': 'timezone_unknown', 'reason': 'offset_time_original_invalid'}
        captured = datetime.fromisoformat(local_text + offset)
        return {**result, 'status': 'known', 'offset_time_original': offset,
                'captured_at': local_text + offset,
                'captured_at_utc': captured.astimezone(timezone.utc).isoformat()}
    except Exception as exc:
        return {**result, 'status': 'unknown', 'reason': 'original_capture_time_unreadable',
                'error_type': type(exc).__name__}


def inspect_gps(source: bytes | Path) -> dict[str, Any]:
    result: dict[str, Any] = {'status': 'gps_missing', 'latitude': None, 'longitude': None}
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            gps = image.getexif().get_ifd(34853)
        if not gps:
            return result
        refs = [gps.get(1), gps.get(3)]
        refs = [x.decode('ascii', errors='replace') if isinstance(x, bytes) else str(x or '') for x in refs]
        if refs[0] not in ('N', 'S') or refs[1] not in ('E', 'W'):
            return {**result, 'status': 'gps_redacted_or_invalid'}
        coords = []
        for key, ref, maximum in ((2, refs[0], 90), (4, refs[1], 180)):
            parts = [float(x) for x in gps[key]]
            if len(parts) != 3 or not all(math.isfinite(x) for x in parts):
                raise ValueError('Invalid GPS fractions')
            degrees, minutes, seconds = parts
            if degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                raise ValueError('Invalid GPS range')
            value = degrees + minutes / 60 + seconds / 3600
            if value > maximum:
                raise ValueError('Invalid GPS range')
            coords.append(-value if ref in ('S', 'W') else value)
        return {'status': 'gps_present', 'latitude': coords[0], 'longitude': coords[1]}
    except Exception as exc:
        return {**result, 'status': 'gps_unreadable', 'error_type': type(exc).__name__}
