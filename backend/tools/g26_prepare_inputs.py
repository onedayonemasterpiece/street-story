"""Prepare original PHOTO→neutral OSM MAP for the entire 26-case Street Story G corpus.

The only input metadata are original image paths/SHA and either ORIGINAL EXIF
GPS or the owner's separately declared approximate camera point; never the
known correct building, article SID, placename inferred by an answer key, or T.
Reuse pinned frozen full OSM XML/API evidence. No provider inference or
publication. Private readback has exact sent image hashes and original
map/labels; no new service, job queue or corpus-specific candidate logic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from street_story.camera_hints import read_camera_hints
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from street_story.identity_map_context import geometry_camera_context
from street_story.photo_metadata import inspect_gps
from street_story.reference_image_codec import normalize_reference

ROOT = Path('/home/dev/artifacts/street-story')
SOURCE_INDEX = ROOT/'20261009T111952Z-architecture-t-full-corpus-20261009/source-inventory-26.json'
OUTPUT = ROOT/'20261009-G-spatial-v3'
FROZEN_XML = Path(__file__).resolve().parent/'fixtures/g-frozen-osm-20261009'
TARGET = [102, *range(104, 113), *range(118, 134)]
assert len(TARGET)==26
FOUR = {118,119,121,123}


def save(path: Path, obj):
    if path.exists():
        return
    value=json.dumps(obj,ensure_ascii=False,indent=2)
    path.write_text(value+'\n')
    os.chmod(path,0o600)


def observed_osm(id: int, point: tuple[float,float]) -> tuple[dict, dict]:
    if id==102:
        cold=ROOT/'20261008T061021Z-cold-photo-workers-20261008/gt-independent-provider-20261009-c'
        db=next(cold.glob('data-*/*.sqlite3'))
        with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as conn:
            research=json.loads(conn.execute('SELECT research_json FROM stories LIMIT 1').fetchone()[0])
        assert isinstance(research.get('osm',{}).get('observed_pool'),list)
        return research['osm'],{'source':'frozen_saved_SQLite_original_OSM','filename':str(db)}
    if id in FOUR:
        file=OUTPUT/'observed_osm'/f'photo-{id}.json'
        data=json.loads(file.read_text())
        if data['message_id']!=id or data['camera_point']!=list(point):
            raise ValueError('frozen_exif_osm_binding_mismatch')
        return data['osm'],{'source':'stock_OSMClient_original_frozen','sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    file=FROZEN_XML/f'photo-{id}.osm.gz'
    if not file.is_file():
        raise ValueError('frozen_raw_osm_missing')
    from g_cached_raw_osm import pool_from_cached_xml
    raw=pool_from_cached_xml(file)
    pool=[{**item,**geometry_camera_context(item,*point)} if item.get('geometry') or item.get('members')
         else item for item in raw]
    return {'observed_pool':pool,'coverage':{'source':'frozen_raw_osm','completeness':'unknown'}},{
        'source':'pinned_20261008_original_OSM_XML',
        'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'bytes':file.stat().st_size}


def prepare(id, inputs):
    start=time.monotonic()
    row=inputs[id]
    jpg=Path(row['source_path'])
    if not jpg.is_file():
        return {'id':id,'status':'missing_original_source'}
    raw=jpg.read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    if sha!=row['sha256']:
        return {'id':id,'status':'original_source_sha_mismatch'}
    exif=inspect_gps(raw)
    hints=read_camera_hints(raw)
    basis=row['geographic_basis']
    if basis=='original_exif':
        if exif['status']!='gps_present' or [exif['latitude'],exif['longitude']]!=row['camera_point']:
            return {'id':id,'status':'original_exif_binding_mismatch'}
        point=tuple(row['camera_point'])
    elif basis=='owner_approximate':
        if exif['status']=='gps_present':
            return {'id':id,'status':'owner_hint_overrides_original_exif_not_allowed'}
        point=tuple(row['camera_point'])
    else:
        if point:=row.get('camera_point'):
            return {'id':id,'status':'untrusted_camera_prior'}
        return {'id':id,'status':'missing_geopoint','basis':basis,
                'original_photo_sha256':sha,'g_applicable':False,
                'method_calls':0}
    case_dir=OUTPUT/'cases'/str(id)
    case_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
    result_path=case_dir/'input-receipt.json'
    if result_path.exists():
        prior=json.loads(result_path.read_text())
        if prior['original_photo_sha256']==sha:
            return prior
        return {'id':id,'status':'frozen_input_receipt_conflict'}
    osm,origin=observed_osm(id,point)
    if not osm.get('observed_pool'):
        return {'id':id,'status':'empty_osm_observed_pool'}
    story={'latitude':point[0],'longitude':point[1],'photo_sha256':sha,
        '_identity_original_source_sha256':sha,
        '_identity_map_snapshot':osm,
        '_identity_observed_candidates':[],
        '_camera_position_verified':basis=='original_exif',
        '_location_provenance':{'kind':'owner_approx_camera'} if basis=='owner_approximate' else {},
        '_camera_hints':hints}
    scene=render_scene(story,[])
    if scene is None:
        return {'id':id,'status':'cannot_render_real_osm_map'}
    physical=physical_decision_context(story,[],scene['manifest'])
    source_mime,model_source=normalize_reference(raw)
    source_path=case_dir/'source.model.jpg'
    map_path=case_dir/'map.overview.png'
    if not source_path.exists():
        source_path.write_bytes(model_source)
        os.chmod(source_path,0o600)
    if not map_path.exists():
        map_path.write_bytes(scene['bytes'])
        os.chmod(map_path,0o600)
    save(case_dir/'manifest.json',scene['manifest'])
    save(case_dir/'physical_context.json',physical)
    # Original camera point is never a fitted yaw, pitch or error radius.
    receipt={'id':id,'status':'ready','g_applicable':True,
        'source_path':str(jpg),'original_photo_sha256':sha,
        'original_photo_bytes':len(raw),
        'source_model_image':str(source_path),
        'source_model_sha256':hashlib.sha256(model_source).hexdigest(),
        'source_model_bytes':len(model_source),
        'source_mime':source_mime,
        'map_file':str(map_path),'map_sha256':scene['manifest']['image_sha256'],
        'map_bytes':len(scene['bytes']),
        'map_original_osm':origin,
        'observed_osm_objects':len(osm['observed_pool']),
        'physical_buildings':physical['received_body_count'],
        'camera_point_basis':basis,
        'original_exif_status':exif['status'],
        'source_diagonal_fov_deg':hints.get('diagonal_fov_35mm_deg'),
        'camera_heading_known':hints.get('direction_status')!='missing',
        'model_ground_truth_injected':False,
        'elapsed_map_preparation_s':round(time.monotonic()-start,3),
        'method_calls':0}
    save(result_path,receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',type=int,nargs='*',default=TARGET)
    args=parser.parse_args()
    inputdoc=json.loads(SOURCE_INDEX.read_text())
    if not inputdoc.get('no_expected_identity_or_sid_in_model_input'):
        raise ValueError('Corpus input not certified target-isolated')
    inputs={x['message_id']:x for x in inputdoc['items']}
    if not set(args.ids).issubset(TARGET):
        raise ValueError('Unknown original SOURCE case')
    (OUTPUT/'cases').mkdir(parents=True,exist_ok=True)
    os.chmod(OUTPUT,0o700)
    out=[]
    for id in args.ids:
        try: out.append(prepare(id,inputs))
        except Exception as exc:out.append({'id':id,'status':'preparation_error','error_type':type(exc).__name__,
                                           'error_code':str(exc)[:170]})
    if sorted(args.ids)==sorted(TARGET):
        save(OUTPUT/'g26-preflight.json',{'contract':'source-osm-morphology-preflight-v1',
            'cases':out,'expected_identity_values_loaded':False})
    print(json.dumps({'cases':[{
        'id':r['id'],'status':r['status'],
        'camera_basis':r.get('camera_point_basis'),
        'osm_count':r.get('observed_osm_objects'),
        'physical_bodies':r.get('physical_buildings'),
        'time_s':r.get('elapsed_map_preparation_s'),
        'error':r.get('error_code')} for r in out]},ensure_ascii=False))

if __name__=='__main__':main()
