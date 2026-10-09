"""G v3 checks precomputed options rather than asking the LLM for pose math."""
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from street_story.identity_spatial_options import spatial_option_catalog, for_vision, option_digest
from street_story.identity_spatial_choice import check_spatial_choice, visual_spatial_choice_schema
from test_original_osm_geometry_regression import story_for

SOURCE_SHA='a'*64
MODEL_SHA='b'*64


def prepared(case):
    story=story_for(case)
    scene=render_scene(story,[])
    context=physical_decision_context(story,[],scene['manifest'])
    packet=spatial_option_catalog(story,[],scene['manifest'],context)
    return story,scene,packet


def response(label, options, *, pattern='corner', alternative=None, decision='accept'):
    return {'decision':decision,'candidate_label':label,
        'source_pattern':pattern,'crop_scope':'whole',
        'source_observations':['A visible main building face turns at a distinct photographed corner.',
            'The adjacent mass and return wall differ from the comparison building.'],
        'selected_option_ids':options,
        'contrasted_alternatives':([{'label':alternative,
            'source_vs_map_difference':'Different footprint and visible return position',
            'observed_option_ids':[]}] if alternative is not None else []),
        'request_detail_labels':[],'uncertainties':['Camera compass heading missing']}


def validate(packet,response_):
    return check_spatial_choice(response_,packet,source_sha256=SOURCE_SHA,
        actual_source_sha256=SOURCE_SHA,model_source_sha256=MODEL_SHA,
        actual_map_sha256=packet['map_sha256'])


def test_every_received_building_is_in_all_labels_not_nearest_k():
    _,scene,packet=prepared(132)
    assert len(packet['all_received_physical_bodies'])==2
    assert len(packet['expanded_labels'])==2
    assert 'private_label_to_osm_id' not in for_vision(packet)
    assert packet['map_sha256']==scene['manifest']['image_sha256']
    assert all(type(r[0]) is int for r in packet['all_received_physical_bodies'])
    assert len(option_digest(packet))==64
    assert all(not option_id.startswith('osm:') for option_id in packet['options'])
    assert len(packet['options'])<150


def test_source_corner_with_actual_measured_options_needs_no_numeric_camera_pose():
    _,_,packet=prepared(102)
    byid={cid:int(label) for label,cid in packet['private_label_to_osm_id'].items()}
    subject=byid['osm:way:133035113']
    alternative=byid['osm:way:133035111']
    actual=next(name for name,item in packet['options'].items()
        if item['kind']=='observed_corner' and item['body_label']==subject
        and 'nominal_interior' not in item['camera_side_advisory'])
    result=validate(packet,response(subject,[actual],alternative=alternative))
    assert result['accepted'] is True
    assert result['status']=='accepted_geometry_v3'
    assert result['proof']['chosen_measured_options'][actual]['kind']=='observed_corner'
    assert result['proof']['source_sha256']==SOURCE_SHA
    assert result['proof']['map_image_sha256']==packet['map_sha256']
    assert 'heading_degrees' not in str(visual_spatial_choice_schema())
    assert result['proof']['policy']=='street_story.g_option_evidence.v3'


def test_rear_corner_or_unreceived_option_keeps_conditional_candidate():
    _,_,packet=prepared(102)
    byid={cid:int(label) for label,cid in packet['private_label_to_osm_id'].items()}
    subject=byid['osm:way:133035113']
    alternative=byid['osm:way:133035111']
    inward=next(name for name,item in packet['options'].items()
        if item['kind']=='observed_corner' and item['body_label']==subject
        and 'nominal_interior' in item['camera_side_advisory'])
    a=validate(packet,response(subject,[inward],alternative=alternative))
    assert a['accepted'] is False
    assert 'nominal_camera_rear_wall_uncertainty' in a['reason_codes']
    bad=validate(packet,response(subject,['Cnot_received'],alternative=alternative))
    assert bad['candidate_id']=='osm:way:133035113'
    assert not bad['accepted']
    assert bad['reason_codes']==['model_option_id_not_in_frozen_osm','no_measured_osm_option_selected'] or (
        'model_option_id_not_in_frozen_osm' in bad['reason_codes'])


def test_single_visible_facade_can_suffice_with_actual_competitor_contrast():
    _,_,packet=prepared(106)
    byid={cid:int(label) for label,cid in packet['private_label_to_osm_id'].items()}
    subject=byid['osm:way:150596899']
    alt=byid['osm:way:150596903']
    option=next(key for key,value in packet['options'].items()
        if value['body_label']==subject and value['kind']=='single_frontage')
    model=response(subject,[option],pattern='single_frontage',alternative=alt)
    assert validate(packet,model)['accepted'] is True
    sparse=response(subject,[option],pattern='single_frontage')
    assert not validate(packet,sparse)['accepted']
    assert 'single_facade_without_distinguishing_competitor' in validate(packet,sparse)['reason_codes']


def test_original_132_road_axis_directions_have_different_real_first_hits():
    _,_,packet=prepared(132)
    roads=[v for v in packet['options'].values() if v['kind']=='road_axis_direction']
    assert roads
    name_map={cid:int(label) for label,cid in packet['private_label_to_osm_id'].items()}
    target=name_map['osm:way:192217077']
    match=next((k for k,v in packet['options'].items()
        if v['kind']=='road_axis_direction' and v['first_plan_hit_body_label']==target),None)
    assert match
    bad=next((k for k,v in packet['options'].items()
       if v['kind']=='road_axis_direction' and v['first_plan_hit_body_label']!=target),None)
    assert bad
    alt=next(label for label in name_map.values() if label!=target)
    good=validate(packet,response(target,[match],pattern='street_termination',alternative=alt))
    assert good['accepted'] is True
    contradicted=validate(packet,response(target,[bad],pattern='street_termination',alternative=alt))
    assert contradicted['accepted'] is False
    assert 'option_not_bound_to_chosen_physical_body' in contradicted['reason_codes']


def test_full_original_label_scope_and_unchanged_scene_under_detail():
    story,scene,packet=prepared(111)
    body=packet['private_label_to_osm_id']
    target=next(cid for cid in body.values() if cid=='osm:way:95290265')
    zoom=render_scene(story,[],detail_candidate_ids=[target])
    expanded=physical_decision_context(story,[],zoom['manifest'])
    after=spatial_option_catalog(story,[],zoom['manifest'],expanded,
        focus_candidate_ids=[target])
    assert len(after['all_received_physical_bodies'])==len(packet['all_received_physical_bodies'])
    assert set(after['private_label_to_osm_id'].values())==set(body.values())
    assert after['expanded_labels']==[int(next(k for k,v in body.items() if v==target))]
    assert zoom['manifest']['objects']==scene['manifest']['objects']


def test_schema_invalid_and_source_map_mismatch_fail_closed():
    _,_,packet=prepared(132)
    labels=sorted(int(label) for label in packet['private_label_to_osm_id'])
    selected=response(labels[0],['F9999.0.0'],alternative=labels[1])
    invalid=validate(packet,{'type':'object','properties':{}})
    assert invalid['status']=='invalid'
    proof=check_spatial_choice(selected,packet,source_sha256=SOURCE_SHA,
        model_source_sha256=MODEL_SHA,actual_source_sha256='c'*64,
        actual_map_sha256=packet['map_sha256'])
    assert proof['status']=='invalid'
    assert 'source_map_not_original_bound' in proof['reason_codes']
