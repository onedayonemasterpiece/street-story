"""Small model context for an accepted physical subject; never an auth proof."""
from __future__ import annotations

from .identity_proof import accepted_identity, physical_scope



def _exif_angular_reference(story, manifest):
    """Reference diagonal from the selected photo; never an inferred yaw/range.

    A 35mm-equivalent EXIF diagonal is only a rough angular scale for comparing
    observed map outlines with SOURCE. It is not the actual cropped image FOV
    and never estimates distance, subject size or camera pointing direction.
    """
    import json
    import math
    camera = manifest.get('camera') or {}
    if camera.get('position_status') not in {'original_exif', 'owner_approximate'}:
        return None
    research = json.loads(story.get('research_json') or '{}')
    hints = (story.get('_camera_hints') or
        (research.get('visual_identity') or {}).get('camera_hints') or {})
    if not isinstance(hints, dict):
        return None
    degrees = hints.get('diagonal_fov_35mm_deg')
    if (isinstance(degrees, bool) or not isinstance(degrees, (int, float))
            or not math.isfinite(degrees) or not 0 < degrees < 180):
        return None
    return float(degrees)


def _outline_angular_scale(bearing_interval, diagonal_degrees):
    if diagonal_degrees is None or not isinstance(bearing_interval, (list, tuple)) or len(bearing_interval) != 3:
        return None
    import math
    span = bearing_interval[2]
    if (isinstance(span, bool) or not isinstance(span, (int, float))
            or not math.isfinite(span) or not 0 <= span <= 360):
        return None
    return round(span / diagonal_degrees, 3)


def physical_decision_context(story, candidates, manifest):
    """One literal row per received body; entrance addresses stay beside it.

    This is presentation of the received pool, never a nearest shortlist or a
    new address join. The broad MAP and full frozen label dictionary stay owned
    by the operation; geometry primitives remain reachable by their pointers.
    """
    from .identity_scene import scene_entries
    from .identity_source_selection import observed_address_context
    from .identity_map_context import osm_geometry_context
    from .identity_spatial_features import _local, _point
    import math
    entries = scene_entries(story, candidates)
    addresses = observed_address_context(story, entries)
    memberships = {item['physical_candidate_id']: item['address_entries']
        for item in addresses['building_address_memberships']}
    table = manifest.get('objects') or {}
    rows = {row.get('candidate_id'): row for row in (
        dict(zip(table.get('columns') or [], values)) for values in table.get('rows') or [])}
    result = []
    origin = _point(manifest.get('anchor') or story)
    detail = next((view for view in manifest.get('views') or []
        if view.get('name') in {'anchor_detail', 'nominated_detail'}), None)
    window = (detail or manifest.get('coverage') or {}).get('extent_east_north_m') or (
        manifest.get('coverage') or {}).get('view_extent_east_north_m')
    expanded_ids = set((detail or {}).get('target_candidate_ids') or [])
    reference_diagonal = _exif_angular_reference(story, manifest)
    for entry in entries:
        cid = entry['candidate_id']
        row = rows.get(cid, {})
        tags = {**(entry.get('tags') or {}), **(entry.get('map_object') or {}).get('tags', {})}
        if not (tags.get('building') not in {None, '', 'no'} or tags.get('building:part')):
            continue
        literal = []
        own = entry.get('map_address') or {}
        if own.get('street') or own.get('house_number'):
            literal.append([cid, own.get('city'), own.get('street'), own.get('house_number'), 'osm.subject_address'])
        for anchor in memberships.get(cid, []):
            address = anchor['address']
            literal.append([anchor['mapped_entry_id'], address.get('city'), address.get('street'),
                address.get('house_number'), 'osm_closed_way_node_membership'])
        sides = []
        geometry = entry.get('map_geometry') or osm_geometry_context(entry)
        for ri, ring in enumerate(geometry.get('rings') or []):
            points = [_point(point) for point in ring.get('points') or []]
            if origin and all(point is not None for point in points):
                for si, (a, b) in enumerate(zip(points, points[1:])):
                    first, second = _local(a, origin), _local(b, origin)
                    sides.append([ri, si, round(math.dist(first, second), 1),
                        [round(value, 1) for value in first], [round(value, 1) for value in second]])
        # A bounded primitive excerpt, never a building shortlist. Full rings
        # remain in the frozen snapshot; the omitted count is explicit.
        vertices = [point for side in sides for point in side[3:5]]
        in_window = bool(vertices) and (not window or (max(p[0] for p in vertices) >= window[0]
            and min(p[0] for p in vertices) <= window[2] and max(p[1] for p in vertices) >= window[1]
            and min(p[1] for p in vertices) <= window[3]))
        selected_sides = (sides if cid in expanded_ids else
            sorted(sides, key=lambda side: (-side[2], side[0], side[1]))[:4] if in_window else [])
        result.append([row.get('label'), cid, row.get('geometry_status'), row.get('boundary_distance_m'),
            row.get('bearing_start_end_span_degrees'), row.get('extent_east_north_m'),
            row.get('longest_observed_segments_m'), row.get('height_levels'), literal,
            tags.get('name'), row.get('contour_roles'), row.get('contours_complete'),
            selected_sides, len(sides) - len(selected_sides),
            _outline_angular_scale(row.get('bearing_start_end_span_degrees'), reference_diagonal)])
    return {'columns': ['label', 'candidate_id', 'contour_status', 'boundary_distance_m',
        'bearing_start_end_span_degrees', 'extent_east_north_m', 'longest_segments_m',
        'height_levels', 'literal_address_entries', 'observed_name', 'contour_roles', 'contours_complete',
        'observed_side_segments', 'omitted_side_count', 'outline_span_over_exif_diagonal'],
        'source_angular_reference': {
            'diagonal_fov_35mm_deg': reference_diagonal,
            'camera_position_status': (manifest.get('camera') or {}).get('position_status'),
            'policy': 'Each outline span / nominal 35mm-equivalent EXIF diagonal is an advisory ratio, '
                'NOT the fraction of image pixels, a distance estimate, visibility proof or a ranking. '
                'Only already observed OSM boundary vertices are used. Photo crop/calibration, '
                'which facade is visible, GPS uncertainty and horizontal yaw remain unknown. '
                'No body is removed when this measurement is unavailable.'},
        'segment_columns': ['ring_index', 'segment_index', 'length_m', 'start_east_north_m', 'end_east_north_m'],
        'primitive_excerpt_extent_east_north_m': window,
        'expansion': 'Request map_detail with exact received target_candidate_ids. All body rows remain reachable; '
            'only side excerpts use the displayed area, not identity eligibility or nearest-K. '
            'Explicit requested bodies expose all observed sides, including short setbacks.',
        'rows': result, 'address_columns': ['entry_id', 'city', 'street', 'house_number', 'provenance'],
        'received_body_count': len(result),
        'policy': 'Every received physical body remains reachable, including far telephoto subjects. '
            'Rows have neutral MAP labels, never relevance ranks. Entrance numbers remain distinct; '
            'literal membership does not infer one postal address or one entrance. Null/empty geometry, '
            'levels, city or addresses are unknown. A visible side is nominated by an actual segment; '
            'no facade, camera yaw or obstruction is inferred from map north. Use map_detail with exact '
            'labels for a needed contour/pose relation; the full pool is retained, no nearest-K exclusion.'}


