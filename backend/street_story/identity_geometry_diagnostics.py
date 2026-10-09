"""Non-authorizing geometry diagnosis for a SOURCE/MAP model nomination.

Accepting a building remains exclusively the existing strict
freeze_geometry_proof predicate. A real model can correctly nominate a physical
building while supplying incomplete/wrong segment, camera or coverage evidence.
Keep that LLM nomination as a *conditional hypothesis*, never as an identity,
score, fact-binding, verified source or POI-memory key.

All numeric relationships below come from supplied OSM primitives. There are
no ground-truth IDs, address clues, style-match heuristics or provider calls.
"""
from __future__ import annotations

import math


def _received_physical_label(story, decision, receipt, candidates):
    from .identity_candidate_policy import candidate_identity_eligible
    from .identity_subject_binding import article_candidate
    from .identity_scene import scene_entries
    if not isinstance(decision, dict):
        return None
    cid = decision.get('candidate_id')
    if not isinstance(cid, str) or not cid.startswith(('osm:way:', 'osm:relation:')):
        return None
    manifest = receipt.get('manifest') or {}
    objects = manifest.get('objects') or {}
    rows = [dict(zip(objects.get('columns') or [], r)) for r in objects.get('rows') or []]
    labels = [r['candidate_id'] for r in rows if r.get('label') == decision.get('candidate_label')]
    if len(labels) != 1 or labels[0] != cid:
        return None
    catalog = {e['candidate_id']: e for e in scene_entries(story, candidates)}
    candidate = next((x for x in [*(story.get('_identity_observed_candidates') or []),*candidates]
                      if isinstance(x,dict) and x.get('candidate_id') == cid), {})
    if cid not in catalog or not candidate or not candidate_identity_eligible(candidate) or article_candidate(candidate):
        return None
    return cid


def _bound_source_and_map(story, receipt):
    import re
    def sha(v):
        return isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) is not None
    if (receipt.get('joint_image_input') is not True or
            not sha(story.get('photo_sha256')) or
            receipt.get('source_photo_sha256') != story['photo_sha256'] or
            not sha(receipt.get('original_source_sha256')) or
            not sha(receipt.get('model_source_sha256')) or
            not sha(receipt.get('map_image_sha256'))):
        return False
    if (story.get('_identity_original_source_sha256') is not None and
            story['_identity_original_source_sha256'] != receipt['original_source_sha256']):
        return False
    return (receipt.get('manifest') or {}).get('image_sha256') == receipt['map_image_sha256']


