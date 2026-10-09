"""LLM-chosen street termination → strict OSM-verified physical G certificate.

This helper DOES NOT discover or select a building. The visual LLM must first
nominate exact received MAP body, SOURCE street-ending observation, actual OSM
road ID/direction, and material alternative from the *opposite* ray. Host
mechanics derive a conditional horizontal bearing from observed OSM vertices,
not a fabricated EXIF heading or guessed model camera pose. A fixed symmetric
translation stress test (±2m) is explicitly hypothetical, not measured camera
accuracy. Existing freeze_geometry_proof has the final authority.

Never call this on text-only planning, a reference photo, unbound pixel inputs
or any answer-key-labelled test data.
"""
from __future__ import annotations

import hashlib
import json


def freeze_model_street_termination(story, visual_response, source_map_receipt,
                                    physical_context, candidates):
    from .identity_geometry_nomination import check_visual_geometry_nomination
    from .identity_geometry_contract import CONTRACT
    from .identity_proof import freeze_geometry_proof
    from .identity_spatial_features import _point, _local, _bearing, geometry_feature, measure_spatial_relations

    receipt = dict(source_map_receipt or {})
    manifest = receipt.get('manifest') or {}
    summary = check_visual_geometry_nomination(
        visual_response, manifest, physical_context,
        source_map_bound=(receipt.get('joint_image_input') is True and
            receipt.get('source_photo_sha256') == story.get('photo_sha256') and
            receipt.get('map_image_sha256') == manifest.get('image_sha256')))
    if (summary['status'] != 'conditional_physical_nomination'
            or summary['reason_codes']
            or not isinstance(visual_response, dict)):
        return None
    cid = summary['candidate_id']
    relations = [r for r in visual_response['spatial_relations']
        if r['kind'] == 'street_termination' and
        visual_response['candidate_label'] in r['map_body_labels']]
    if len(relations) != 1:
        return None
    relation = relations[0]
    if not relation['source_observation'].strip() or not visual_response['source_observations']:
        return None

    axis_rows = (physical_context.get('bidirectional_road_axis_cues') or {}).get('rows') or []
    row = next((r for r in axis_rows if r[0] == relation['road_candidate_id']), None)
    direction_index = relation['road_direction_index']
    if row is None or direction_index not in (0,1):
        return None
    toward = row[6][direction_index][1]
    opposite = row[6][1-direction_index][1]
    if (not toward or toward[0][1] != cid
            or not opposite or opposite[0][1] == cid
            or opposite[0][0] not in visual_response['alternative_labels']):
        return None
    contrary_id = opposite[0][1]
    if contrary_id not in (receipt.get('physical_body_candidate_ids') or []):
        return None
    road={'candidate_id':row[0],'kind':'road_axis',
          'line_index':row[2],'segment_index':row[3]}
    axis = geometry_feature(story,candidates,road)
    origin = _point(story)
    if axis is None or origin is None or len(axis.get('coordinates') or []) != 2:
        return None
    a,b=[_local(_point(point),origin) for point in axis['coordinates']]
    direction=_bearing((b[0]-a[0],b[1]-a[1]))
    if direction_index == 1:
        direction=(direction+180)%360
    # Fixed symmetric stress test, independent of the selected candidate,
    # never extrapolated as measured GPS error or model-observed second image.
    poses=[
        {'east_m':0.,'north_m':0.,'heading_degrees':direction},
        {'east_m':2.,'north_m':0.,'heading_degrees':direction},
        {'east_m':-2.,'north_m':0.,'heading_degrees':direction},
    ]
    for pose in poses:
        calculation=measure_spatial_relations(story,candidates,[cid],
             pose=pose, front_segments=[],street_axis=road)
        if (not calculation or not calculation['street_axis_ray_intersections']
                or calculation['street_axis_ray_intersections'][0]['candidate_id'] != cid):
            return None
        relative = calculation['objects'][0]['relative_bearing_degrees']
        if relative is None or abs(relative)>=90:
            return None

    camera_kind=(manifest.get('camera') or {}).get('position_status')
    if camera_kind not in ('original_exif','owner_approximate'):
        return None
    source_statement=relation['source_observation'].strip()[:290]
    extra=visual_response['source_observations'][:3]
    source_note=source_statement
    if extra and extra[0] != source_statement:
        # Original unaltered SOURCE observation text is included as separate
        # assumptions; do not append invented photographic structures.
        source_note=source_statement
    alt_reason=('The identical observed street axis in the opposite '
        'direction first intersects another physical OSM body; '
        'the SOURCE model explicitly selected the opposite approach.')
    decision={
        'decision':'accepted_geometry',
        'candidate_id':cid,
        'candidate_label':summary['candidate_label'],
        'spatial_correspondence':{
            'pattern_kind':'street_termination',
            'source_pattern':source_note,
            'pitch_basis':'Vertical camera pitch is not measured; only horizontal road geometry is tested.',
            'coverage_basis':('Both observed road directions were considered; only received OSM '
                              '2D footprint hits, not 3D occlusion or all-world uniqueness.'),
            'candidate_ids':[cid],
            'pose':poses[0],
            'front_segments':[],
            'street_axis':road,
            'uncertainty_scenarios':[
                {'pose':poses[1], 'assumption':(
                    'Counterfactual eastward 2m camera translation; NOT measured GPS error '
                    'and not a second SOURCE image.'), 'source_pattern_preserved':True},
                {'pose':poses[2], 'assumption':(
                    'Counterfactual westward 2m camera translation; NOT measured GPS error '
                    'and not a second SOURCE image.'), 'source_pattern_preserved':True}]},
        'scope':'The single received mapped physical footprint facing the selected SOURCE road approach.',
        'decisive_relations':[{
            'source_observation':source_statement,
            'map_features':[road,{'candidate_id':cid,'kind':'contour','ring_index':0}],
            'correspondence':('The chosen observed road direction first hits this exact '
                              'OSM footprint in the nominal and two fixed alternate poses.') }],
        'rejected_alternatives':[{'candidate_id':contrary_id,'reason':alt_reason}],
        'assumptions':[
            'The facing direction was decided by a visual model using the ORIGINAL SOURCE and neutral MAP.',
            'The OSM segment azimuth is measured map geometry; it is NOT a compass EXIF heading.',
            f'Camera anchor is {camera_kind}; its positional uncertainty is unknown.'],
        'bounded_coverage':{
            'scope':'Received OSM road and building footprints only; both ray directions considered.',
            'limitations':[
                '2D first-hit does not prove real 3D visibility, heights or missing OSM features.',
                'Two 2m translation scenarios are not an inferred error radius.',
                'No externally calibrated crop or actual measured camera heading.'],
            'material_alternatives_resolved':True},
        'camera_pose':{
            'position_basis':f'Nominal {camera_kind} anchor, not accuracy or verified bearing.',
            'yaw_basis':'Source visual approach direction + azimuth derived from observed road segment, not EXIF.',
            'sensitivity':'Nominal and fixed symmetric 2m translations preserve the same first 2D footprint intersection.'},
        'next_action':{'kind':'none','reason':'Qualified street-axis geometry certificate.',
            'target_candidate_ids':[]}}
    receipt.update(geometry_contract=CONTRACT,
        material_alternative_candidate_ids=[contrary_id])
    proof=freeze_geometry_proof(story,decision,receipt,candidates)
    if proof is None:
        return None
    return {'candidate_id':cid,'proof':proof,
            'source':'visual_model_nomination_plus_observed_osm_two_way_road',
            'visual_response_sha256':hashlib.sha256(json.dumps(
                 visual_response,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            'road_candidate_id':road['candidate_id'],
            'scenario_count':len(poses),
            'camera_position_status':camera_kind,
            'no_measured_yaw':True,
            'scope':'Source-observed street termination validated on 2D OSM and fixed scenarios; '
                    'not an exhaustive proof of global uniqueness or 3D occlusion.'}
