"""Lossless, orientation-independent OSM footprint morphology for SOURCE/MAP.

Measured plan morphology can discriminate a compact tower footprint from an
elongated wing. A 2D footprint is NOT a photographed silhouette: camera yaw,
vertical perspective, photo crop, obstruction, building parts and stale OSM
height tags remain unknown. Missing mapped height must NOT be filled with an
invented storey height. These values never authorize identity or filter POIs.

Works with both complete closed OSM ways and joined member-way outer rings of
an OSM multipolygon. Unclosed, ambiguous or disjoint multipart exteriors
return explicit unknown geometry, not a guessed bounding box.
"""
from __future__ import annotations

import math
import re


SHAPE_COLUMNS = [
    'status', 'long_axis_m', 'short_axis_m', 'footprint_elongation',
    'footprint_area_m2', 'convexity', 'plan_compactness',
    'concave_corner_count', 'major_axis_deg_from_north',
    'explicit_height_m', 'observed_building_levels',
    'height_over_long_axis', 'height_over_short_axis',
]
POLICY = (
    'Plan axes are measured minimum-area rotated bounding rectangle of ONE '
    'complete original OSM outer contour; long/short axes describe ground '
    'footprint, NOT image silhouette, facade length or a calibrated perspective. '
    'Area/compactness/concavity describe 2D plan only. EXPLICIT height is '
    'parsed only from observed OSM height tag; levels are not converted into '
    'assumed meters. Height/footprint ratios are soft relative morphology hints '
    'when both measurements are present, not calibrated visual height. '
    'Camera yaw, pitch, crop, viewpoint and occlusion can make a compact '
    'footprint appear wide, or an elongated plan appear narrow. '
    'Never choose/remove an identity using shape alone. Compare the SOURCE '
    'vertical vs horizontal silhouette AND actual OSM physical relationships '
    'to alternative observed bodies. Missing/multipart map shape is UNKNOWN.'
)


def _hull(points):
    """Monotone convex hull on actual observed projected exterior vertices."""
    ordered = sorted(set(points))
    if len(ordered) < 3:
        return []
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    bottom = []
    for point in ordered:
        while len(bottom) >= 2 and cross(bottom[-2], bottom[-1], point) <= 0:
            bottom.pop()
        bottom.append(point)
    top = []
    for point in reversed(ordered):
        while len(top) >= 2 and cross(top[-2], top[-1], point) <= 0:
            top.pop()
        top.append(point)
    return bottom[:-1] + top[:-1]


def _signed_area(points):
    return sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(points, points[1:] + points[:1])) / 2


def _height_tag(tags):
    """Meters only; no implicit floors-to-height conversion or unit guessing."""
    if not isinstance(tags, dict):
        return None
    value = tags.get('height')
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    txt = str(value).strip().lower().replace(',', '.')
    if not re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)\s*(?:m)?', txt):
        return None
    number = float(re.sub(r'\s*m$', '', txt))
    return round(number, 2) if math.isfinite(number) and 1 <= number <= 1000 else None


def _levels_tag(tags):
    value = tags.get('building:levels') if isinstance(tags, dict) else None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    txt = str(value).strip().replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', txt):
        return None
    number = float(txt)
    return round(number, 1) if math.isfinite(number) and .5 <= number <= 250 else None


def observed_plan_shape(entry, *, max_vertices=2000):
    """Return columns-compatible 13-item row using only OSM original geometry.

    The coordinate origin is the FIRST mapped vertex (shape is translation
    invariant), never an inferred camera. Joined outer fragments are allowed
    only if the existing OSM parser yields exactly one closed outer ring.
    Multipolygons with two distinct outer bodies stay unknown rather than
    artificially merging buildings and inventing a long single structure.
    """
    from .identity_map_context import osm_geometry_context
    from .identity_spatial_features import _point, _local

    def unknown(reason):
        return [reason] + [None] * (len(SHAPE_COLUMNS) - 1)

    if not isinstance(entry, dict):
        return unknown('missing_original_osm_geometry')
    geom = entry.get('map_geometry') or osm_geometry_context(entry)
    rings = [r for r in geom.get('rings') or []
             if r.get('role') in (None, 'outer')]
    if len(rings) != 1:
        return unknown('multipart_or_missing_outer_contour')
    ring = rings[0]
    coords = ring.get('points') or []
    if (ring.get('closed') is not True or not 4 <= len(coords) <= max_vertices):
        return unknown('incomplete_or_excessive_outer_contour')
    gps = [_point(point) for point in coords]
    if any(p is None for p in gps) or gps[0] != gps[-1]:
        return unknown('invalid_outer_vertices')
    base = gps[0]
    projected = [_local(p, base) for p in gps[:-1]]
    if len(projected) < 3:
        return unknown('degenerate_outer_contour')
    area = abs(_signed_area(projected))
    perimeter = sum(math.dist(a, b) for a, b in zip(projected, projected[1:] + projected[:1]))
    if area < .25 or perimeter < 2:
        return unknown('degenerate_outer_contour')
    hull = _hull(projected)
    if len(hull) < 3:
        return unknown('degenerate_outer_contour')
    hull_area = abs(_signed_area(hull))
    if hull_area < .25:
        return unknown('degenerate_outer_contour')
    options = []
    # Rotate bounding rectangle through each *observed* convex hull edge.
    # Number of hull vertices is usually small; large malformed contours
    # fail boundedly without inventing a simplified polygon.
    if len(hull) > 256:
        return unknown('too_many_outer_hull_vertices')
    for a, b in zip(hull, hull[1:] + hull[:1]):
        length = math.dist(a, b)
        if length < 1e-6:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        vx, vy = -uy, ux
        u = [p[0] * ux + p[1] * uy for p in hull]
        v = [p[0] * vx + p[1] * vy for p in hull]
        a_span, b_span = max(u) - min(u), max(v) - min(v)
        major = max(a_span, b_span)
        minor = min(a_span, b_span)
        if minor < .1:
            continue
        along = (ux, uy) if a_span >= b_span else (vx, vy)
        bearing = math.degrees(math.atan2(along[0], along[1])) % 180
        options.append((major * minor, major, minor, bearing))
    if not options:
        return unknown('degenerate_minimum_rectangle')
    _, major, minor, bearing = min(options, key=lambda x: (
        x[0], x[1], x[2], x[3]))
    signs = []
    for prev, curr, nxt in zip(
            projected[-1:] + projected[:-1],
            projected, projected[1:] + projected[:1]):
        turning = ((curr[0] - prev[0]) * (nxt[1] - curr[1]) -
                   (curr[1] - prev[1]) * (nxt[0] - curr[0]))
        if abs(turning) > .5:
            signs.append(turning)
    orientation = 1 if _signed_area(projected) > 0 else -1
    concave = sum(1 for turn in signs if turn * orientation < 0)
    tags = {**(entry.get('tags') or {}),
            **(entry.get('map_object') or {}).get('tags', {})}
    height = _height_tag(tags)
    levels = _levels_tag(tags)
    safe_ratio = lambda a, b: round(a / b, 2) if a is not None and b >= .1 else None
    return [
        'observed_closed_outer',
        round(major, 2), round(minor, 2), safe_ratio(major, minor),
        round(area, 1), round(min(1., area / hull_area), 3),
        round(min(1., 4 * math.pi * area / (perimeter * perimeter)), 3),
        concave, round(bearing, 1), height, levels,
        safe_ratio(height, major), safe_ratio(height, minor),
    ]
