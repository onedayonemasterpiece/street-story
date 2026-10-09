"""Structured spatial evidence in the existing joint SOURCE/MAP decision.

The model owns the image interpretation. The host resolves actual primitives,
computes their relations and checks the stated pose scenarios. No target score,
new inference or independent semantic judge is introduced.
"""
from __future__ import annotations

CONTRACT = 'source-map-spatial-correspondence-v2'


def correspondence_schema(feature):
    text = {'type': 'string', 'minLength': 1, 'maxLength': 300}
    pose = {'type': 'object', 'properties': {
        'east_m': {'type': 'number'}, 'north_m': {'type': 'number'},
        'heading_degrees': {'type': 'number', 'minimum': 0, 'maximum': 360}},
        'required': ['east_m', 'north_m', 'heading_degrees'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'pattern_kind': {'type': 'string', 'enum': ['corner', 'frontage_sequence', 'street_termination']},
        'source_pattern': text, 'pitch_basis': text, 'coverage_basis': text,
        'candidate_ids': {'type': 'array', 'minItems': 1, 'maxItems': 6,
            'uniqueItems': True, 'items': {'type': 'string', 'maxLength': 100}},
        'pose': pose,
        'front_segments': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object',
            'properties': {'first': feature, 'second': feature},
            'required': ['first', 'second'], 'additionalProperties': False}},
        'street_axis': {'anyOf': [feature, {'type': 'null'}]},
        'uncertainty_scenarios': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': {'type': 'object', 'properties': {'pose': pose,
                'assumption': text, 'source_pattern_preserved': {'type': 'boolean'}},
                'required': ['pose', 'assumption', 'source_pattern_preserved'], 'additionalProperties': False}}},
        'required': ['pattern_kind', 'source_pattern', 'pitch_basis', 'coverage_basis',
            'candidate_ids', 'pose', 'front_segments', 'street_axis', 'uncertainty_scenarios'],
        'additionalProperties': False}


def measured_correspondence(story, candidates, decision, receipt):
    from .identity_spatial_features import geometry_feature, measure_spatial_relations
    value = decision.get('spatial_correspondence') or {}
    cid = decision['candidate_id']
    ids = value.get('candidate_ids') or []
    if cid not in ids or any(not value.get(key, '').strip() for key in
            ('source_pattern', 'pitch_basis', 'coverage_basis')):
        return None
    table = (receipt.get('manifest') or {}).get('objects') or {}
    received = {dict(zip(table.get('columns') or [], row)).get('candidate_id')
        for row in table.get('rows') or []}
    if any(item not in received for item in ids):
        return None
    alternatives = {item['candidate_id'] for item in decision['rejected_alternatives']}
    context_ids = set(receipt.get('physical_body_candidate_ids') or [])
    if context_ids and any(item not in context_ids for item in ids):
        return None
    required = set(receipt.get('material_alternative_candidate_ids') or []) - {cid}
    if not required.issubset(alternatives):
        return None
    fronts, axis = value['front_segments'], value['street_axis']
    pattern = value['pattern_kind']
    if pattern == 'corner':
        if not fronts or axis is not None:
            return None
        refs = [fronts[0]['first'], fronts[0]['second']]
        features = [geometry_feature(story, candidates, ref) for ref in refs]
        if any(ref.get('kind') != 'segment' or ref.get('candidate_id') != cid for ref in refs):
            return None
        if any(not feature or len(feature['coordinates']) != 2 for feature in features):
            return None
        if not any(a == b for a in features[0]['coordinates'] for b in features[1]['coordinates']):
            return None
    elif pattern == 'frontage_sequence':
        if len(ids) < 2 or not fronts or axis is not None:
            return None
        if any(pair['first'].get('kind') != 'segment' or pair['second'].get('kind') != 'segment'
                or pair['first']['candidate_id'] == pair['second']['candidate_id'] for pair in fronts):
            return None
    elif pattern == 'street_termination':
        if not axis or axis.get('kind') != 'road_axis':
            return None
    else:
        return None
    measurements = []
    poses = [value['pose'], *[item['pose'] for item in value['uncertainty_scenarios']]]
    if any(item['source_pattern_preserved'] is not True or not item['assumption'].strip()
            for item in value['uncertainty_scenarios']):
        return None
    if any(pose == poses[0] for pose in poses[1:]):
        return None  # Merely renaming a crop is not an input-uncertainty test.
    for pose in poses:
        measured = measure_spatial_relations(story, candidates, ids,
            pose=pose, front_segments=fronts, street_axis=axis)
        if measured is None:
            return None
        if pattern == 'corner':
            # A nearly straight/anti-parallel pair is not a discriminating
            # photographed corner even when the OSM ends technically touch.
            # This checks only actual map-plane geometry; it NEVER infers the
            # photographed return, pose or target identity.
            turn = abs(measured['front_segments'][0]['side_angle_difference_degrees'])
            if min(turn, 180-turn) < 8:
                return None
        if pattern == 'street_termination':
            hits = measured['street_axis_ray_intersections']
            axis_info = measured.get('street_axis_scenario') or {}
            difference = abs(axis_info.get('heading_difference_degrees', 180))
            # A model could formerly claim an unrelated arbitrary road, point
            # its ray at any desired building, and pass the 'first hit' guard.
            # Heading need only broadly follow the observed road in either
            # direction, not a falsely exact camera bearing.
            axis_misalignment = min(difference, abs(180-difference))
            if (not hits or hits[0]['candidate_id'] != cid or
                    axis_misalignment > 30):
                return None
        # A horizontal viewing scenario must place nominated bodies in front.
        if any(item['relative_bearing_degrees'] is None or abs(item['relative_bearing_degrees']) >= 90
                for item in measured['objects']):
            return None
        measurements.append(measured)
    if pattern == 'frontage_sequence' and any(
            measured['left_to_right_order'] != measurements[0]['left_to_right_order']
            for measured in measurements[1:]):
        return None
    return {'contract': CONTRACT, 'measurements': measurements,
        'scope': 'Observed OSM relations under explicit model pose scenarios; '
            'SOURCE interpretation and material relevance remain the joint model decision.'}