def compact_physical_identity(identity, *, photo_sha256=None, generation=None, control_revision=None):
    if not isinstance(identity, dict):
        return {}
    # A prior compact DTO is presentation only. Actual authorization always
    # evaluates the full frozen identity before constructing this context.
    projected = 'physical_identity_accepted' in identity and 'geometry_proof' not in identity
    accepted = bool(identity.get('physical_identity_accepted')) if projected else accepted_identity(
        identity, photo_sha256=photo_sha256, generation=generation, control_revision=control_revision)
    result = {}
    for key in ('status', 'candidate_id', 'candidate_name', 'canonical_name', 'locality', 'country',
            'proof_kind', 'photo_sha256', 'wikipedia_url', 'wikidata', 'osm_id', 'candidate_url'):
        value = identity.get(key)
        limit = 2000 if key.endswith('_url') else 300
        if isinstance(value, str) and len(value) <= limit:
            result[key] = value
    for key in ('generation', 'control_revision', 'visual_reference_verified'):
        if key in identity:
            result[key] = identity[key]
    scope = identity.get('physical_scope') if projected else physical_scope(identity)
    if isinstance(scope, str) and scope:
        result['physical_scope'] = scope[:600]  # Preserve either proof contract's bounded literal physical scope.
    result['physical_identity_accepted'] = accepted
    result['observations'] = [str(value)[:300] for value in (identity.get('observations') or [])[:3]]
    if projected:
        aliases = identity.get('subject_alias_candidate_ids') or [identity.get('candidate_id')]
        omitted = int(identity.get('subject_aliases_omitted_count') or 0)
    else:
        from .identity_subject_binding import subject_aliases
        aliases = sorted(subject_aliases(identity.get('candidates') or []).get(
            identity.get('candidate_id'), {identity.get('candidate_id')}))
        omitted = 0
    valid = [value for value in aliases if isinstance(value, str) and value and len(value) <= 100]
    result['subject_alias_candidate_ids'] = valid[:32]
    result['subject_aliases_omitted_count'] = omitted + len(aliases) - len(result['subject_alias_candidate_ids'])
    return result
