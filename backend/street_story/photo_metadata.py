"""Read selected-photo GPS; never substitute the phone's current position or (0, 0)."""
from __future__ import annotations
import io
import math
from pathlib import Path
from typing import Any
from PIL import Image


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
