"""Source-independent OSM spatial choices for an LLM-first G method.

The host enumerates factual observed alternatives. Vision decides which, if
any, are visible in SOURCE. No expected building, address, historical title,
fitted yaw, guessed height or mandatory two-corner reconstruction is used.
Initial labels include EVERY received physical body, while bounded detailed
options are presentation hints. A previous MODEL-nominated group can expand
its own choices without silently dropping the rest of the original map.
"""
from __future__ import annotations

import hashlib
import json
import math

VERSION = 'street_story.g_spatial_options.v3'


def _round(value, digits=2):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return round(value, digits)
    return None


def _body_rows(physical):
    columns = physical.get('columns') or []
    return [dict(zip(columns, values)) for values in physical.get('rows') or []]


def _map_labels(manifest):
    objects = manifest.get('objects') or {}
    mapping = {}
    for values in objects.get('rows') or []:
        row = dict(zip(objects.get('columns') or [], values))
        if type(row.get('label')) is int and isinstance(row.get('candidate_id'), str):
            mapping[row['candidate_id']] = row['label']
    return mapping


def _wall_segments(story, entry):
    from .identity_map_context import osm_geometry_context
    from .identity_spatial_features import _point, _local
    geometry = entry.get('map_geometry') or osm_geometry_context(entry)
    origin = _point(story)
    if origin is None:
        return [], {}
    sides = []
    closed = {}
    for ring_index, ring in enumerate(geometry.get('rings') or []):
        if ring.get('role') not in (None, 'outer'):
            continue
        points = ring.get('points') or []
        if len(points) < 3 or any(_point(p) is None for p in points):
            continue
        if ring.get('closed') is True and _point(points[0]) == _point(points[-1]):
            closed[ring_index] = len(points) - 2
        for segment_index, (first, second) in enumerate(zip(points, points[1:])):
            a, b = _local(_point(first), origin), _local(_point(second), origin)
            length = math.dist(a, b)
            if length >= 0.25:
                sides.append([ring_index, segment_index, _round(length),
                    [_round(a[0]), _round(a[1])], [_round(b[0]), _round(b[1])]])
    return sides, closed


