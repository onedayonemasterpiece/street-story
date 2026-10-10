"""Small observed-vector checks for explicit model hypotheses, not recognition.

No function selects an identity, assumes a frontage or invents a camera pose.
Inputs name exact received OSM IDs and optional explicit pose/side scenarios.
"""
from __future__ import annotations

import math

from .identity_map_context import osm_geometry_context


def _index(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _point(value):
    if not isinstance(value, dict):
        return None
    lat, lon = value.get('latitude', value.get('lat')), value.get('longitude', value.get('lon'))
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError, OverflowError):
        return None
    return (lat, lon) if math.isfinite(lat+lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _entries(story, candidates):
    from .identity_scene import scene_entries
    return {item['candidate_id']: item for item in scene_entries(story, candidates)}


def geometry_feature(story, candidates, reference):
    """Resolve one exact observed feature; invalid/missing references return None.

    Contour indices refer to map_geometry.rings and the transmitted contour_roles.
    A segment uses consecutive observed vertices; closure is never fabricated.
    Attribute references require an actual literal OSM tag, not missing values.
    """
    if not isinstance(reference, dict):
        return None
    cid, requested_kind = reference.get('candidate_id'), reference.get('kind')
    if not isinstance(cid, str) or not isinstance(requested_kind, str):
        return None
    kind = {'footprint':'contour','component':'contour','tag':'attribute','entry':'point'}.get(requested_kind, requested_kind)
    entry = _entries(story, candidates).get(cid)
    if not entry:
        return None
    tags = {**(entry.get('tags') or {}), **(entry.get('map_object') or {}).get('tags', {})}
    geometry = entry.get('map_geometry') or osm_geometry_context(entry)
    result = {'candidate_id': cid, 'kind': requested_kind, 'provenance': 'osm.observed_geometry',
        'source_url': (entry.get('map_object') or {}).get('source_url') or entry.get('source_url')}
    if kind == 'attribute':
        key = reference.get('key') or reference.get('tag_key')
        if not isinstance(key, str) or key not in tags or tags[key] is None or tags[key] == '':
            return None
        return {**result, 'key': key, 'value': tags[key], 'provenance':
            (entry.get('map_object') or {}).get('provenance') or 'osm.tags'}
    if kind == 'point':
        if requested_kind == 'entry' and (not cid.startswith('osm:node:') or not (
                tags.get('entrance') not in {None,'','no'} or tags.get('addr:housenumber') or
                (entry.get('map_address') or {}).get('house_number'))):
            return None
        point = _point(entry.get('map_coordinates') or entry.get('center') or entry)
        return {**result, 'coordinates': [{'lat': point[0], 'lon': point[1]}],
            'provenance': (entry.get('map_coordinates') or {}).get('provenance') or 'osm.position'} if point else None
    index_key = 'line_index' if kind == 'road_axis' else 'ring_index'
    index = _index(0 if reference.get(index_key) is None else reference[index_key])
    if index is None:
        return None
    if kind == 'road_axis':
        if not tags.get('highway'):
            return None
        lines = geometry.get('lines') or []
        if index >= len(lines):
            return None
        points, role, closed = lines[index], 'observed_road_line', False
    elif kind in {'contour', 'segment'}:
        rings = geometry.get('rings') or []
        if index >= len(rings):
            return None
        ring = rings[index]
        points, role, closed = ring.get('points') or [], ring.get('role'), ring.get('closed') is True
    else:
        return None
    if len(points) < 2 or any(_point(point) is None for point in points):
        return None
    result.update({index_key: index, 'role': role, 'closed': closed})
    if requested_kind == 'component':
        result['component_membership'] = entry.get('physical_component') or entry.get('physical_components')
    if kind in {'segment', 'road_axis'}:
        segment = _index(reference.get('segment_index', 0))
        if segment is None or segment+1 >= len(points):
            return None
        result['segment_index'] = segment
        points = points[segment:segment+2]
    return {**result, 'coordinates': [dict(point) for point in points]}


def _local(point, origin):
    scale = 6_371_000*math.pi/180
    return (((point[1]-origin[1]+180) % 360-180)*scale*math.cos(math.radians(origin[0])),
        (point[0]-origin[0])*scale)


def _bearing(point):
    return math.degrees(math.atan2(point[0], point[1])) % 360


def _distance(point, a, b):
    delta = (b[0]-a[0], b[1]-a[1])
    norm = delta[0]**2+delta[1]**2
    fraction = max(0., min(1., ((point[0]-a[0])*delta[0]+(point[1]-a[1])*delta[1])/norm)) if norm else 0.
    return math.hypot(point[0]-a[0]-fraction*delta[0], point[1]-a[1]-fraction*delta[1])


def _intersection(a, b, c, d):
    x, y = b[0]-a[0], b[1]-a[1]
    u, v = d[0]-c[0], d[1]-c[1]
    cross = x*v-y*u
    if abs(cross) < 1e-12:
        return None
    p, q = c[0]-a[0], c[1]-a[1]
    return ((p*v-q*u)/cross, (p*y-q*x)/cross)


def measure_spatial_relations(story, candidates, candidate_ids, *, pose=None, front_segments=(), street_axis=None):
    """Measure at most six nominated objects and one explicit pose/axis scenario.

    Nominal approximate coordinates and offsets are scenarios, never GPS accuracy.
    Boundary gaps are OSM-vector distances, not evidence of a physical passage.
    Front sides are nominated segment pairs; their signed normal is explicit.
    """
    if not isinstance(candidate_ids, (list, tuple)) or not 1 <= len(candidate_ids) <= 6:
        return None
    entries = _entries(story, candidates)
    if any(not isinstance(cid,str) for cid in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids) or any(cid not in entries for cid in candidate_ids):
        return None
    origin = _point(story)
    if not origin:
        return None
    if pose is not None and not isinstance(pose,dict):
        return None
    pose = pose or {}
    east, north = pose.get('east_m', 0), pose.get('north_m', 0)
    yaw = pose.get('heading_degrees')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (east, north)):
        return None
    if yaw is not None and (isinstance(yaw, bool) or not isinstance(yaw, (int, float)) or not math.isfinite(yaw)):
        return None
    camera = (east, north)
    outlines, objects = {}, []
    for cid in candidate_ids:
        entry = entries[cid]
        geometry = entry.get('map_geometry') or osm_geometry_context(entry)
        lines = [[_local(_point(p), origin) for p in line] for line in geometry.get('lines') or []
            if len(line) >= 2 and all(_point(p) for p in line)]
        outlines[cid] = [(a, b) for line in lines for a, b in zip(line,line[1:])]
        position = _point(entry.get('map_coordinates') or entry.get('center') or entry)
        center = _local(position, origin) if position else None
        delta = (center[0]-east, center[1]-north) if center else None
        bearing = _bearing(delta) if delta else None
        relative = ((bearing-yaw+180) % 360-180) if bearing is not None and yaw is not None else None
        objects.append({'candidate_id': cid, 'representative_bearing_degrees': round(bearing, 2) if bearing is not None else None,
            'relative_bearing_degrees': round(relative, 2) if relative is not None else None,
            'perspective_x_tangent': round(math.tan(math.radians(relative)), 4) if relative is not None and abs(relative) < 90 else None,
            'boundary_distance_m': round(min(_distance(camera,a,b) for a,b in outlines[cid]),2) if outlines[cid] else None})
    gaps = []
    for index, first in enumerate(candidate_ids):
        for second in candidate_ids[index+1:]:
            distances = []
            shared_vertex = False
            for a,b in outlines[first]:
                for c,d in outlines[second]:
                    shared_vertex |= any(p == q for p in (a,b) for q in (c,d))
                    crossing = _intersection(a,b,c,d)
                    distances.append(0. if crossing and all(0 <= v <= 1 for v in crossing)
                        else min(_distance(a,c,d),_distance(b,c,d),_distance(c,a,b),_distance(d,a,b)))
            gaps.append({'candidate_ids':[first,second],'observed_boundary_gap_m':round(min(distances),2) if distances else None,
                'shared_observed_vertex':shared_vertex, 'physical_passage_verified':False})
    fronts = []
    if not isinstance(front_segments,(tuple,list)) or len(front_segments) > 6:
        return None
    for pair in front_segments:
        refs = [pair.get('first'), pair.get('second')] if isinstance(pair,dict) else []
        features = [geometry_feature(story,candidates,ref) for ref in refs]
        if len(features) != 2 or any(not f or f['candidate_id'] not in candidate_ids or len(f['coordinates']) != 2 for f in features):
            return None
        a,b = [_local(_point(p),origin) for p in features[0]['coordinates']]
        c,d = [_local(_point(p),origin) for p in features[1]['coordinates']]
        length = math.dist(a,b)
        if not length:
            return None
        normal = ((b[1]-a[1])/length, -(b[0]-a[0])/length)
        shift = ((c[0]+d[0]-a[0]-b[0])/2, (c[1]+d[1]-a[1]-b[1])/2)
        angular_spans = [abs((_bearing((start[0]-east,start[1]-north))-
            _bearing((end[0]-east,end[1]-north))+180)%360-180) for start,end in ((a,b),(c,d))]
        fronts.append({'references':refs,'front_length_m':round(length,2),
            'second_length_m':round(math.dist(c,d),2),'normal_bearing_degrees':round(_bearing(normal),2),
            'normal_basis':'right_of_first_directed_observed_segment',
            'signed_front_setback_m':round(sum(x*y for x,y in zip(shift,normal)),2),
            'first_angular_span_degrees':round(angular_spans[0],2),
            'second_angular_span_degrees':round(angular_spans[1],2),
            'first_to_second_angular_ratio':round(angular_spans[0]/angular_spans[1],3) if angular_spans[1] else None,
            'side_angle_difference_degrees':round((_bearing((d[0]-c[0],d[1]-c[1]))-_bearing((b[0]-a[0],b[1]-a[1]))+180)%360-180,2)})
    intersections = []
    axis_summary = None
    if street_axis:
        axis = geometry_feature(story,candidates,street_axis)
        if not axis or axis['kind'] != 'road_axis' or yaw is None:
            return None
        radians = math.radians(yaw)
        end = (east+math.sin(radians),north+math.cos(radians))
        a,b = [_local(_point(p),origin) for p in axis['coordinates']]
        axis_bearing = _bearing((b[0]-a[0],b[1]-a[1]))
        axis_summary = {'observed_segment_bearing_degrees':round(axis_bearing,2),
            'chosen_ray_heading_degrees':yaw,
            'heading_difference_degrees':round((yaw-axis_bearing+180)%360-180,2),
            'camera_to_observed_axis_segment_m':round(_distance(camera,a,b),2)}
        # Coverage is the complete received physical pool, not the nominated shortlist.
        for cid,entry in entries.items():
            tags = {**(entry.get('tags') or {}), **(entry.get('map_object') or {}).get('tags',{})}
            if tags.get('building') in {None,'','no'} and not tags.get('building:part'):
                continue
            geometry = entry.get('map_geometry') or osm_geometry_context(entry)
            hits = []
            for line in geometry.get('lines') or []:
                points = [_point(p) for p in line]
                if not points or any(p is None for p in points):
                    continue
                local = [_local(p,origin) for p in points]
                for a,b in zip(local,local[1:]):
                    hit = _intersection(camera,end,a,b)
                    if hit and hit[0] >= 0 and 0 <= hit[1] <= 1:
                        hits.append(hit[0])
            if hits:
                intersections.append({'candidate_id':cid,'ray_distance_m':round(min(hits),2)})
        intersections.sort(key=lambda item:(item['ray_distance_m'],item['candidate_id']))
    from .identity_scene import scene_camera_context
    return {'camera':scene_camera_context(story), 'pose_scenario':{'east_m':east,'north_m':north,
        'heading_degrees':yaw,'provenance':'explicit_scenario' if pose else 'nominal_input',
        'is_measured_camera_pose':False},'objects':objects,
        'left_to_right_order':[item['candidate_id'] for item in sorted(objects,key=lambda row:row['relative_bearing_degrees'])]
            if yaw is not None and all(item['relative_bearing_degrees'] is not None for item in objects) else None,
        'boundary_gaps':gaps,'front_segments':fronts,'street_axis_reference':street_axis,'street_axis_scenario':axis_summary,
        'street_axis_ray_intersections':intersections,
        'policy':'Measured OSM relations for nominated hypotheses only. Nominal/shifted poses are assumptions, '
            'not GPS accuracy. Relative bearings define left/right only within the chosen front view; '
            'ray hits are conditional on the chosen axis/pose and received map coverage. '
            'Missing geometry never excludes an alternative; small OSM gaps do not verify physical passages.'}
