"""One existing-provider G SOURCE/MAP call per frozen original; active shortlist for T.

This script adds NO queue/service/planner/reviewer. Source and map are the
unchanged g26_prepare_inputs artifacts. Existing application Gemini quota
pool/executor and its before-provider-send journaling are reused, exactly
one key per unit and no auto-switch to another role. UNKNOWN may not be
renamed/deleted to force resend; independent case units continue.

Usage:
 python tools/g26_run.py --ids 106 --model gemini-3.8-flash --dry-run
 python tools/g26_run.py --ids 106 125 132 --model gemini-3.8-flash
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time

from google.genai import types
from jsonschema import Draft202012Validator

from street_story.config import Settings
from street_story.runtime import RuntimeStreetStoryService
from street_story.identity_spatial_choice import visual_spatial_choice_schema
from street_story.identity_spatial_funnel import project_g_funnel
from street_story.identity_spatial_options import for_vision
from street_story.identity_plan_diagnostics import provider_outcome

ROOT=Path('/home/dev/artifacts/street-story/20261009-G-spatial-v3')
INDEX=Path('/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/source-inventory-26.json')
ALL=[102,*range(104,113),*range(118,134)]


def _json(path):
    return json.loads(Path(path).read_text())


def _write_once(path, payload):
    path=Path(path)
    if path.exists():
        raise RuntimeError('frozen_artifact_already_exists:'+path.name)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    os.chmod(tmp,0o600)
    tmp.replace(path)


def compact_packet(packet):
    """Keep ALL physical labels while presenting short scene-independent hints.

    At most one morphology, two wall segments and one *actual* corner option
    per initially expanded OSM body, plus both street directions. This affects
    input size only, NEVER model eligibility or active list cardinality.
    A label outside this detail subset can still be shortlisted/requested.
    """
    counts={}
    visible={}
    for key,record in packet['options'].items():
        kind=record.get('kind')
        label=record.get('body_label')
        if kind=='road_axis_direction':
            visible[key]=record
            continue
        if type(label) is not int:
            continue
        previous=counts.get((label,kind),0)
        limit={'plan_shape':1,'single_frontage':2,
               'observed_corner':1,'physical_pair':1}.get(kind,0)
        if previous<limit:
            visible[key]=record
            counts[(label,kind)]=previous+1
    # Distinct official labels stay exact. The host validates option IDs
    # against this actual presented set, not a larger invisible cache.
    packet={**packet,'options':visible,
            'original_full_option_count':len(packet['options']),
            'input_options_shown':len(visible)}
    return packet


def source_map_prompt(packet):
    """No labels, addresses, ground truth or architecture text inserted by host."""
    return (
        'Original SOURCE photo first; full neutral north-up OSM MAP second. '
        'Your task is G SPATIAL SHORTLIST, not mandatory single-object acceptance. '
        'FIRST examine the photographic scene: front facade, visible side, corner, '
        'neighbor mass, attached or separated volumes, setback sign, street ending, '
        'silhouette and partial/cropped scope. Only THEN compare actual OSM map '
        'labels and host-measured options. '
        'Return decision=shortlist and active_hypotheses with a SCENE-APPROPRIATE '
        'number of physically plausible building labels (not fixed top 3 or 8). '
        'For each state a specific SOURCE spatial compatibility observation '
        'and which visual/map distinction remains unresolved. '
        'Main facade vs immediately receding neighbor may require BOTH bodies active. '
        'Do not discard a candidate merely because the MODEL mislabeled which '
        'connected OSM walls appear on SOURCE; record this uncertainty. '
        'A candidate outside expanded options is still eligible: the complete '
        'physical body index is supplied. request_detail_labels if needed. '
        'Reserve contains other bodies automatically, and can be reopened on '
        'new evidence. Mark explicit_contradictions ONLY for a concrete '
        'PHOTO-to-map spatial disagreement with stated viewing/crop conditions; '
        'unreviewed does NOT mean rejected. '
        'If no meaningful selection is supported, return decision=no_reduction '
        'with active_hypotheses=[], no_reduction_reason and next_useful_step=T '
        'or existing_images. Do not waste another same-input G call. '
        'A sufficient independent distinctive image+MAP match MAY return '
        'decision=accept with a single supported physical label and actual '
        'measurements/options where available; one remaining list member '
        'is NOT enough for acceptance. '
        'For every unresolved scene give t_distinguishing_question: concrete '
        'visible distinguishing issue for an architecture-description method '
        '(e.g. which wing owns a visible turret/portal or which facade turns). '
        'Do not require three geometry features, camera yaw, pitch or exact GPS '
        'accuracy when SOURCE does not show them. OSM is 2D, not 3D occlusion. '
        'EXIF GPS gives origin, not known ±2m accuracy. Elongated footprint '
        'may appear narrow end-on; upper-only telephoto crop is not full '
        'building width. Missing mapped height is UNKNOWN, not a contradiction. '
        'Use actual short option IDs for already calculated geometry when '
        'meaningful; never fabricate an ID or swap the chosen segment to pass. '
        'No Wikipedia article, historic label, address oracle or REF images '
        'are provided. All source-visible semantics come from SOURCE pixels, '
        'not a guessed ground-truth building. Return a filled JSON instance, '
        'not a schema description. Host-provided neutral measurements:\n'
        +json.dumps(for_vision(packet),ensure_ascii=False,separators=(',',':')))


async def run_one(cid,model,*,dry):
    start=time.monotonic()
    case=ROOT/'cases'/str(cid)
    inp=case/'input-receipt.json'
    if not inp.exists():
        return {'id':cid,'status':'missing_geopoint','method_calls':0}
    input_receipt=_json(inp)
    if input_receipt.get('status')!='ready':
        return {'id':cid,'status':input_receipt['status'],'method_calls':0}
    full=_json(case/'spatial-options-v3-llm-first.json')
    packet=compact_packet(full)
    prompt=source_map_prompt(packet)
    schema=visual_spatial_choice_schema()
    import google.genai.types as t
    config=t.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema,max_output_tokens=3700,
        system_instruction='Visually analyze SOURCE and neutral MAP. Return your '
           'actual physical spatial shortlist, distinguishing uncertainty and '
           'T handoff. Do not infer camera pose or emit the schema definition. '
           'No tools, browsing or text-only identity lookup.')
    # Offline SDK struct check before any provider admission.
    encoded=config.model_dump(exclude_none=True,mode='json')
    if 'response_json_schema' not in encoded:
        raise ValueError('provider_did_not_serialize_G_schema')
    path=case/('inference-'+model.replace('/','-'))
    intent=path/'provider-intent.json'
    result_path=path/'result.json'
    if result_path.exists():
        previous=_json(result_path)
        return {'id':cid,'status':'reused_completed_receipt',
                'model':model,'active_count':previous.get('active_count'),
                'accepted':previous.get('accepted'),
                'method_calls':0}
    if intent.exists():
        last=_json(intent)
        return {'id':cid,'status':'existing_attempt_no_resend',
                'model':model,'phase':last.get('phase'),
                'provider_send_state':last.get('provider_send_state'),
                'method_calls':0}
    from devcoveer_story_diag import load_installer
    installer=load_installer()
    for f in (installer.PROVIDERS_ENV,installer.SERVICE_ENV):
        installer.require_mode(f,0o600)
        os.environ.update(installer.parse_dotenv(f))
    os.environ['DATA_DIR']=str(ROOT/'provider-shared-data')
    os.environ['VIBEPUBLISH_BASE_URL']=''
    os.environ.pop('_'.join(('VIBEPUBLISH','BEARER','TOKEN')),None)
    service=RuntimeStreetStoryService(Settings.from_env())
    service.providers.vibepublish=None
    qualification=_json(installer.RESEARCH_QUALIFICATION)
    for key,value in qualification.get('caches',{}).items():
        service.store.cache_put(key,value,3600)
    route=next((v for v in service.providers.gemini.web_search_routes if v[0]==model),None)
    if route is None:
        return {'id':cid,'status':'model_not_registered_in_existing_app_routes','method_calls':0}
    selected_model,pool,quota,executor=route
    state=pool.snapshot('grounded_research')
    if dry:
        return {'id':cid,'status':'dry_run','model':selected_model,
            'healthy_keys':state.get('healthy_keys'),
            'observed_physical_bodies':packet['physical_body_count'],
            'shown_detail_body_count':len(packet['expanded_labels']),
            'shown_option_count':len(packet['options']),
            'full_precomputed_options':packet['original_full_option_count'],
            'all_body_index_count':len(packet['all_received_physical_bodies']),
            'prompt_utf8_bytes':len(prompt.encode()),
            'schema_utf8_bytes':len(json.dumps(schema).encode()),
            'source_image_bytes':input_receipt['source_model_bytes'],
            'map_image_bytes':input_receipt['map_bytes'],
            'method_calls':0}
    if state.get('healthy_keys',0)<1:
        return {'id':cid,'status':'no_healthy_provider_keys','model':model,'method_calls':0}
    src=Path(input_receipt['source_model_image']).read_bytes()
    image=Path(input_receipt['map_file']).read_bytes()
    raw=Path(input_receipt['source_path']).read_bytes()
    original_sha=hashlib.sha256(raw).hexdigest()
    if original_sha!=input_receipt['original_photo_sha256']:
        return {'id':cid,'status':'SOURCE_changed','method_calls':0}
    if hashlib.sha256(image).hexdigest()!=input_receipt['map_sha256']:
        return {'id':cid,'status':'MAP_changed','method_calls':0}
    from g26_prepare_inputs import SOURCE_INDEX, observed_osm
    meta={v['message_id']:v for v in _json(SOURCE_INDEX)['items']}[cid]
    lat,lon=meta['camera_point']
    osm,_=observed_osm(cid,(lat,lon))
    from street_story.camera_hints import read_camera_hints
    from street_story.identity_scene import scene_entries
    story={'latitude':lat,'longitude':lon,'photo_sha256':original_sha,
        '_identity_original_source_sha256':original_sha,
        '_identity_map_snapshot':osm,
        '_camera_position_verified':meta['geographic_basis']=='original_exif',
        '_location_provenance':{'kind':'owner_approx_camera'} if
             meta['geographic_basis']=='owner_approximate_hint' else {},
        '_camera_hints':read_camera_hints(raw)}
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    output_input={'id':cid,'model':model,
        'role':'existing_google_grounded_research_image',
        'original_SOURCE_sha256':original_sha,
        'model_SOURCE_sha256':hashlib.sha256(src).hexdigest(),
        'model_MAP_sha256':hashlib.sha256(image).hexdigest(),
        'model_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'model_schema_sha256':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest(),
        'model_prompt_utf8_bytes':len(prompt.encode()),
        'input_physical_body_count':packet['physical_body_count'],
        'presented_options_count':len(packet['options']),
        'expected_identity_values_loaded':False}
    _write_once(path/'input-receipt.json',output_input)
    # Durable exact actual SOURCE/MAP/prompt before provider send, no overwrite.
    (path/'prompt.txt').write_text(prompt)
    (path/'schema.json').write_text(json.dumps(schema,ensure_ascii=False,indent=2))
    os.chmod(path/'prompt.txt',0o600)
    os.chmod(path/'schema.json',0o600)
    _write_once(path/'sent-options.json',packet)
    request_id=hashlib.sha256(json.dumps([cid,model,output_input],sort_keys=True).encode()).hexdigest()
    marker={'id':cid,'model':model,'phase':'intent',
      'provider_send_state':'not_sent','request_sha256':request_id,
      'original_photo_sha256':original_sha,
      'model_photo_sha256':output_input['model_SOURCE_sha256'],
      'model_map_sha256':output_input['model_MAP_sha256'],
      'key_pool_healthy_before_send':state.get('healthy_keys'),
      'start_elapsed_s':round(time.monotonic()-start,3)}
    # Shared official quota ledger and original provider admission: one key
    # at most, no provider tournament or opportunistic fallback on UNKNOWN.
    pool.policy=replace(pool.policy,max_failover_keys=1)
    def save_marker():
        tmp=path/'provider-intent.tmp'
        tmp.write_text(json.dumps(marker,ensure_ascii=False,indent=2))
        os.chmod(tmp,0o600)
        tmp.replace(intent)
    save_marker()
    def before_send():
        marker.update(phase='send_intent',provider_send_state='possibly_sent')
        save_marker()
    async def ask(key,timeout):
        marker.update(phase='provider_admission',timeout_s=timeout)
        save_marker()
        return await service.providers.gemini._generate(
            key,timeout,[types.Part.from_bytes(data=src,mime_type=input_receipt['source_mime']),
             types.Part.from_bytes(data=image,mime_type='image/png'),prompt],
            config,operation='grounded_research',model=model,quota=quota,
            before_provider_send=before_send)
    try:
        resp=await executor.execute_joint('grounded_research',ask)
    except Exception as exc:
        code,status=provider_outcome(exc)
        marker.update(phase='failed',provider_send_state=code,
            http_status=status,error_type=type(exc).__name__,
            elapsed_ms=round((time.monotonic()-start)*1000))
        save_marker()
        _write_once(result_path,{'id':cid,'status':'provider_failed',
            'technical_send_state':code,'http_status':status,
            'model':model,'model_calls':int(code!='not_sent'),
            'whole_method_elapsed_ms':marker['elapsed_ms'],'accepted':False,
            'active_count':None,'reserve_count':packet['physical_body_count']})
        return {'id':cid,'status':'provider_failed','send_state':code,
            'http_status':status,'error_type':type(exc).__name__,
            'method_calls':int(code!='not_sent'),'elapsed_ms':marker['elapsed_ms']}
    received=resp.text or ''
    (path/'closed-model-response.json').write_text(received)
    os.chmod(path/'closed-model-response.json',0o600)
    marker.update(phase='response_closed',provider_send_state='response_closed',
       response_sha256=hashlib.sha256(received.encode()).hexdigest(),
       usage_total_tokens=getattr(getattr(resp,'usage_metadata',None),'total_token_count',None),
       elapsed_ms=round((time.monotonic()-start)*1000))
    save_marker()
    try:
        model_answer=json.loads(received)
    except ValueError:
        model_answer={}
    errors=list(Draft202012Validator(schema).iter_errors(model_answer))
    handoff=project_g_funnel(model_answer,packet,scene_entries(story,[]),
         source_sha256=original_sha,
         model_source_sha256=output_input['model_SOURCE_sha256'],
         actual_source_sha256=original_sha,
         actual_map_sha256=input_receipt['map_sha256'])
    report={'id':cid,'model':model,'status':handoff['status'],
        'accepted':handoff.get('accepted',False),
        'accepted_id':handoff.get('accepted_id'),
        'active_count':handoff.get('active_count',0),
        'reserve_count':handoff.get('reserve_count',packet['physical_body_count']),
        'physical_body_count':packet['physical_body_count'],
        'active_physical_candidates':[{
            'candidate_id':x['candidate_id'],'label':x['label'],
            'source_match':x['source_match'],
            'unresolved_difference':x['unresolved_difference']}
            for x in handoff.get('active') or []],
        't_question':handoff.get('t_distinguishing_question'),
        'next_step':handoff.get('next_step'),
        'provider_phase':'response_closed','model_calls':1,
        'total_tokens':marker['usage_total_tokens'],
        'whole_method_elapsed_ms':marker['elapsed_ms'],
        'schema_errors':[list(e.absolute_path) for e in errors],
        'source_sha256':original_sha,'map_sha256':input_receipt['map_sha256'],
        'raw_response_sha256':marker['response_sha256'],
        'oracle_absent_from_model':True}
    _write_once(path/'funnel-handoff.json',handoff)
    _write_once(result_path,report)
    return report


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',type=int,nargs='+',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    if not set(args.ids).issubset(ALL):
        raise ValueError('Requested unknown original SOURCE photo')
    rows=[]
    for id in args.ids:
        try:rows.append(await run_one(id,args.model,dry=args.dry_run))
        except Exception as exc:rows.append({'id':id,'status':'pre_send_or_local_error',
             'error_type':type(exc).__name__,'code':str(exc)[:200],
             'method_calls':0})
    print(json.dumps({'model':args.model,'cases':rows},ensure_ascii=False))
if __name__=='__main__':
    asyncio.run(main())
