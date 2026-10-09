"""Lean image-first G observations, distinct from the strict physical proof.

The visual model sees ORIGINAL SOURCE and neutral MAP, but no expected ID or
publisher/ref imagery. It may nominate a received physical building and a
street direction or joined side. The host verifies only literal pointers and
observed OSM relations. This is NOT a geometry identity certificate; only the
existing strict freeze_geometry_proof or an independently validated successor
contract may authorize a physical identity or POI facts.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator

CONTRACT = 'street_story.geometry_nomination.v1'


def visual_geometry_nomination_schema():
    """Small provider-compatible response schema; no fake numeric camera pose.

    Avoid conditional JSON-schema (if/then, anyOf) and deeply nested canonical
    provider schema in an instruction. The host separately checks each pointer,
    source-map binding and actual OSM rays/corners after the one paid response.
    """
    statement = {'type': 'string', 'maxLength': 300}
    item = {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': [
            'corner', 'frontage_sequence', 'street_termination', 'setback',
            'neighbour_order', 'other']},
        'source_observation': statement,
        'map_body_labels': {'type': 'array', 'items': {'type': 'integer'},
            'maxItems': 3},
        'road_candidate_id': {'type': 'string'},
        'road_direction_index': {'type': 'integer'},
        'segment_ring_index': {'type': 'integer'},
        'first_segment_index': {'type': 'integer'},
        'second_segment_index': {'type': 'integer'}},
        'required': ['kind', 'source_observation', 'map_body_labels',
            'road_candidate_id', 'road_direction_index', 'segment_ring_index',
            'first_segment_index', 'second_segment_index']}
    return {'type': 'object', 'properties': {
        'decision': {'type': 'string', 'enum': ['nominated', 'uncertain']},
        'candidate_label': {'type': 'integer'},
        'source_observations': {'type': 'array', 'items': statement,
            'maxItems': 5},
        'source_horizontal_extent': {'type': 'string',
            'enum': ['broad', 'medium', 'narrow', 'unknown']},
        'spatial_relations': {'type': 'array', 'items': item, 'maxItems': 3},
        'alternative_labels': {'type': 'array', 'items': {'type': 'integer'},
            'maxItems': 5},
        'uncertainties': {'type': 'array', 'items': statement, 'maxItems': 5}},
        'required': ['decision', 'candidate_label', 'source_observations',
            'source_horizontal_extent', 'spatial_relations', 'alternative_labels', 'uncertainties']}


def check_visual_geometry_nomination(response, scene_manifest, physical_context, *,
                                     source_map_bound=True):
    """Return a bounded, strictly NON-authorizing conditional G hypothesis.

    Response's labels are untrusted; exact dictionary joins are the sole source
    of physical IDs. Uncertain without candidate is valid; no property implies
    an identity, verified camera yaw, 3D occlusion or measured GPS error.
    """
    if not source_map_bound:
        return {'status': 'invalid', 'candidate_id': None,
                'authorizes_identity': False, 'reason_codes': ['unbound_source_map']}
    errors = list(Draft202012Validator(
        visual_geometry_nomination_schema()).iter_errors(response))
    if errors:
        return {'status': 'invalid', 'candidate_id': None,
                'authorizes_identity': False, 'reason_codes': [
                    'invalid_structured_visual_response']}
    physical_rows = [dict(zip(physical_context.get('columns') or [], row))
        for row in physical_context.get('rows') or []]
    by_label = {r['label']:r for r in physical_rows if type(r.get('label')) is int}
    objects = scene_manifest.get('objects') or {}
    labels = {d['label']: d['candidate_id'] for row in objects.get('rows') or []
        for d in [dict(zip(objects.get('columns') or [], row))]
        if type(d.get('label')) is int and isinstance(d.get('candidate_id'), str)}
    reasons = []
    index = response['candidate_label']
    if response['decision'] == 'uncertain' and index == 0:
        return {'status': 'uncertain', 'candidate_id': None,
            'authorizes_identity': False, 'reason_codes': ['model_declared_uncertain']}
    subject = by_label.get(index)
    if subject is None or labels.get(index) != subject['candidate_id']:
        return {'status': 'invalid', 'candidate_id': None,
            'authorizes_identity': False, 'reason_codes': [
                'nomination_is_not_received_physical_body']}
    cid = subject['candidate_id']
    kinds = []
    for relation in response['spatial_relations']:
        kind = relation['kind']
        kinds.append(kind)
        bodies = relation['map_body_labels']
        if not relation['source_observation'].strip():
            reasons.append('missing_source_observation')
        if not bodies or index not in bodies or len(set(bodies)) != len(bodies):
            reasons.append('relation_not_bound_to_subject')
        if any(label not in by_label for label in bodies):
            reasons.append('unreceived_building_relation')
        if kind == 'corner':
            corners = subject.get('observed_connected_side_pairs') or []
            requested = (relation['segment_ring_index'], relation['first_segment_index'],
                relation['second_segment_index'])
            if not any(pair[0] == requested[0] and
                    {pair[1],pair[2]} == {requested[1],requested[2]}
                    for pair in corners):
                reasons.append('corner_not_observed_in_supplied_map_excerpt')
        elif kind == 'street_termination':
            road = relation['road_candidate_id']
            cues = (physical_context.get('bidirectional_road_axis_cues') or {}).get('rows') or []
            selected = next((r for r in cues if r[0] == road), None)
            direction = relation['road_direction_index']
            if selected is None or direction not in (0,1):
                reasons.append('road_direction_not_supplied')
            else:
                hit_list = selected[6][direction][1]
                if not hit_list or hit_list[0][1] != cid:
                    reasons.append('road_first_hit_disagrees_with_nomination')
        elif kind in {'frontage_sequence','setback','neighbour_order'} and len(bodies) < 2:
            reasons.append('insufficient_distinct_physical_bodies')
    if response['decision'] == 'nominated' and not response['spatial_relations']:
        reasons.append('no_spatial_relationship_supplied')
    if response['decision'] == 'nominated' and not any(
            kind in {'corner', 'frontage_sequence', 'street_termination',
                     'setback', 'neighbour_order'} for kind in kinds):
        reasons.append('no_discriminating_spatial_relation')
    # The angle ratio is ADVISORY only: the focal-35mm framing, unknown crop,
    # camera translation and visible facade versus entire plan are not
    # interchangeable measurements. Never reject or rank an identity by it.
    ratio = subject.get('outline_span_over_exif_diagonal')
    if (response['source_horizontal_extent'] == 'broad'
            and physical_context.get('source_angular_reference', {}).get(
                'camera_position_status') == 'original_exif'
            and isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
            and ratio < .25):
        reasons.append('nominal_angular_scale_conflict_recheck_crop_or_pose')
    alternative_ids = []
    for label in response['alternative_labels']:
        if label not in by_label or label == index:
            reasons.append('unreceived_or_self_alternative')
        else:
            alternative_ids.append(by_label[label]['candidate_id'])
    if response['decision'] == 'uncertain':
        reasons.append('model_declared_uncertain')
    if not response['source_observations'] or not any(
            isinstance(text, str) and text.strip()
            for text in response['source_observations']):
        reasons.append('source_not_described')
    return {'status': ('conditional_physical_nomination' if
                        response['decision'] == 'nominated' else 'uncertain'),
        'candidate_id': cid, 'candidate_label': index,
        'supported_spatial_kinds': list(dict.fromkeys(kinds)),
        'alternative_candidate_ids': list(dict.fromkeys(alternative_ids)),
        'reason_codes': list(dict.fromkeys(reasons)),
        'authorizes_identity': False,
        'scope': 'Model-chosen physical body; semantic SOURCE and map relation '
            'are only bounded observations, not a strict geometry certificate.'}
