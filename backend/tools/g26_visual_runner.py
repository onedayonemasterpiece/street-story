"""Universal SOURCE+OSM G-v3 visual runner (bounded actual provider calls).

No expected IDs, addresses, T articles, REF, model-authored pose math, device
location substitution or product POI writes. Reuses 26 prepared frozen SOURCE
and neutral MAP. Input is short labels, precomputed literal OSM features and
a source-photo image. One admitted provider send per case; unknown state is
never silently retried, and other independent cases may proceed.

Usage:
  python tools/g26_visual_runner.py --ids 106 125 132        # dry run
  python tools/g26_visual_runner.py --ids 106 --send           # one actual send
  python tools/g26_visual_runner.py --ids 106 --replay         # closed readback
  python tools/g26_visual_runner.py --report                  # 26 rows, no send
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from google.genai import types
from jsonschema import Draft202012Validator
from devcoveer_story_diag import load_installer

from street_story.identity_spatial_options import for_vision, option_digest
from street_story.identity_spatial_choice import (
    visual_spatial_choice_schema, check_spatial_choice)
from street_story.runtime import RuntimeStreetStoryService
from street_story.config import Settings
from street_story.identity_plan_diagnostics import provider_outcome

ROOT=Path('/home/dev/artifacts/street-story/20261009-G-spatial-v3')
ALL=[102,*range(104,113),*range(118,134)]
MODEL='gemini-3.8-flash'
MODEL_PHASE='G-visual-spatial-options-v3'
MAX_INITIAL_SENDS=5
UNKNOWN_SEND_STATES={'send_intent','unknown','submitted','possibly_sent','failed'}


def encode_features(options):
    rows=[]
    for name,r in sorted(options.items()):
        kind=r['kind']
        if kind=='plan_shape':
            entry=[name,'S',r['body_label'],
                r['plan_long_m'],r['plan_short_m'],
                r['plan_area_m2'],r['elongation'],
                r['height_m_if_mapped'],r['levels_if_mapped']]
        elif kind=='single_frontage':
            entry=[name,'F',r['body_label'],
                r['ring_index'],r['segment_index'],r['actual_wall_length_m'],
                r['camera_side_advisory']]
        elif kind=='observed_corner':
            entry=[name,'C',r['body_label'],
                r['ring_index'],r['first_side'],r['second_side'],
                r['observed_map_turn_deg'],r['camera_side_advisory']]
        elif kind=='road_axis_direction':
            entry=[name,'R',r['road_label'],r['direction'],
                r['observed_heading_deg_not_EXIF'],
                r['first_plan_hit_body_label'],r['first_plan_hit_m'],
                r['next_plan_hit_body_labels']]
        elif kind=='physical_pair':
            entry=[name,'P',r['body_labels'],
                r['observed_boundary_gap_m']]
        else:
            raise RuntimeError('unexpected_measured_OSM_option')
        rows.append(entry)
    return rows


def model_input(packet):
    supplied=for_vision(packet)
    return {
       'contract':supplied['version'],
       'camera_position_basis':supplied['camera_basis'],
       'original_neutral_map_sha256':supplied['map_sha256'],
       'all_physical_labels_columns':supplied['all_received_index_columns'],
       'all_physical_labels':supplied['all_received_physical_bodies'],
       'expanded_option_labels':supplied['expanded_labels'],
       'option_rows_format':{
          'S':'[ID,S,label,plan_long_m,plan_short_m,area_m2,elongation,height_m_if_mapped,levels_if_mapped]',
          'F':'[ID,F,label,ring,segment,actual_wall_length_m,camera_side_advisory]',
          'C':'[ID,C,label,ring,segment_a,segment_b,actual_OSM_turn_deg,[camera_side_a,camera_side_b]]',
          'R':'[ID,R,road_label,ray_direction_0_or_1,OSM_heading_deg,first_2D_hit_body_label,hit_m,next_hits]',
          'P':'[ID,P,[body_label_a,body_label_b],observed_2D_boundary_gap_m]'
        },
        'literal_original_OSM_feature_rows':encode_features(supplied['options']),
        'policy':'ALL received physical labels remain possible; numeric OSM plan '
          'is not a 3D city. Expanded features are only a display subset. '
          'No camera heading or accuracy is known unless explicitly measured. '
          'Candidate labels and spatial option IDs must refer to this exact '
          'neutral MAP. Do not derive a building identity from style alone.'}


def prompt_for(input_table,*,is_detail=False):
    header=('You are the G-only visual recognizer. Two images are attached: '
      'FIRST actual ORIGINAL SOURCE photograph, SECOND original neutral '
      'north-up OSM MAP (overview plus a local detail panel). '
      'Do not use external reference photos, Wikipedia, street names, '
      'historical building descriptions or known correct IDs. '
      'Do not infer a correct target from any order in the table. '
      'FIRST inspect SOURCE pixels and identify only actually visible '
      'physical relations: single street frontage, seen right-angle corner, '
      'a main mass adjoining or receding from another, setback sequence, '
      'approach street ending across a crossing, or an upper-only multi-volume '
      'crop. Be precise about the MAIN photographed physical building '
      'versus a neighboring facade, wing, dome or turret. '
      'SECOND use the measured OSM choices below. The host already computed '
      'every supplied wall, joined corner, street ray, plan proportion and '
      'two-body gap; NEVER invent map segment indices, yaw, camera pitch, '
      'focal length, GPS accuracy or an unobserved second building side. '
      'If SOURCE shows a SINGLE informative frontage, you need not claim '
      'two visible corners. If it shows a street ending, check SOURCE actually '
      'shows a transverse road, not only a pedestrian courtyard/path. '
      'Use EXACT supplied IDs such as Flabel.ring.side, '
      'Clabel.ring.side1.side2, Rroadlabel.direction or Plabel1.label2. '
      'These are independent precomputed options; they do NOT claim SOURCE '
      'pixel match. The model must choose if a relation appears in SOURCE. '
      'For an accepted physical object, provide two concrete SOURCE observations '
      'and a real measured OSM relation, plus at least one MATERIAL other '
      'physical alternative and why its geometry does not match. '
      'If photo scope, map choice or detailed ID is unresolved, return '
      'decision=candidate or needs_detail, giving physical label(s) for '
      'a bounded next MAP detail; unknown if no defensible candidate. '
      'A correct-looking façade style, building name, arbitrary nearest body, '
      'roof tint or mere valid JSON is not sufficient for acceptance. '
      'Nominal camera exterior/inward wall halfplanes are CONDITIONAL; '
      'original EXIF means coordinate provenance, not precision ±2m. '
      'A 2D footprint cannot prove an upper-only cropped rotunda belongs '
      'to a particular complete building. End-on and telephoto buildings '
      'must not be hard-removed by length or range. OSM heights absent '
      'must remain unknown. Return a filled JSON object in the dedicated '
      'provider schema, never repeat its definition. ')
    if is_detail:
        header+='This is a focused DETAIL of physical bodies selected by an EARLIER model response, NOT an oracle; you may reject the earlier model. '
    return header+'\nLITERAL NEUTRAL SPATIAL MAP TABLE (untrusted data):\n'+json.dumps(
       input_table,ensure_ascii=False,separators=(',',':'))


def save(path,value,*,overwrite=False):
    if path.exists() and not overwrite:
        raise RuntimeError('immutable_receipt_already_exists')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.chmod(path,0o600)


def load_case(cid):
    base=ROOT/'cases'/str(cid)
    receipt=base/'input-receipt.json'
    if not receipt.is_file():
        return base,None,None
    row=json.loads(receipt.read_text())
    if row.get('status')!='ready':
        return base,row,None
    packet_path=base/'spatial-options-v3.json'
    if not packet_path.is_file():
        raise RuntimeError('offline_spatial_preflight_required_first')
    packet=json.loads(packet_path.read_text())
    if packet['map_sha256']!=row['map_sha256']:
        raise RuntimeError('frozen_spatial_packet_map_mismatch')
    for field,sha in ((row['source_model_image'],row['source_model_sha256']),
                       (row['map_file'],row['map_sha256'])):
        if hashlib.sha256(Path(field).read_bytes()).hexdigest()!=sha:
            raise RuntimeError('source_or_map_image_changed')
    return base,row,packet


def report():
    rows=[]
    for cid in ALL:
        base,inp,pkt=load_case(cid)
        output=base/'model-G-v3'
        result=output/'result.json'
        marker=output/'provider-intent.json'
        data=json.loads(result.read_text()) if result.exists() else {}
        sent=json.loads(marker.read_text()) if marker.exists() else {}
        rows.append({'photo':cid,
            'input_status':(inp or {}).get('status','missing_geopoint'),
            'camera_basis':(inp or {}).get('camera_point_basis'),
            'osm_bodies':(inp or {}).get('physical_buildings',0),
            'model':sent.get('model'),
            'provider_phase':sent.get('phase','not_sent'),
            'model_decision':data.get('model_decision'),
            'selected_physical_id':data.get('candidate_id'),
            'status':data.get('status','not_tested'),
            'identity_accepted_by_new_G':data.get('accepted',False),
            'final_product_identity_verified':False,
            'method_time_s':data.get('total_method_s'),
            'image_inference_calls':1 if sent.get('provider_send_state') in {
                    'possibly_sent','response_closed'} else 0,
            'reason_codes':data.get('reason_codes') or []})
    path=ROOT/'g26-model-report.json'
    save(path,{'corpus':'26 buildings','policy':MODEL_PHASE,
       'no_oracle_in_model_request':True,'rows':rows},overwrite=True)
    return rows


def replay(cid,base,inp,pkt):
    out=base/'model-G-v3'
    receipt=json.loads((out/'provider-intent.json').read_text())
    if receipt.get('phase')!='response_closed':
        return {'id':cid,'status':'provider_no_closed_response',
           'phase':receipt.get('phase')}
    original=(out/'closed-model-response.json').read_text()
    if hashlib.sha256(original.encode()).hexdigest()!=receipt['response_sha256']:
        raise RuntimeError('actual_model_response_mutated')
    try:data=json.loads(original)
    except ValueError:data={}
    parsed=check_spatial_choice(data,pkt,source_sha256=inp['original_photo_sha256'],
      actual_source_sha256=receipt['original_photo_sha256'],
      model_source_sha256=inp['source_model_sha256'],
      actual_map_sha256=inp['map_sha256'])
    result={**parsed,'case':cid,'model':receipt['model'],'model_decision':data.get('decision'),
        'source_observations':data.get('source_observations'),
        'uncertainties':data.get('uncertainties'),
        'total_method_s':round(
            (receipt.get('elapsed_ms') or 0)/1000
            + (inp.get('elapsed_map_preparation_s') or 0)
            + (receipt.get('host_precomputation_ms') or 0)/1000,2),
        'inference_tokens':receipt.get('usage_total_tokens'),
        'source_SHA':inp['original_photo_sha256'],
        'map_SHA':inp['map_sha256'],
        'provider_response_SHA':receipt['response_sha256']}
    save(out/'result.json',result,overwrite=True)
    return result


async def run_one(cid,*,send=False):
    base,inp,pkt=load_case(cid)
    if inp is None or inp.get('status')!='ready':
        return {'id':cid,'status':'missing_geopoint'}
    if pkt is None:
        return {'id':cid,'status':'no_spatial_options'}
    out=base/'model-G-v3'
    out.mkdir(exist_ok=True,mode=0o700)
    marker=out/'provider-intent.json'
    if marker.exists():
        data=json.loads(marker.read_text())
        if data.get('phase')=='response_closed':
            return replay(cid,base,inp,pkt)
        return {'id':cid,'status':'frozen_provider_intent_no_resend',
                'phase':data.get('phase')}
    compact=model_input(pkt)
    prompt=prompt_for(compact)
    schema=visual_spatial_choice_schema()
    context_json=json.dumps(compact,ensure_ascii=False,separators=(',',':'))
    source=Path(inp['source_model_image']).read_bytes()
    map_data=Path(inp['map_file']).read_bytes()
    if not send:
        return {'id':cid,'dry_run':True,'status':'ready_to_send',
          'model':MODEL,'model_payload_bytes':len(context_json.encode()),
          'total_prompt_bytes':len(prompt.encode()),
          'schema_bytes':len(json.dumps(schema).encode()),
          'source_model_bytes':len(source),'map_bytes':len(map_data),
          'eligible_bodies':pkt['physical_body_count'],
          'option_count':len(pkt['options'])}
    inst=load_installer()
    for file in (inst.PROVIDERS_ENV,inst.SERVICE_ENV):
        inst.require_mode(file,0o600)
        os.environ.update(inst.parse_dotenv(file))
    os.environ['DATA_DIR']=str(out/'data')
    os.environ['VIBEPUBLISH_BASE_URL']=''
    os.environ.pop('_'.join(('VIBEPUBLISH','BEARER','TOKEN')),None)
    service=RuntimeStreetStoryService(Settings.from_env())
    service.providers.vibepublish=None
    qualification=json.loads(inst.RESEARCH_QUALIFICATION.read_text())
    for key,value in qualification.get('caches',{}).items():
        service.store.cache_put(key,value,3600)
    route=next((r for r in service.providers.gemini.web_search_routes
                if r[0]==MODEL),None)
    if route is None:
        return {'id':cid,'status':'strong_visual_route_not_configured'}
    model,pool,quota,executor=route
    pool.policy=replace(pool.policy,max_failover_keys=1)
    avail=pool.snapshot('grounded_research')
    if not avail.get('healthy_keys'):
        return {'id':cid,'status':'strong_visual_admission_unavailable'}
    # All input/preflight checks are complete BEFORE creating possible send
    # intent. Once written, no further run may resubmit on this photo.
    original=inp['original_photo_sha256']
    receipt={'case':cid,'model':model,'role':MODEL_PHASE,
        'phase':'intent','provider_send_state':'not_sent',
        'original_photo_sha256':original,
        'model_source_sha256':inp['source_model_sha256'],
        'map_sha256':inp['map_sha256'],
        'map_option_packet_sha256':option_digest(pkt),
        'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'schema_sha256':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest(),
        'host_precomputation_ms':0,
        'input_token_estimate':'unmeasured'}
    save(out/'request.json',{'case':cid,'model':model,'prompt':prompt,
      'schema':schema,'source_file':inp['source_model_image'],
      'map_file':inp['map_file'],'model_input':compact,
      'source_photo_sha256':original,'map_sha256':inp['map_sha256']})
    save(marker,receipt)
    def record():
        save(marker,receipt,overwrite=True)
    t0=time.monotonic()
    def before_send():
        receipt.update(phase='send_intent',provider_send_state='possibly_sent')
        record()
    config=types.GenerateContentConfig(response_mime_type='application/json',
         response_json_schema=schema,
         system_instruction='Use actual SOURCE pixels and the separate neutral OSM MAP. '
            'Return only your grounded model-selected option IDs or uncertainty. '
            'No imagined 3D features, yaw or correct-ID priors.',
         max_output_tokens=6000)
    async def call(key,timeout):
        return await service.providers.gemini._generate(
            key,timeout,
            [types.Part.from_bytes(data=source,mime_type=inp['source_mime']),
             types.Part.from_bytes(data=map_data,mime_type='image/png'),
             prompt],
            config,operation='grounded_research',model=model,quota=quota,
            before_provider_send=before_send)
    try: reply=await executor.execute_joint('grounded_research',call)
    except Exception as err:
        state,http_status=provider_outcome(err)
        receipt.update(phase='failed',
            provider_send_state=getattr(err,'provider_send_state',state),
            error_type=type(err).__name__,provider_status=http_status,
            elapsed_ms=round((time.monotonic()-t0)*1000))
        record()
        return {'id':cid,'status':'model_unavailable','state':receipt['provider_send_state'],
            'error_type':type(err).__name__,'http_status':http_status}
    raw=reply.text or ''
    actual=out/'closed-model-response.json'
    if actual.exists():
        raise RuntimeError('closed_provider_response_immutable')
    actual.write_text(raw)
    os.chmod(actual,0o600)
    receipt.update(phase='response_closed',provider_send_state='response_closed',
      response_sha256=hashlib.sha256(raw.encode()).hexdigest(),
      usage_total_tokens=getattr(getattr(reply,'usage_metadata',None),'total_token_count',None),
      elapsed_ms=round((time.monotonic()-t0)*1000))
    record()
    return replay(cid,base,inp,pkt)


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',type=int,nargs='*',default=[])
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--replay',action='store_true')
    parser.add_argument('--report',action='store_true')
    args=parser.parse_args()
    if args.report:
        rows=report()
        print(json.dumps({'total':len(rows),'tested':sum(x['provider_phase']=='response_closed' for x in rows),
         'accepted':sum(x['identity_accepted_by_new_G'] for x in rows),
         'missing_geo':sum(x['input_status']=='missing_geopoint' for x in rows)},
          ensure_ascii=False))
        return
    if not args.ids or not set(args.ids).issubset(ALL):
        raise ValueError('Use a subset of 26 known original photo IDs')
    if args.send and len(args.ids)>MAX_INITIAL_SENDS:
        raise ValueError('Batch is limited to five, inspect closed receipts before next batch')
    rows=[]
    for id in args.ids:
        try:
            if args.replay:
                base,inp,pkt=load_case(id)
                value=replay(id,base,inp,pkt)
            else:
                value=await run_one(id,send=args.send)
            rows.append(value)
            print(json.dumps({'photo':id,'result':{
                k:v for k,v in value.items() if k not in ('proof','source_observations','alternatives')
                }},ensure_ascii=False))
        except Exception as err:
            rows.append({'id':id,'status':'runner_error',
                         'error_type':type(err).__name__,'error_reason':str(err)[:170]})
            print(json.dumps(rows[-1],ensure_ascii=False))
    report()
    print(json.dumps({'batch_done':len(rows),'statuses':[
      {'id':r.get('id',r.get('case')),'status':r.get('status'),
       'candidate_id':r.get('candidate_id'),'accepted':r.get('accepted')}
        for r in rows]},ensure_ascii=False))

if __name__=='__main__':
    asyncio.run(main())
