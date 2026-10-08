"""Allowlisted structured map anchors; no address or physical identity inference."""
from __future__ import annotations

import math


def osm_geometry_context(item: dict, *, source_url: str | None = None) -> dict:
    """Preserve observed OSM boundary coordinates; never invent missing vertices."""
    def points(raw):
        if not isinstance(raw, list):
            return []
        result = []
        for point in raw:
            if not isinstance(point, dict):
                return []
            if isinstance(point.get('lat'), bool) or isinstance(point.get('lon'), bool):
                return []
            try:
                lat, lon = float(point['lat']), float(point['lon'])
            except (KeyError, TypeError, ValueError):
                return []
            if not math.isfinite(lat + lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                return []
            result.append({'lat': lat, 'lon': lon})
        return result if len(result) >= 2 else []
    lines = []
    line = points(item.get('geometry'))
    if line:
        lines.append(line)
    for member in item.get('members') or []:
        if isinstance(member, dict) and member.get('type') == 'way':
            line = points(member.get('geometry'))
            if line:
                lines.append(line)
    return {'lines': lines, 'provenance': 'osm.observed_geometry', 'source_url': source_url,
            'scope': 'mapped_entry_only'} if lines else {}


def geometry_camera_context(item: dict, lat: float, lon: float) -> dict:
    """Local tangent-plane distances and angular extent are soft spatial priors."""
    geometry = osm_geometry_context(item)
    lines = geometry.get('lines') or []
    if not lines:
        return {}
    scale = 6_371_000 * math.pi / 180
    cos_lat = math.cos(math.radians(lat))
    local = [[(((p['lon'] - lon + 180) % 360 - 180) * scale * cos_lat,
               (p['lat'] - lat) * scale) for p in line] for line in lines]
    nearest = math.inf
    inside = False
    bearings = []
    for line in local:
        in_ring = False
        for x, y in line:
            bearings.append(math.degrees(math.atan2(x, y)) % 360)
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = min(1.0, max(0.0, -(ax * dx + ay * dy) / length2)) if length2 else 0.0
            nearest = min(nearest, math.hypot(ax + t * dx, ay + t * dy))
            if (ay > 0) != (by > 0) and ax + (bx - ax) * (-ay) / (by - ay) > 0:
                in_ring = not in_ring
        if line[0] == line[-1] and in_ring:
            inside = not inside
    bearings.sort()
    gaps = [(bearings[(i + 1) % len(bearings)] + (360 if i == len(bearings) - 1 else 0) - value, i)
            for i, value in enumerate(bearings)]
    gap, index = max(gaps)
    return {'boundary_distance_m': round(nearest, 1), 'distance_m': 0.0 if inside else round(nearest, 1),
            'distance_provenance': 'camera_to_observed_osm_boundary',
            'footprint_bearing_interval': {'start_degrees': bearings[(index + 1) % len(bearings)],
                'end_degrees': bearings[index], 'angular_span_degrees': round(360.0 if inside else 360 - gap, 1),
                'provenance': 'camera_to_observed_osm_geometry'}, 'camera_inside_footprint': inside}


def map_entry_context(item: dict, *, source_url: str | None = None, coordinate_provenance: str | None = None) -> dict:
    tags = item.get('tags') if isinstance(item.get('tags'), dict) else {}
    kind = item.get('osm_type') or item.get('type')
    object_id = item.get('osm_id') or item.get('id')
    osm_object = kind in {'node', 'way', 'relation'} and bool(object_id)
    source_url = source_url or (f'https://www.openstreetmap.org/{kind}/{object_id}' if osm_object else None)
    result = {}
    if osm_object:
        result['candidate_id'] = f'osm:{kind}:{object_id}'
    mapped_tags = {key: str(tags[key])[:180] for key in (
        'name', 'building', 'building:part', 'entrance', 'amenity', 'shop', 'office', 'tourism',
        'historic', 'highway', 'man_made', 'landuse', 'leisure', 'place') if tags.get(key)}
    mapped_kind = {key: str(item[key])[:100] for key in ('category', 'class', 'addresstype') if item.get(key)}
    if item.get('osm_type') and item.get('type'):
        mapped_kind['type'] = str(item['type'])[:100]
    if mapped_tags or mapped_kind:
        result['map_object'] = {**mapped_kind, 'tags': mapped_tags,
            'provenance': 'osm.tags' if mapped_tags else 'nominatim.reverse',
            'source_url': source_url, 'scope': 'mapped_entry_only'}
        entrances = item.get('building_entrance_node_ids')
        if kind == 'way' and mapped_tags.get('building') not in {None, '', 'no'} and isinstance(entrances, list):
            result['map_object']['building_entrances'] = {
                'proof': 'osm_closed_way_node_membership', 'source_url': source_url,
                'candidate_ids': [f'osm:node:{value}' for value in entrances
                                  if isinstance(value, int) and not isinstance(value, bool) and value > 0]}
    address = {key: str(tags[field]).strip()[:180] for key, field in (
        ('street', 'addr:street'), ('house_number', 'addr:housenumber')) if tags.get(field)}
    origin = 'osm.tags'
    if not address and isinstance(item.get('address'), dict):
        address = {key: str(item['address'][field]).strip()[:180] for key, field in (
            ('street', 'road'), ('house_number', 'house_number')) if item['address'].get(field)}
        origin = 'nominatim.reverse.address'
    if address:
        result['map_address'] = {**address, 'provenance': origin,
                                 'source_url': source_url, 'scope': 'mapped_entry_only'}
    if tags.get('highway') and tags.get('name'):
        result['road_name'] = str(tags['name']).strip()[:180]
    geometry = osm_geometry_context(item, source_url=source_url)
    if geometry:
        result['map_geometry'] = geometry
    for key in ('boundary_distance_m', 'representative_distance_m', 'distance_provenance',
                'footprint_bearing_interval', 'camera_inside_footprint'):
        if key in item:
            result[key] = item[key]
    center = item.get('center') if isinstance(item.get('center'), dict) else {}
    raw_lat, raw_lon = item.get('lat', center.get('lat')), item.get('lon', center.get('lon'))
    if isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
        return result
    try:
        lat, lon = float(raw_lat), float(raw_lon)
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            result['map_coordinates'] = {'latitude': lat, 'longitude': lon,
                'provenance': coordinate_provenance or ('osm.center' if center else 'osm.position'), 'source_url': source_url}
    except (TypeError, ValueError, OverflowError):
        pass
    return result
