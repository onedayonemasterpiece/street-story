"""Structured spatial evidence in the existing joint SOURCE/MAP decision.

The model owns the image interpretation. The host resolves actual primitives,
computes their relations and checks the stated pose scenarios. No target score,
new inference or independent semantic judge is introduced.
"""
from __future__ import annotations

LEGACY_CONTRACT = 'source-map-spatial-correspondence-v2'
CONTRACT = 'source-map-spatial-correspondence-v3'

GEOMETRY_COMPARISON_INSTRUCTIONS = (
    'Compare the photographed projection with the received MAP geometry, not image size with '
    'plan area. A nearby compact footprint can occupy most of SOURCE; a large complex can recede '
    'or be partly hidden. Roof dormers, projecting bays and facade decoration do not by themselves '
    'establish extra ground-plan wings. Distinguish actual attached volumes from facade/roof '
    'articulation and background neighbors. Missing heading or height stays unmeasured. '
    'A pose hypothesized from visible SOURCE/MAP relations may be tested as an explicit conditional '
    'scenario, never presented as supplied EXIF or measured camera direction. Test material '
    'alternatives and pose sensitivity; unknown heading alone neither accepts nor rejects identity. '
    'Use camera_pose and decisive_relations to explain which observed relations distinguish the '
    'photographed body under the plausible views, and what remains unobserved. Mere apparent size '
    'or resemblance of the whole image to one outline does not resolve an alternative. Use '
    'general_relations or existing quantitative scenarios without inventing measurements. '
    'Requested conditional_boundary_view supplies actual side bearings, angular spans and '
    'midpoint self-occlusion from the nominal anchor. Compare the visible adjoining SOURCE '
    'faces with those sides; a whole footprint is not its visible projection. Inside/on-boundary '
    'means an exterior view is undefined at that scenario, not that the body is excluded: '
    'an approximate anchor may need an explicit offset scenario. These 2D rays do not measure '
    'heading, height or visibility past other bodies. '
    'When sufficient discriminating geometry exists, accept G independently; otherwise retain '
    'uncertainty and one useful next_action for existing text/REF. No mandatory REF or second judge. '
)


def _measured_schema(feature):
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


def correspondence_schema(feature):
    """General observed relations, plus conditional quantitative scenarios."""
    text = {'type': 'string', 'minLength': 1, 'maxLength': 300}
    general = {'type': 'object', 'properties': {
        'pattern_kind': {'const': 'general_relations'},
        'source_pattern': text, 'coverage_basis': text,
        'assumption_limits': {'type': 'array', 'maxItems': 8, 'items': text},
        'candidate_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                          'uniqueItems': True, 'items': {'type': 'string', 'maxLength': 100}},
        'decisive_relation_indices': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                     'uniqueItems': True, 'items': {'type': 'integer', 'minimum': 0, 'maximum': 7}}},
        'required': ['pattern_kind', 'source_pattern', 'coverage_basis', 'assumption_limits',
                     'candidate_ids', 'decisive_relation_indices'], 'additionalProperties': False}
    return {'oneOf': [general, _measured_schema(feature)]}


def _general_correspondence(story, candidates, decision, receipt, value):
    from .identity_spatial_features import geometry_feature
    cid = decision['candidate_id']
    ids = value.get('candidate_ids') or []
    table = (receipt.get('manifest') or {}).get('objects') or {}
    received = {dict(zip(table.get('columns') or [], row)).get('candidate_id')
                for row in table.get('rows') or []}
    allowed = set(receipt.get('physical_body_candidate_ids') or received)
    required = set(receipt.get('material_alternative_candidate_ids') or []) - {cid}
    addressed = {item['candidate_id'] for item in decision['rejected_alternatives']}
    relations = decision['decisive_relations']
    indices = value.get('decisive_relation_indices') or []
    if (cid not in ids or not set(ids).issubset(allowed & received)
            or not required.issubset(addressed)
            or set(indices) != set(range(len(relations)))
            or not value['source_pattern'].strip() or not value['coverage_basis'].strip()):
        return None
    resolved = []
    for index in indices:
        relation = relations[index]
        features = []
        for ref in relation['map_features']:
            feature = geometry_feature(story, candidates, ref)
            if ref['candidate_id'] not in received or not feature:
                return None
            features.append({'reference': ref, 'observed_feature': feature})
        if not features:
            return None
        resolved.append({'relation_index': index, 'features': features})
    return {'contract': CONTRACT, 'kind': 'general_relations', 'resolved_relations': resolved,
            'assumption_limits': value['assumption_limits'],
            'scope': 'Same joint SOURCE/MAP semantic decision, bound to received primitives; no numerical pose claimed.'}


def measured_correspondence(story, candidates, decision, receipt):
    from .identity_spatial_features import geometry_feature, measure_spatial_relations
    value = decision.get('spatial_correspondence') or {}
    if value.get('pattern_kind') == 'general_relations':
        if receipt.get('geometry_contract') != CONTRACT:
            return None  # Frozen v2 operations retain their original measured contract.
        return _general_correspondence(story, candidates, decision, receipt, value)
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
        if pattern == 'corner' and measured['front_segments'][0]['side_angle_difference_degrees'] in {0, -180, 180}:
            return None
        if pattern == 'street_termination':
            hits = measured['street_axis_ray_intersections']
            if not hits or hits[0]['candidate_id'] != cid:
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
