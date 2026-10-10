"""Allowlisted structured map anchors; no address or physical identity inference."""
from __future__ import annotations

import math


def expand_disjoint_building_components(elements: list[dict]) -> list[dict]:
    """Expose observed closed member ways, never alias separate exterior bodies.

    Open outer fragments are joined mechanically to count complete rings. A
    fragment is never promoted as a whole building. Missing/ambiguous geometry
    remains ordinary context rather than acquiring invented component IDs.
    """
    output = {((item.get('osm_type') or item.get('type')), str(item.get('osm_id') or item.get('id'))): dict(item)
              for item in elements if isinstance(item, dict)}
    def point_in_ring(point, ring):
        x, y = point
        inside = False
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            if (ay > y) != (by > y) and ax + (bx - ax) * (y - ay) / (by - ay) > x:
                inside = not inside
        return inside
    def overlaps(first, second):
        if (max(p[0] for p in first) < min(p[0] for p in second)
                or max(p[0] for p in second) < min(p[0] for p in first)
                or max(p[1] for p in first) < min(p[1] for p in second)
                or max(p[1] for p in second) < min(p[1] for p in first)):
            return False
        def orient(a, b, c):
            return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        def on_segment(a, b, c):
            return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        for a, b in zip(first, first[1:]):
            for c, d in zip(second, second[1:]):
                ac, ad, ca, cb = orient(a,b,c), orient(a,b,d), orient(c,d,a), orient(c,d,b)
                if ((ac*ad < 0 and ca*cb < 0) or (ac == 0 and on_segment(a,b,c))
                        or (ad == 0 and on_segment(a,b,d)) or (ca == 0 and on_segment(c,d,a))
                        or (cb == 0 and on_segment(c,d,b))):
                    return True
        return point_in_ring(first[0], second) or point_in_ring(second[0], first)
    for item in elements:
        if not isinstance(item, dict):
            continue
        tags = item.get('tags') or {}
        if ((item.get('osm_type') or item.get('type')) != 'relation'
                or tags.get('type') != 'multipolygon' or tags.get('building') in {None, '', 'no'}):
            continue
        members = [m for m in item.get('members') or [] if isinstance(m, dict)
                   and m.get('type') == 'way' and m.get('role') in {'outer', ''}]
        edges = []
        for member in members:
            lines = osm_geometry_context({'geometry': member.get('geometry')}).get('lines') or []
            ref = member.get('ref')
            if not lines or not isinstance(ref, int) or isinstance(ref, bool) or ref <= 0:
                break
            edges.append((member, lines[0]))
        if len(edges) != len(members) or len(edges) < 2:
            continue
        # Join only a complete, unambiguous outer chain; dangling edges and
        # branching endpoints cannot establish a physical component.
        pending = list(edges)
        rings = []
        while pending:
            member, line = pending.pop(0)
            refs = [member['ref']]
            line = list(line)
            while line[0] != line[-1]:
                continuations = [(i, m, pts) for i, (m, pts) in enumerate(pending)
                                 if pts[0] == line[-1] or pts[-1] == line[-1]]
                if len(continuations) != 1:
                    break
                index, following, points = continuations[0]
                pending.pop(index)
                line.extend((points if points[0] == line[-1] else list(reversed(points)))[1:])
                refs.append(following['ref'])
            if line[0] != line[-1] or len(line) < 4:
                rings = []
                break
            rings.append((refs, line))
        if len(rings) < 2:
            continue
        planar = [[(p['lon'], p['lat']) for p in line] for _refs, line in rings]
        if any(overlaps(first, second) for i, first in enumerate(planar) for second in planar[i+1:]):
            continue
        parent_id = item.get('osm_id') or item.get('id')
        parent_cid = f'osm:relation:{parent_id}'
        parent_url = f'https://www.openstreetmap.org/relation/{parent_id}'
        exact_ids = [f'osm:way:{refs[0]}' for refs, _line in rings if len(refs) == 1]
        output[('relation', str(parent_id))].update(identity_eligible=False,
            identity_ineligible_reason='disjoint_outer_building_components',
            identity_role='multi_component_building_context',
            physical_components={'proof': 'osm_disjoint_outer_geometry', 'source_url': parent_url,
                'component_count': len(rings), 'exact_member_candidate_ids': exact_ids,
                'outer_component_way_ids': [refs for refs, _line in rings]})
        for refs, line in rings:
            if len(refs) != 1:
                continue  # No single observed way ID denotes this joined ring.
            member_id = refs[0]
            member = next(m for m, _pts in edges if m['ref'] == member_id)
            key = ('way', str(member_id))
            existing = output.get(key) or {}
            own_tags = dict(existing.get('tags') or member.get('tags') or {})
            inherited = ('building' not in own_tags or
                (existing.get('physical_component') or {}).get('inherited_building_tag') is True)
            member_tags = {**({'building': tags['building']} if inherited else {}), **own_tags}
            output[key] = {**existing, 'type': 'way', 'id': member_id, 'tags': member_tags,
                'geometry': line, 'selection_bucket': existing.get('selection_bucket') or item.get('selection_bucket'),
                'physical_component': {'proof': 'osm_disjoint_outer_closed_way_membership',
                    'parent_candidate_id': parent_cid, 'parent_source_url': parent_url,
                    'member_candidate_id': f'osm:way:{member_id}', 'role': 'outer',
                    'inherited_building_tag': inherited},
                'parent_relation_context': {'candidate_id': parent_cid, 'source_url': parent_url,
                    'scope': 'parent_relation_only', 'tags': {key: tags[key] for key in
                        ('name', 'building', 'addr:street', 'addr:housenumber') if key in tags}}}
            if member_tags.get('building') in {'', 'no'}:
                output[key].update(identity_eligible=False,
                    identity_ineligible_reason='outer_member_explicitly_not_building')
    return list(output.values())


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
    lines, fragments = [], []
    line = points(item.get('geometry'))
    if line:
        lines.append(line)
        fragments.append(('outer', line, None))
    for member in item.get('members') or []:
        if isinstance(member, dict) and member.get('type') == 'way':
            line = points(member.get('geometry'))
            if line:
                lines.append(line)
                fragments.append((member.get('role') or 'outer', line, member.get('ref')))
    rings = []
    for role in dict.fromkeys(role for role, _line, _ref in fragments):
        pending = [(list(line), [ref] if ref else []) for r, line, ref in fragments if r == role]
        while pending:
            chain, refs = pending.pop(0)
            while chain[0] != chain[-1]:
                next_edges = [(i, edge, ids) for i, (edge, ids) in enumerate(pending)
                    if edge[0] == chain[-1] or edge[-1] == chain[-1]]
                if len(next_edges) != 1:
                    break
                index, edge, ids = next_edges[0]
                pending.pop(index)
                chain.extend((edge if edge[0] == chain[-1] else list(reversed(edge)))[1:])
                refs.extend(ids)
            rings.append({'role': role, 'points': chain, 'closed': chain[0] == chain[-1],
                'member_candidate_ids': [f'osm:way:{ref}' for ref in refs]})
    return {'lines': lines, 'rings': rings, 'provenance': 'osm.observed_geometry', 'source_url': source_url,
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
        for x, y in line:
            bearings.append(math.degrees(math.atan2(x, y)) % 360)
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = min(1.0, max(0.0, -(ax * dx + ay * dy) / length2)) if length2 else 0.0
            nearest = min(nearest, math.hypot(ax + t * dx, ay + t * dy))
    for ring in geometry.get('rings') or []:
        if not ring.get('closed'):
            continue
        points = [(((p['lon'] - lon + 180) % 360 - 180) * scale * cos_lat,
                   (p['lat'] - lat) * scale) for p in ring['points']]
        in_ring = False
        for (ax, ay), (bx, by) in zip(points,points[1:]):
            if (ay > 0) != (by > 0) and ax + (bx - ax) * (-ay) / (by - ay) > 0:
                in_ring = not in_ring
        if in_ring:
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
    for key in ('physical_components', 'physical_component', 'parent_relation_context',
                'identity_eligible', 'identity_ineligible_reason', 'identity_role'):
        if key in item:
            result[key] = item[key]
    if osm_object:
        result['candidate_id'] = f'osm:{kind}:{object_id}'
    mapped_tags = {key: str(tags[key])[:180] for key in (
        'name', 'building', 'building:part', 'entrance', 'amenity', 'shop', 'office', 'tourism',
        'historic', 'highway', 'man_made', 'landuse', 'leisure', 'place',
        'height', 'building:levels', 'roof:levels') if tags.get(key)}
    mapped_kind = {key: str(item[key])[:100] for key in ('category', 'class', 'addresstype') if item.get(key)}
    if item.get('osm_type') and item.get('type'):
        mapped_kind['type'] = str(item['type'])[:100]
    if mapped_tags or mapped_kind:
        result['map_object'] = {**mapped_kind, 'tags': mapped_tags,
            'provenance': 'osm.tags' if mapped_tags else 'nominatim.reverse',
            'source_url': source_url, 'scope': 'mapped_entry_only'}
        component = item.get('physical_component') or {}
        if component.get('inherited_building_tag') is True:
            result['map_object']['provenance'] = 'osm.relation_outer_geometry'
            result['map_object']['physical_component'] = component
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
