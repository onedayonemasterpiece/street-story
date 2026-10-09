"""One model-proposed zoom of ORIGINAL neutral OSM, never oracle-guided.

A model that saw original SOURCE+MAP may request detail for exact received
physical labels. This function preserves all bodies on the overview and renders
a highlighted/enlarged MAP subview of *only model proposed* bodies. Geometry
options are independently recalculated from the same immutable observed OSM.
No extra Google/Overpass requests, no address/SID/text inputs, no fitted yaw.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator


def model_nominated_detail(story, previous_response, previous_options, *,
                           candidates=(), max_bodies=4, prior_host_unaccepted=False):
    from .identity_spatial_choice import visual_spatial_choice_schema
    from .identity_scene import render_scene
    from .identity_model_context import physical_decision_context
    from .identity_spatial_options import spatial_option_catalog

    if (not isinstance(previous_response,dict)
            or not Draft202012Validator(visual_spatial_choice_schema()).is_valid(previous_response)
            or previous_response['decision'] not in {'candidate','needs_detail','accept'}
            or (previous_response['decision']=='accept' and
                (not prior_host_unaccepted or not previous_response['request_detail_labels']))
            or previous_options.get('version')!='street_story.g_spatial_options.v3'):
        return None
    label_to_id = previous_options.get('private_label_to_osm_id') or {}
    proposed=[]
    model_labels=[previous_response['candidate_label'],
                  *previous_response['request_detail_labels'],
                  *[item['label'] for item in previous_response['contrasted_alternatives']]]
    for label in model_labels:
        if type(label) is not int:
            return None
        if label==0:
            continue
        cid=label_to_id.get(str(label))
        if not isinstance(cid,str):
            return None
        if cid not in proposed:
            proposed.append(cid)
    if not 1<=len(proposed)<=max_bodies:
        return None
    scene=render_scene(story,list(candidates),detail_candidate_ids=proposed)
    if (not scene or not scene.get('manifest')
            or not any(v.get('name')=='nominated_detail'
                for v in scene['manifest'].get('views') or [])):
        return None
    ctx=physical_decision_context(story,list(candidates),scene['manifest'])
    opts=spatial_option_catalog(story,list(candidates),scene['manifest'],ctx,
                                focus_candidate_ids=proposed)
    if set(opts['private_label_to_osm_id'].values()) != set(
            previous_options['private_label_to_osm_id'].values()):
        return None
    return {'candidate_ids':proposed,
            'model_proposed_labels':[int(next(k for k,v in label_to_id.items()
                                              if v==cid)) for cid in proposed],
            'map':scene,'physical_context':ctx,'spatial_options':opts,
            'previous_map_sha256':previous_options['map_sha256'],
            'all_body_labels_preserved':True,
            'detail_is_not_an_identity_acceptance':True}
