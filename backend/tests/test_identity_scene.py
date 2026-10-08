import copy
import io
import math

import httpx
import pytest
from PIL import Image

from street_story.identity_map_context import map_entry_context, geometry_camera_context, osm_geometry_context
from street_story.identity_scene import render_scene, planner_scene, lean_scene_manifest
from street_story.providers import OSMClient
from street_story.db import Store


def point(x, y):
    scale = 6_371_000 * math.pi / 180
    return {'lat':54.7+y/scale, 'lon':20.5+x/(scale*math.cos(math.radians(54.7)))}


def building(index, x, y=10):
    return {'type':'way','id':index,'tags':{'building':'yes'},
        'geometry':[point(x,y),point(x+12,y),point(x+12,y+18),point(x,y+18),point(x,y)],
        'center':point(x+6,y+9)}


def rows(scene):
    table = scene['manifest']['objects']
    decoded = [dict(zip(table['columns'], row)) for row in table['rows']]
    for row in decoded:
        row['height_levels'] = dict(zip(scene['manifest']['height_fields'], row['height_levels']))
        row['address'] = {key:value for key,value in zip(scene['manifest']['address_fields'],row['address']) if value}
    return decoded


def test_all_observed_buildings_survive_scene_and_labels_are_stable_beyond_twenty():
    raw = [building(i, i*9) for i in range(1,26)]
    story = {'latitude':54.7,'longitude':20.5, '_identity_map_snapshot':{'observed_pool':raw}}
    original = copy.deepcopy(story)
    scene = render_scene(story, [map_entry_context(raw[0])])
    packet = rows(scene)
    assert len(packet) == 25 and all(item['label_visible'] for item in packet)
    assert any(item['candidate_id']=='osm:way:21' and item['representative_distance_m']>180 for item in packet)
    assert scene['manifest']['camera']['heading_status']=='missing'
    assert 'heading_degrees' not in scene['manifest']['camera']
    assert all(item['height_levels']['height'] is None and item['height_status']=='missing' for item in packet)
    assert story == original
    reordered = {**story,'_identity_map_snapshot':{'observed_pool':list(reversed(raw))}}
    assert render_scene(reordered, []) == scene
    assert 'points' not in scene['manifest']['objects']['columns']


def test_outer_inner_fragments_and_measured_levels_reach_neutral_scene_without_filled_hole():
    outer = [point(20,20),point(120,20),point(120,120),point(20,120),point(20,20)]
    inner = [point(45,45),point(90,45),point(90,90),point(45,90),point(45,45)]
    item = {'type':'relation','id':99,'center':point(70,70),
        'tags':{'type':'multipolygon','building':'yes','building:levels':'3','roof:levels':'1'},
        'members':[{'type':'way','ref':1,'role':'outer','geometry':outer[:3]},
            {'type':'way','ref':2,'role':'outer','geometry':outer[2:]},
            {'type':'way','ref':3,'role':'inner','geometry':inner}]}
    context = osm_geometry_context(item)
    assert [r['role'] for r in context['rings']] == ['outer','inner']
    assert all(r['closed'] for r in context['rings'])
    assert context['rings'][0]['member_candidate_ids']==['osm:way:1','osm:way:2']
    occupied=point(30,30)
    courtyard=point(60,60)
    assert geometry_camera_context(item,occupied['lat'],occupied['lon'])['camera_inside_footprint']
    assert not geometry_camera_context(item,courtyard['lat'],courtyard['lon'])['camera_inside_footprint']
    item.update(geometry_camera_context(item,54.7,20.5))
    scene = render_scene({'latitude':54.7,'longitude':20.5,'_identity_map_snapshot':{'observed_pool':[item]}},[])
    row = rows(scene)[0]
    assert row['height_levels']=={'height':None,'building:levels':'3','roof:levels':'1'}
    assert row['height_status']=='missing' and row['contours_complete']
    assert row['extent_east_north_m']==[100.,100.]
    assert row['boundary_distance_m']>20 and row['representative_distance_m']>90
    x,y=scene['manifest']['camera_pixel']
    mpp=scene['manifest']['meters_per_pixel']
    with Image.open(io.BytesIO(scene['bytes'])) as image:
        assert image.getpixel((round(x+53/mpp),round(y-54/mpp)))==(250,250,247)


