"""Evaluate universal host spatial-option and actual-map applicability, 26/26.
No model invocation, no target oracle, no T data. True SOURCE/MAP hashes
are read from frozen preparation receipts.
"""
import argparse
import json
import hashlib
import os
import time
from pathlib import Path
from street_story.identity_spatial_options import (
    spatial_option_catalog, for_vision, option_digest)

ROOT=Path('/home/dev/artifacts/street-story/20261009-G-spatial-v3')
ALL=[102,*range(104,113),*range(118,134)]

def preflight(cid):
    start=time.monotonic()
    case=ROOT/'cases'/str(cid)
    receipt=case/'input-receipt.json'
    if not receipt.is_file():
        return {'id':cid,'status':'missing_geopoint','physical_count':0,'option_count':0,
                'method_calls':0}
    source=json.loads(receipt.read_text())
    if source['status']!='ready':
        return {'id':cid,'status':source['status'],'physical_count':0,'method_calls':0}
    if hashlib.sha256(Path(source['map_file']).read_bytes()).hexdigest()!=source['map_sha256']:
        return {'id':cid,'status':'map_sha_mismatch','method_calls':0}
    manifest=json.loads((case/'manifest.json').read_text())
    physical=json.loads((case/'physical_context.json').read_text())
    # Compact options require the original received OSM geometry and SOURCE
    # camera. Use exactly the saved case receipt's camera and no target.
    from g26_prepare_inputs import SOURCE_INDEX, observed_osm
    index={v['message_id']:v for v in json.loads(SOURCE_INDEX.read_text())['items']}
    meta=index[cid]
    camera=tuple(meta['camera_point'])
    osm,_source=observed_osm(cid,camera)
    from street_story.camera_hints import read_camera_hints
    story={'latitude':camera[0],'longitude':camera[1],
         '_identity_map_snapshot':osm,
         '_camera_hints':read_camera_hints(Path(meta['source_path']).read_bytes()),
         '_camera_position_verified':meta['geographic_basis']=='original_exif',
         '_location_provenance':{'kind':'owner_approx_camera'}
            if meta['geographic_basis']=='owner_approximate_hint' else {}}
    packet=spatial_option_catalog(story,[],manifest,physical)
    displayed=for_vision(packet)
    assert packet['map_sha256']==source['map_sha256']
    assert len(packet['all_received_physical_bodies'])==physical['received_body_count']
    assert len(packet['private_label_to_osm_id'])>=physical['received_body_count']
    assert 'private_label_to_osm_id' not in displayed
    option_rows=list(packet['options'].values())
    kinds={k:sum(x.get('kind')==k for x in option_rows)
        for k in ('plan_shape','single_frontage','observed_corner',
                  'road_axis_direction','physical_pair')}
    result={'id':cid,'status':'usable_spatial_options',
        'camera_basis':source['camera_point_basis'],
        'physical_count':packet['physical_body_count'],
        'expanded_body_count':len(packet['expanded_labels']),
        'option_count':len(packet['options']),'option_kinds':kinds,
        'full_index_count':len(packet['all_received_physical_bodies']),
        'serialised_model_bytes':len(json.dumps(displayed,ensure_ascii=False).encode()),
        'options_sha256':option_digest(packet),
        'method_calls':0,'precompute_ms':round((time.monotonic()-start)*1000,1)}
    detail_path=case/'spatial-options-v3.json'
    if not detail_path.exists():
        detail_path.write_text(json.dumps(packet,ensure_ascii=False,indent=2))
        os.chmod(detail_path,0o600)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--ids',type=int,nargs='*',default=ALL)
    args=p.parse_args()
    rows=[]
    for cid in args.ids:
        try:rows.append(preflight(cid))
        except Exception as exc:
            rows.append({'id':cid,'status':'option_error',
                'error_type':type(exc).__name__,'error_code':str(exc)[:240],
                'method_calls':0})
    if set(args.ids)==set(ALL):
        path=ROOT/'g26-spatial-preflight.json'
        path.write_text(json.dumps({'contract':'G-options-v3',
            'oracle_not_loaded':True,'cases':rows},ensure_ascii=False,indent=2))
        os.chmod(path,0o600)
    print(json.dumps({'total':len(rows),'usable':sum(x['status']=='usable_spatial_options' for x in rows),
        'errors':[{k:v for k,v in row.items() if k in ('id','status','error_type','error_code')}
          for row in rows if row['status']!='usable_spatial_options'],
        'cases':[{
            'id':row['id'],'status':row['status'],
            'bodies':row.get('physical_count'),'options':row.get('option_count'),
            'types':row.get('option_kinds'),'packet_bytes':row.get('serialised_model_bytes'),
            'ms':row.get('precompute_ms')} for row in rows]},ensure_ascii=False))
if __name__=='__main__':main()
