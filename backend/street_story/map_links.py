"""Post ending for the owner-confirmed building: city hashtag and clickable map links.

The targets come only from the exactly selected contour (or the owner's explicit
point), never from the camera point or a neighbouring object. The model never
types these URLs: the saved text gets one canonical line of named links that the
existing publication compiler turns into labels without raw URLs.
"""
from __future__ import annotations

import math
import re
from typing import Any

LABELS = ('ЯндексКарты', '2ГИС')
_LINKED_LINE = re.compile(
    r'^[ \t]*\[(?:ЯндексКарты|2ГИС)\]\(https://[^)\s]+\)'
    r'(?:[ \t]*[·•|,–-][ \t]*\[(?:ЯндексКарты|2ГИС)\]\(https://[^)\s]+\))*[ \t]*$', re.M)
_PLAIN_LINE = re.compile(r'^[ \t]*(?:ЯндексКарты|2ГИС)(?:[ \t]*[·•|,–-][ \t]*(?:ЯндексКарты|2ГИС))*[ \t]*$', re.M)


def _ring_points(ring) -> list[tuple[float, float]]:
    points = ring.get('points') if isinstance(ring, dict) else ring
    result = []
    for point in points or []:
        try:
            lat, lon = float(point['lat']), float(point['lon'])
        except (KeyError, TypeError, ValueError):
            return []
        if math.isfinite(lat) and math.isfinite(lon):
            result.append((lon, lat))
    return result if len(result) >= 4 and result[0] == result[-1] else []


def _inside(point, rings) -> bool:
    # Even-odd over all closed rings: courtyards (inner rings) are not the building.
    x, y = point
    inside = False
    for ring in rings:
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            if (ay > y) != (by > y) and ax + (bx - ax) * (y - ay) / (by - ay) > x:
                inside = not inside
    return inside


def _edge_distance(point, rings) -> float:
    x, y = point
    scale = math.cos(math.radians(y))
    best = math.inf
    for ring in rings:
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            dx, dy = (bx - ax) * scale, by - ay
            px, py = (x - ax) * scale, y - ay
            t = max(0.0, min(1.0, (px * dx + py * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
            best = min(best, math.hypot(px - t * dx, py - t * dy))
    return best


def building_point(candidate: dict[str, Any]) -> tuple[float, float] | None:
    """(lat, lon) inside the selected footprint, or the owner's explicit point."""
    rings = [ring for ring in (_ring_points(r) for r in ((candidate.get('map_geometry') or {}).get('rings') or [])) if ring]
    coordinates = candidate.get('map_coordinates') or {}
    try:
        center = (float(coordinates['longitude']), float(coordinates['latitude']))
    except (KeyError, TypeError, ValueError):
        center = None
    if not rings:
        explicit = coordinates.get('provenance') in {'osm.center', 'owner.explicit_point'}
        return (center[1], center[0]) if center and explicit else None
    if center and _inside(center, rings):
        return center[1], center[0]
    xs = [x for ring in rings for x, _ in ring]
    ys = [y for ring in rings for _, y in ring]
    best, best_distance = None, 0.0
    for row in range(1, 24):
        for column in range(1, 24):
            point = (min(xs) + (max(xs) - min(xs)) * column / 24, min(ys) + (max(ys) - min(ys)) * row / 24)
            if _inside(point, rings):
                distance = _edge_distance(point, rings)
                if distance > best_distance:
                    best, best_distance = point, distance
    return (best[1], best[0]) if best else None


def building_links(research: dict[str, Any]) -> dict[str, str] | None:
    selection = research.get('place_selection') or {}
    if selection.get('mode') != 'exact':
        return None
    point = building_point(selection.get('candidate') or {})
    if point is None:
        return None
    lat, lon = point
    target = f'{lon:.6f}%2C{lat:.6f}'
    return {'ЯндексКарты': f'https://yandex.ru/maps/?whatshere%5Bpoint%5D={target}&whatshere%5Bzoom%5D=18',
            '2ГИС': f'https://2gis.ru/geo/{target}'}


def city_hashtag(research: dict[str, Any]) -> str | None:
    selection = research.get('place_selection') or {}
    if selection.get('mode') != 'exact':
        return None
    city = ((selection.get('candidate') or {}).get('search_anchor') or {}).get('city')
    tag = re.sub(r'[^\w]', '', str(city or ''))
    return '#' + tag if tag else None


def with_place_footer(text: str, research: dict[str, Any]) -> str:
    """End with the confirmed city hashtag and the verified map-link line.

    Typed or invented map-link lines are replaced; an existing hashtag is kept.
    """
    if not text:
        return text
    body, linked = _LINKED_LINE.subn('', text)
    body, plain = _PLAIN_LINE.subn('', body)
    if linked or plain:
        body = re.sub(r'\n{3,}', '\n\n', body)
    body = body.rstrip()
    if not body:
        return body
    hashtag = city_hashtag(research)
    if hashtag and hashtag.casefold() not in body.casefold():
        body += '\n\n' + hashtag
    links = building_links(research)
    if not links:
        return body if body != text.rstrip() else text
    return body + '\n\n' + ' · '.join(f'[{label}]({links[label]})' for label in LABELS)
