"""Optional photo-camera priors. Never replace visual proof or filter candidates."""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

from PIL import Image


POLICY = 'exif_camera_reference_priority_v1'


def _number(value: Any, minimum=0.0, maximum=1_000_000.0) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and minimum <= number <= maximum else None
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def _ref(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode('ascii', errors='replace')
    return str(value or '').rstrip('\x00').strip().upper()


def read_camera_hints(source: bytes | Path) -> dict[str, Any]:
    """Only an allowlist; no position, capture date, serial, MakerNote or user text."""
    result: dict[str, Any] = {'exif_present': False, 'direction_status': 'missing'}
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            exif = image.getexif()
            result['exif_present'] = bool(exif)
            # Decode IFDs independently: broken GPS must not erase valid lens data.
            try:
                camera = exif.get_ifd(34665)
            except Exception:
                camera = {}
            try:
                gps = exif.get_ifd(34853)
            except Exception:
                gps = {}
                result['direction_status'] = 'unreadable'
            orientation = _number(exif.get(274), 1, 8)
            if orientation is not None and orientation.is_integer():
                result['pixel_orientation'] = int(orientation)
            direction = _number(gps.get(17), 0, 359.999999)
            reference = _ref(gps.get(16))
            if 16 in gps or 17 in gps:
                if direction is None or reference not in {'T', 'M'}:
                    result['direction_status'] = 'invalid_or_unspecified_reference'
                else:
                    result.update(direction_degrees=direction, direction_ref=reference,
                                  direction_status='true_north' if reference == 'T' else 'magnetic_uncorrected')
            # Track/destination bearing are deliberately NOT camera direction.
            result['gps_track_present'] = 15 in gps
            for tag, name in ((37386, 'focal_length_mm'), (41989, 'focal_length_35mm'), (41988, 'digital_zoom_ratio')):
                value = _number(camera.get(tag), 0.001, 10_000)
                if value is not None:
                    result[name] = value
            for tag, name in ((31, 'horizontal_error_m'), (11, 'gps_dop')):
                value = _number(gps.get(tag))
                if value is not None:
                    result[name] = value
            datum = _ref(gps.get(18))
            if datum:
                result['map_datum_wgs84'] = datum.replace('-', '').replace(' ', '') in {'WGS84', 'WGS1984'}
            focal = result.get('focal_length_35mm')
            if focal:
                # Reference diagonal FOV, not exact real/cropped horizontal FOV.
                # The separate DigitalZoomRatio is not multiplied into this value.
                result['diagonal_fov_35mm_deg'] = round(math.degrees(2 * math.atan(math.hypot(36, 24) / (2 * focal))), 1)
    except Exception as exc:
        result['read_error'] = type(exc).__name__
    return result


def model_camera_hints(hints: dict[str, Any]) -> dict[str, Any]:
    return {key: hints[key] for key in (
        'focal_length_mm', 'focal_length_35mm', 'digital_zoom_ratio', 'diagonal_fov_35mm_deg',
    ) if key in hints}


def metadata_summary(hints: dict[str, Any]) -> dict[str, Any]:
    """Routine logs expose availability/reason, not compass angle or GPS position."""
    return {'policy': POLICY, 'exif_present': hints.get('exif_present', False),
            'direction_status': hints.get('direction_status', 'missing'),
            'horizontal_error_available': 'horizontal_error_m' in hints,
            'gps_track_ignored': hints.get('gps_track_present', False),
            **model_camera_hints(hints)}


def _point(item: dict[str, Any]) -> tuple[float, float] | None:
    center = item.get('center') if isinstance(item.get('center'), dict) else {}
    lat = _number(item.get('lat', center.get('lat')), -90, 90)
    lon = _number(item.get('lon', center.get('lon')), -180, 180)
    return (lat, lon) if lat is not None and lon is not None else None


def annotate_camera_alignment(candidates, osm, wikipedia, lat, lon, hints, *, position_verified=False):
    """Attach soft alignment only. The nearest-first shortlist order/set is unchanged."""
    heading = _number(hints.get('direction_degrees'), 0, 359.999999)
    origin = _point({'lat': lat, 'lon': lon})
    if (not position_verified or hints.get('direction_status') != 'true_north'
            or hints.get('direction_ref') != 'T' or heading is None or origin is None
            or hints.get('map_datum_wgs84') is False):
        return [dict(item) for item in candidates]
    raw = {}
    for item in [osm.get('reverse', {}), *(osm.get('nearby') or [])]:
        kind, key = item.get('osm_type') or item.get('type'), item.get('osm_id') or item.get('id')
        if key is not None:
            raw[f'osm:{kind}:{key}'] = item
    for item in wikipedia:
        if item.get('pageid') is not None:
            raw['wiki:' + str(item['pageid'])] = item
    error = _number(hints.get('horizontal_error_m')) or 0.0
    cone = max(45.0, min(90.0, (hints.get('diagonal_fov_35mm_deg') or 60.0) / 2 + 30.0))
    phi1 = math.radians(origin[0])
    result = []
    for item in candidates:
        enriched = dict(item)
        point = _point(raw.get(item.get('candidate_id'), {}))
        if point is not None:
            phi2 = math.radians(point[0])
            dl = math.radians(point[1] - origin[1])
            a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
            metres = 2 * 6_371_000 * math.asin(math.sqrt(min(1, max(0, a))))
            # GPS uncertainty and polygon-centre error dominate nearby bearings.
            if metres > max(25.0, 2 * error):
                y = math.sin(dl) * math.cos(phi2)
                x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
                if abs(x) + abs(y) > 1e-12:
                    bearing = math.degrees(math.atan2(y, x)) % 360
                    delta = abs((bearing - heading + 180) % 360 - 180)
                    enriched['camera_alignment'] = 'ahead' if delta <= cone else 'off_axis'
                    enriched['camera_direction_difference_deg'] = round(delta, 1)
        result.append(enriched)
    return result


def reference_order(candidates):
    """Only references inside the CURRENT nearest-first batch get soft priority.

    No farther batch jumps ahead, no candidate is dropped, and the model still
    sees every member of its original nearest-first batch. Unknowns remain eligible.
    """
    return sorted(candidates, key=lambda item: {'ahead': 0, 'off_axis': 2}.get(item.get('camera_alignment'), 1))