def diagnose_geometry_nomination(story, decision, receipt, candidates):
    """Return a bounded proof diagnosis or conditional nomination, never a PASS.

    output.status:
       no_grounded_nomination: invalid/unknown physical pointer or image binding
       conditional_physical_nomination: literal model candidate, proof insufficient
       accepted_geometry: existing host proof alone has succeeded

    The caller may retain only conditional hypotheses as NOT_ACCEPTED, never
    silently put the subject into facts or POI-memory. Original raw decision and
    receipt continue to be stored by their existing durability mechanisms.
    """
    from .identity_spatial_features import geometry_feature, measure_spatial_relations
    from .identity_proof import freeze_geometry_proof
    from .identity_geometry_contract import CONTRACT
    from .identity_source_selection import geometry_decision_schema
    from jsonschema import Draft202012Validator

    reasons = []
    if not isinstance(receipt, dict) or not _bound_source_and_map(story, receipt):
        return {'status': 'no_grounded_nomination', 'candidate_id': None,
                'reason_codes': ['source_map_evidence_unbound'], 'authorizes_identity': False}
    cid = _received_physical_label(story, decision, receipt, candidates)
    if cid is None:
        return {'status': 'no_grounded_nomination', 'candidate_id': None,
                'reason_codes': ['physical_label_or_candidate_unverified'], 'authorizes_identity': False}
    proof = freeze_geometry_proof(story, decision, receipt, candidates)
    if proof is not None:
        return {'status': 'accepted_geometry', 'candidate_id': cid,
                'proof_sha256': proof['proof_sha256'], 'reason_codes': [],
                'authorizes_identity': True}
    structured = receipt.get('geometry_contract') == CONTRACT
    schema = geometry_decision_schema([], structured=structured)
    if not Draft202012Validator(schema).is_valid(decision):
        reasons.append('semantic_decision_or_geometry_shape_invalid')
    current = decision.get('spatial_correspondence') or {}
    if not isinstance(current, dict):
        current = {}
    pattern = current.get('pattern_kind')
    refs = current.get('front_segments')
    if not isinstance(refs, list):
        if pattern in ('corner','frontage_sequence'):
            reasons.append('front_segment_pairs_not_an_array')
        refs = []
    if pattern == 'corner':
        if not refs:
            reasons.append('no_provable_corner_pair')
        for pair in refs[:3]:
            if not isinstance(pair, dict):
                reasons.append('corner_pair_shape_invalid')
                continue
            first,second = pair.get('first'), pair.get('second')
            originals = [geometry_feature(story,candidates,ref) for ref in (first,second)]
            if any(not ref or ref.get('kind')!='segment' or ref.get('candidate_id')!=cid
                   for ref in (first,second) if isinstance(ref,dict)) or (
                    not isinstance(first,dict) or not isinstance(second,dict)):
                reasons.append('corner_references_not_subject_segments')
            if any(feature is None for feature in originals):
                reasons.append('corner_segments_not_received')
                continue
            if not any(a==b for a in originals[0]['coordinates']
                       for b in originals[1]['coordinates']):
                reasons.append('corner_segments_do_not_share_vertex')
            else:
                from .identity_spatial_features import _point, _local
                origin = _point(story)
                if origin is not None:
                    f1,f2 = originals
                    pts1=[_local(_point(x),origin) for x in f1['coordinates']]
                    pts2=[_local(_point(x),origin) for x in f2['coordinates']]
                    vx,vy=pts1[1][0]-pts1[0][0],pts1[1][1]-pts1[0][1]
                    wx,wy=pts2[1][0]-pts2[0][0],pts2[1][1]-pts2[0][1]
                    turn=abs(math.degrees(math.atan2(vx*wy-vy*wx,vx*wx+vy*wy)))
                    if min(turn,180-turn)<8:
                        reasons.append('corner_nearly_collinear')
    uncertainty=current.get('uncertainty_scenarios')
    origin_pose=current.get('pose')
    if not isinstance(origin_pose,dict):
        reasons.append('model_pose_not_supplied')
    elif not isinstance(uncertainty,list) or not uncertainty:
        reasons.append('no_distinct_uncertainty_scenario')
    else:
        poses=[origin_pose,*[u.get('pose') for u in uncertainty if isinstance(u,dict)]]
        if any(u==origin_pose for u in poses[1:]):
            reasons.append('repeated_nominal_pose')
        for pose in poses[:4]:
            if not isinstance(pose,dict):
                reasons.append('malformed_pose')
                continue
            measured=measure_spatial_relations(story,candidates,current.get('candidate_ids') or [cid],
                pose=pose,front_segments=refs,street_axis=current.get('street_axis'))
            if measured is None:
                reasons.append('pose_or_osm_relations_unmeasurable')
                continue
            if any(item['relative_bearing_degrees'] is None or
                abs(item['relative_bearing_degrees'])>=90 for item in measured.get('objects') or []):
                reasons.append('nominated_body_outside_forward_view')
            if pattern=='street_termination':
                info=measured.get('street_axis_scenario') or {}
                difference=abs(info.get('heading_difference_degrees',180))
                if min(difference,abs(180-difference))>30:
                    reasons.append('model_ray_not_aligned_with_street_axis')
                if not measured.get('street_axis_ray_intersections') or (
                    measured['street_axis_ray_intersections'][0]['candidate_id']!=cid):
                    reasons.append('street_termination_does_not_first_hit_nominated_body')
    declared=set(current.get('candidate_ids') or [])
    if cid not in declared:
        reasons.append('subject_missing_from_declared_geometry')
    required=set(receipt.get('material_alternative_candidate_ids') or [])-{cid}
    rejected={x.get('candidate_id') for x in decision.get('rejected_alternatives') or []
              if isinstance(x,dict)}
    if not required.issubset(rejected):
        reasons.append('material_alternatives_not_addressed')
    if not reasons:
        reasons.append('strong_spatial_proof_not_confirmed')
    return {'status':'conditional_physical_nomination','candidate_id':cid,
        'reason_codes':list(dict.fromkeys(reasons))[:12],
        'model_claimed_pattern':pattern if isinstance(pattern,str) else None,
        'authorizes_identity':False,
        'scope':'Received physical OSM candidate nominated by image-using LLM; actual SOURCE/MAP '
            'proof failed; no measured camera pose or probability is inferred.'}