def spatial_option_catalog(story, candidates, manifest, physical, *,
                           focus_candidate_ids=(), max_initial_bodies=18):
    from .identity_scene import scene_entries
    from .identity_corner_context import observed_connected_pairs
    from .identity_spatial_features import measure_spatial_relations

    labels = _map_labels(manifest)
    body = {row['candidate_id']: row for row in _body_rows(physical)
            if isinstance(row.get('candidate_id'), str)}
    if not body or any(cid not in labels or labels[cid] != row.get('label')
                       for cid, row in body.items()):
        raise ValueError('neutral_body_labels_missing_or_inconsistent')
    focus = set(focus_candidate_ids)
    if any(cid not in body for cid in focus):
        raise ValueError('detail_ref_not_in_received_map')
    # Sort only what is *presented* in option detail, not candidates eligible
    # for recognition. Every physical label remains in the full index and map.
    def display_order(cid):
        row = body[cid]
        sector = row.get('bearing_start_end_span_degrees')
        span = sector[2] if isinstance(sector, (tuple, list)) and len(sector) == 3 else None
        span = span if isinstance(span, (float, int)) and not isinstance(span, bool) else -1
        dist = row.get('boundary_distance_m')
        dist = dist if isinstance(dist, (float, int)) and not isinstance(dist, bool) else 999999
        return (-span, dist, row['label'])
    detailed = focus if focus else set(sorted(body, key=display_order)[:max_initial_bodies])
    cols = physical.get('plan_morphology_columns') or []
    index = []
    for row in sorted(body.values(), key=lambda r: r['label']):
        shape = dict(zip(cols, row.get('plan_morphology') or []))
        sector = row.get('bearing_start_end_span_degrees')
        span = sector[2] if isinstance(sector, (tuple, list)) and len(sector) == 3 else None
        index.append([row['label'], _round(row.get('boundary_distance_m'), 1),
            _round(span, 1), _round(shape.get('long_axis_m'), 1),
            _round(shape.get('short_axis_m'), 1), _round(shape.get('footprint_elongation')),
            _round(shape.get('explicit_height_m'), 1), _round(shape.get('observed_building_levels'), 1),
            bool(shape.get('status') == 'observed_closed_outer'), row['candidate_id'] in detailed])

    entries = {item.get('candidate_id'): item for item in scene_entries(story, candidates)}
    options = {}
    def add(option_id, record):
        if option_id in options:
            raise ValueError('duplicate_derived_osm_option')
        options[option_id] = record

    for cid in sorted(detailed, key=lambda c: labels[c]):
        row, entry = body[cid], entries.get(cid)
        if entry is None:
            continue
        label = labels[cid]
        shape = dict(zip(cols, row.get('plan_morphology') or []))
        if shape.get('status') == 'observed_closed_outer':
            add(f'S{label}', {'kind': 'plan_shape', 'body_label': label,
                'plan_long_m': _round(shape.get('long_axis_m')),
                'plan_short_m': _round(shape.get('short_axis_m')),
                'plan_area_m2': _round(shape.get('footprint_area_m2')),
                'elongation': _round(shape.get('footprint_elongation')),
                'height_m_if_mapped': _round(shape.get('explicit_height_m')),
                'levels_if_mapped': _round(shape.get('observed_building_levels'))})
        sides, closed = _wall_segments(story, entry)
        exterior = {tuple(x) for x in row.get('nominal_camera_exterior_side_indices') or []}
        interior = {tuple(x) for x in row.get('nominal_camera_inward_side_indices') or []}
        def face(ri, si):
            return ('nominal_exterior' if (ri, si) in exterior else
                    'nominal_interior' if (ri, si) in interior else 'unknown')
        for ri, si, length, _a, _b in sorted(
                sides, key=lambda s: (-s[2], s[0], s[1]))[:6]:
            add(f'F{label}.{ri}.{si}', {'kind': 'single_frontage',
                'body_label': label, 'ring_index': ri, 'segment_index': si,
                'actual_wall_length_m': length,
                'camera_side_advisory': face(ri, si)})
        pairs, _ = observed_connected_pairs(sides, max_pairs=max(8, len(sides)*2),
                                             closed_rings=closed)
        selected = 0
        for ri, first, second, turn in pairs:
            angle = abs(turn)
            if min(angle, abs(180-angle)) < 12:
                continue
            add(f'C{label}.{ri}.{first}.{second}', {
                'kind': 'observed_corner', 'body_label': label,
                'ring_index': ri, 'first_side': first, 'second_side': second,
                'observed_map_turn_deg': turn,
                'camera_side_advisory': [face(ri, first), face(ri, second)]})
            selected += 1
            if selected >= 9:
                break

    # Both street directions are displayed unranked by photographed matching.
    roads = (physical.get('bidirectional_road_axis_cues') or {}).get('rows') or []
    for road in roads:
        road_label = labels.get(road[0])
        if type(road_label) is not int:
            continue
        for direction, item in enumerate(road[6][:2]):
            hits = [x for x in item[1] if x[1] in body]
            if not hits:
                continue
            add(f'R{road_label}.{direction}', {'kind': 'road_axis_direction',
                'road_label': road_label, 'direction': direction,
                'observed_heading_deg_not_EXIF': _round(item[0], 1),
                'first_plan_hit_body_label': hits[0][0],
                'first_plan_hit_m': _round(hits[0][2]),
                'next_plan_hit_body_labels': [h[0] for h in hits[1:3]]})

    # A pair option is generated only when these bodies were named by a
    # previous MODEL response, not by manually supplied expected identity.
    if focus:
        group = sorted(focus, key=lambda cid: labels[cid])
        for i, cid in enumerate(group):
            for alt in group[i+1:]:
                measured = measure_spatial_relations(story, candidates, [cid, alt])
                gaps = (measured or {}).get('boundary_gaps') or []
                gap = gaps[0].get('observed_boundary_gap_m') if gaps else None
                if _round(gap) is not None:
                    add(f'P{labels[cid]}.{labels[alt]}', {
                        'kind': 'physical_pair', 'body_labels': [labels[cid], labels[alt]],
                        'observed_boundary_gap_m': _round(gap),
                        'not_a_verified_passage': True})

    return {'version': VERSION,'map_sha256': manifest.get('image_sha256'),
        'camera_basis': (manifest.get('camera') or {}).get('position_status'),
        'physical_body_count': len(body),'all_received_index_columns': [
            'body_label','nominal_boundary_m','plan_angular_span_deg','plan_long_m',
            'plan_short_m','elongation','mapped_height_m','mapped_levels',
            'complete_plan','detail_expanded'],
        'all_received_physical_bodies': index,
        'expanded_labels': sorted(labels[cid] for cid in detailed),
        'options': options,
        'private_label_to_osm_id': {str(label):cid for cid,label in labels.items()},
        'policy': 'All original observed bodies remain available; expanded options are '
          'only a display subset. Model selects real visible SOURCE pattern or UNKNOWN, '
          'never a fabricated yaw or a map geometry not in options. Plan is 2D, '
          'corner visibility depends on camera position and 3D occlusion, '
          'street ray direction is NOT measured camera heading.'}


def option_digest(packet):
    return hashlib.sha256(json.dumps(packet,ensure_ascii=False,sort_keys=True,
         separators=(',',':')).encode()).hexdigest()


def for_vision(packet):
    """No direct OSM string IDs in model labels; host retains bijection."""
    return {k:v for k,v in packet.items() if k!='private_label_to_osm_id'}
