"""Original OSM street geometry with a hypothetical two-model SOURCE response.

No inference in tests. Real output requires an actual blind model and a later
independent road review; these fixtures do not count as product acceptance.
"""
import copy
import hashlib
import json

from google.genai import types

from street_story.mvp_research import MvpResearchMixin
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from street_story.identity_road_followup import (
    street_termination_review_schema, combine_road_review,
)
from street_story.identity_road_witness import freeze_model_street_termination
from street_story.identity_geometry_nomination import check_visual_geometry_nomination
from test_original_osm_geometry_regression import story_for


def full_case():
    story=story_for(132)
    story.update(photo_sha256=hashlib.sha256(b'original-source-test-boundary').hexdigest(),
        _identity_generation=0,_identity_research_control_revision=0,
        _identity_original_source_sha256=hashlib.sha256(b'original-source-test-boundary').hexdigest(),
        research_json='{}')
    story['_identity_observed_candidates']=MvpResearchMixin._candidate_catalog(
        story['_identity_map_snapshot'],[],observed_pool=True)
    scene=render_scene(story,[])
    ctx=physical_decision_context(story,[],scene['manifest'])
    body_labels={r[1]:r[0] for r in ctx['rows']}
    road=next(r for r in ctx['bidirectional_road_axis_cues']['rows'] if r[0]=='osm:way:67826885')
    camera=story['_identity_original_source_sha256']
    receipt={'joint_image_input':True,'source_photo_sha256':camera,'original_source_sha256':camera,
        'model_source_sha256':camera,'map_image_sha256':scene['manifest']['image_sha256'],
        'manifest':scene['manifest'],'map_identity_labels_required':True,
        'physical_body_candidate_ids':[r[1] for r in ctx['rows']]}
    return story,ctx,receipt,body_labels,road


def proposals():
    _story,_ctx,_receipt,body,road=full_case()
    subject=body['osm:way:192217077']
    opposite=body[road[6][0][1][0][1]]
    prior={'decision':'nominated','candidate_label':subject,
       'source_horizontal_extent':'broad',
       'source_observations':['SOURCE shows a broad brick building and T-junction ahead.'],
       'spatial_relations':[],'alternative_labels':[opposite],
       'uncertainties':['No EXIF bearing; camera coordinates supplied approximately.']}
    review={'verdict':'street_termination_supported','prior_subject_label':subject,
       'road_candidate_id':road[0],'road_direction_index':1,
       'opposite_first_hit_label':opposite,
       'source_road_observations':[
          'SOURCE approach street reaches a cross street directly opposite broad frontal building.',
          'The photographed ground approach is aligned with this measured road direction.'],
       'visual_contradictions':[],
       'uncertainties':['The exact horizontal heading is derived from OSM, not EXIF.']}
    return prior,review


def test_followup_provider_schema_is_small_and_has_no_camera_yaw_guess():
    schema=street_termination_review_schema()
    obj=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema).model_dump(exclude_none=True,mode='json')
    assert 'response_json_schema' in obj
    assert len(json.dumps(schema).encode()) < 1700
    assert 'heading_degrees' not in json.dumps(schema)


def test_real_osm_132_model_selected_street_witness_passes_strict_host():
    story,ctx,receipt,body,road=full_case()
    prior,followup=proposals()
    combined=combine_road_review(prior,followup)
    assert combined is not None
    conditioned=check_visual_geometry_nomination(combined,receipt['manifest'],ctx)
    assert conditioned['candidate_id']=='osm:way:192217077'
    assert conditioned['status']=='conditional_physical_nomination'
    assert conditioned['reason_codes']==[]
    assert conditioned['authorizes_identity'] is False
    proof=freeze_model_street_termination(story,combined,receipt,ctx,
        story['_identity_observed_candidates'])
    assert proof is not None
    assert proof['candidate_id']=='osm:way:192217077'
    assert proof['scenario_count']==3
    assert proof['proof']['proof_sha256']
    assert proof['no_measured_yaw'] is True


def test_wrong_street_direction_and_unreviewed_competitor_never_authorize():
    story,ctx,receipt,*_=full_case()
    prior,followup=proposals()
    for change in ('wrong_direction','missing_competitor','fabricated_road','contradiction',
                   'unconfirmed'):
        invalid=copy.deepcopy(followup)
        if change=='wrong_direction':
            invalid['road_direction_index']=0
        elif change=='missing_competitor':
            invalid['opposite_first_hit_label']=999999
        elif change=='fabricated_road':
            invalid['road_candidate_id']='osm:way:999999999'
        elif change=='contradiction':
            invalid['visual_contradictions']=['SOURCE orientation visibly conflicts with road']
        elif change=='unconfirmed':
            invalid['verdict']='uncertain'
        combined=combine_road_review(prior,invalid)
        if combined is not None:
            assert freeze_model_street_termination(story,combined,receipt,ctx,
                 story['_identity_observed_candidates']) is None
        else:
            assert change in {'contradiction','unconfirmed'}