def test_component_and_literal_multiple_address_memberships_remain_exact():
    whole = building(9,20)
    whole['physical_component']={'parent_candidate_id':'osm:relation:99','member_candidate_id':'osm:way:9',
        'role':'outer','proof':'osm_disjoint_outer_closed_way_membership'}
    entries=[whole, {'type':'node','id':1, **point(21,15),'tags':{'entrance':'yes','addr:street':'Literal Street','addr:housenumber':'18A'}},
        {'type':'node','id':2, **point(28,15),'tags':{'entrance':'yes','addr:street':'Literal Street','addr:housenumber':'18B'}}]
    scene=render_scene({'latitude':54.7,'longitude':20.5,'_identity_map_snapshot':{'observed_pool':entries}},[])
    packet={r['candidate_id']:r for r in rows(scene)}
    assert packet['osm:way:9']['component_membership']['parent_candidate_id']=='osm:relation:99'
    assert packet['osm:node:1']['address']['house_number']=='18A'
    assert packet['osm:node:2']['address']['house_number']=='18B'
    assert packet['osm:way:9']['address']=={}


@pytest.mark.asyncio
async def test_one_existing_map_request_keeps_unnamed_roads_entries_and_far_physical_contour(tmp_path):
    nodes=[{'type':'node','id':i+1,**p} for i,p in enumerate(building(9,190)['geometry'])]
    entries=[*nodes, {'type':'way','id':9,'nodes':[1,2,3,4,1],'tags':{'building':'yes','height':'12.4'}},
        {'type':'way','id':10,'nodes':[1,3],'tags':{'highway':'service'}},
        {'type':'node','id':20,**point(196,9),'tags':{'entrance':'yes'}}]
    calls=[]
    async def handle(request):
        calls.append(request)
        if request.url.path.endswith('/map.json'):
            return httpx.Response(200,json={'elements':entries})
        return httpx.Response(200,json={'elements':[]} if request.method=='POST' else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await OSMClient(Store(tmp_path/'db.sqlite3'),'fixture',client).lookup(54.7,20.5)
    assert result['map_patch_radius_m']==320
    assert result['close_radius_m']==160
    assert len([r for r in calls if r.url.path.endswith('/map.json')])==1
    assert {9,10,20}.issubset({item['id'] for item in result['observed_pool']})
    scene=await planner_scene(None,{'latitude':54.7,'longitude':20.5,'_identity_map_snapshot':result},[])
    packet={r['candidate_id']:r for r in rows(scene)}
    assert packet['osm:way:10']['object_kind']['highway']=='service'
    assert packet['osm:way:9']['height_levels']['height']=='12.4'
    assert packet['osm:way:9']['representative_distance_m']>180


@pytest.mark.parametrize('latitude,longitude', [(None,None),(0,None),(True,20.5)])
def test_missing_or_invalid_gps_never_becomes_zero_or_map_localization(latitude,longitude):
    assert render_scene({'latitude':latitude,'longitude':longitude},[map_entry_context(building(9,10))]) is None


def test_incomplete_and_missing_contours_do_not_acquire_invented_vertices_or_height():
    missing = map_entry_context({'type':'way','id':1,'tags':{'building':'yes'}})
    partial = map_entry_context(building(2,20))
    partial['map_geometry']['lines'][0][2].pop('lon')
    scene = render_scene({'latitude':54.7,'longitude':20.5},[missing,partial])
    packet = rows(scene)
    assert packet[0]['geometry_status']=='missing'
    assert packet[1]['geometry_status']=='point_only'
    assert packet[1]['extent_east_north_m'] is None


def test_owner_city_anchor_never_becomes_camera_gps_or_heading_prior():
    import json
    story={'latitude':54.7,'longitude':20.5,'photo_sha256':'source','_identity_generation':2,
        '_camera_position_verified':True, '_camera_hints':{'direction_degrees':90,'direction_ref':'T','direction_status':'true_north'},
        'research_json':json.dumps({'location_provenance':{'kind':'owner_live_place_query','query':'Known city'}})}
    scene=render_scene(story,[map_entry_context(building(1,20))])
    assert scene['manifest']['anchor']['label']=='SEARCH CONTEXT'
    assert scene['manifest']['anchor']['source']=='owner_live_place_query'
    assert scene['manifest']['camera']['latitude'] is None
    assert not scene['manifest']['camera']['position_verified']
    assert 'heading_degrees' not in scene['manifest']['camera']
    assert scene['manifest']['distance_origin']=='search_context_anchor'
    assert 'search anchor' in scene['manifest']['policy_instruction']


def test_verified_camera_and_measured_direction_keep_selected_original_provenance():
    scene=render_scene({'latitude':54.7,'longitude':20.5,'_camera_position_verified':True,
        '_camera_hints':{'direction_status':'true_north','direction_ref':'T','direction_degrees':17}},[map_entry_context(building(1,20))])
    assert scene['manifest']['anchor']['label']=='CAMERA GPS'
    assert scene['manifest']['camera']['position_verified']
    assert scene['manifest']['camera']['heading_degrees']==17
    assert scene['manifest']['distance_origin']=='camera_gps'


def test_lean_scene_keeps_all_exact_labels_and_measured_bodies_without_null_road_rows():
    road={'type':'way','id':40,'tags':{'highway':'service','name':'Literal Road'},'geometry':[point(0,0),point(220,0)]}
    scene=render_scene({'latitude':54.7,'longitude':20.5,'_identity_map_snapshot':{'observed_pool':[
        building(21,190),road]}},[])
    original=copy.deepcopy(scene['manifest'])
    compact=lean_scene_manifest(scene['manifest'])
    assert {row[1] for row in compact['objects']['rows']}=={'osm:way:21','osm:way:40'}
    assert len(compact['physical_geometry']['rows'])==1
    assert compact['height_fields']==['height','building:levels','roof:levels']
    assert compact['anchor']['label']=='SEARCH CONTEXT'
    assert compact['distance_origin']=='search_context_anchor'
    assert 'longest_observed_segments_m' in compact['physical_geometry']['columns']
    assert scene['manifest']==original


@pytest.mark.asyncio
async def test_real_planner_transports_separate_source_and_map_then_retains_original_binding(tmp_path):
    import json
    from types import SimpleNamespace
    from test_visual_search_continuation import prepared
    from street_story.identity_discovery import suggest
    svc,_adapter,story,_sessions=prepared(tmp_path)
    observed=[{**map_entry_context(building(i,i*9)), 'name':'', 'identity_eligible':True} for i in range(1,26)]
    snapshot={**svc._identity_snapshot(story['id'])[0], 'latitude':54.7,'longitude':20.5,
        '_camera_position_verified':True, '_identity_observed_candidates':observed}
    calls=[]
    class Executor:
        async def execute(self,operation,call):
            assert operation=='grounded_research'
            return await call('offline-fixture',5)
    async def generate(key,timeout,contents,config,**kwargs):
        calls.append(contents)
        assert len(contents)==3
        assert contents[0].inline_data.mime_type=='image/jpeg'
        assert contents[1].inline_data.mime_type=='image/png'
        context=json.loads(contents[-1].split('Данные ниже — только контекст:\n')[1])
        packet=context['map_scene']
        assert packet['camera']['position_verified']
        assert any(row[1]=='osm:way:21' for row in packet['objects']['rows'])
        return SimpleNamespace(text=json.dumps({'entity_name':'','wikipedia_queries':[], 'selected_wikipedia_page_ids':[],
            'visual_query':'Visible facade windows','commons_query':'','article_queries':[],
            'first_wave_hypotheses':[{'kind':'appearance','subject_id':'','query':'Visible facade windows','reason':'Observe SOURCE geometry'}]}))
    svc.providers.gemini=SimpleNamespace(executor=Executor(),_generate=generate)
    await suggest(svc,snapshot,'',[])
    receipt=snapshot['_identity_search_plan_payload']['source_map_receipt']
    assert len(calls)==1 and receipt['joint_image_input']
    assert receipt['source_photo_sha256']==story['photo_sha256']
    assert receipt['map_image_sha256']==__import__('hashlib').sha256(calls[0][1].inline_data.data).hexdigest()
