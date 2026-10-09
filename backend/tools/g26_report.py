"""One-row-per-original G funnel progress/evaluation; POSTHOC oracle only.

Read original receipt/model outcomes and *only here* load owner ground truth.
Never imported from g26_run.py or transmitted to a provider. A legacy closed
receipt replay is displayed in separate columns and is NOT a fresh cold hit.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT=Path('/home/dev/artifacts/street-story/20261009-G-spatial-v3')
MANIFEST=Path('/home/dev/artifacts/street-story/20261008T172026Z-product-recovery-audit-implementation-20261008/acceptance-manifest-v3.json')
ALL=[102,*range(104,113),*range(118,134)]


def _json(path):
    return json.loads(Path(path).read_text())


def one(cid, model):
    case=ROOT/'cases'/str(cid)
    input_file=case/'input-receipt.json'
    meta=_json(input_file) if input_file.exists() else {'status':'missing_geopoint'}
    packet_file=case/'spatial-options-v3-llm-first.json'
    packet=_json(packet_file) if packet_file.exists() else {}
    initial=packet.get('physical_body_count') or meta.get('physical_buildings') or 0
    model_file=case/('inference-'+model)/'result.json'
    active=[]
    reason=meta['status']
    latency=None
    calls=0
    accepted_id=None
    status=('missing_geopoint' if not meta.get('g_applicable') else 'not_yet_sent')
    if model_file.exists():
        data=_json(model_file)
        status=data.get('status','unknown')
        active=[x.get('candidate_id') for x in data.get('active_physical_candidates') or []
                if isinstance(x,dict)]
        accepted_id=data.get('accepted_id')
        reason=data.get('safe_error_code') or (
            ';'.join(data.get('schema_errors') or [])
            if isinstance(data.get('schema_errors'),list) else
            data.get('technical_send_state') or status)
        latency=data.get('whole_method_elapsed_ms')
        calls=data.get('model_calls',0)
    else:
        reason='not_started' if status=='not_yet_sent' else reason
    return {'id':cid,'geopoint':meta.get('camera_point_basis'),
        'input_status':meta['status'],'initial_physical_count':initial,
        'G_model_status':status,'active_count':len(active) if model_file.exists() and
            status in {'active_shortlist','accepted_identity_proposal'} else None,
        'active_ids':active,'reserve_count':initial-len(active)
            if status in {'active_shortlist','accepted_identity_proposal'} else initial,
        'accepted_id':accepted_id,'model_calls':calls,'total_method_ms':latency,
        'reason':reason}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    oracle={row['message_id']:row.get('expected_physical_id')
       for row in _json(MANIFEST)['items']}
    previous=ROOT/'g26-closed-prior-leads-physical-v2.json'
    legacy={row['id']:row for row in _json(previous)['cases']} if previous.exists() else {}
    rows=[one(cid,args.model) for cid in ALL]
    counts={'g_input_ready':0,'g_input_missing':0,'new_model_attempted':0,
      'new_response_with_active_group':0,'correct_accepted':0,'wrong_accepted':0,
      'correct_active_shortlisted':0,'wrong_active_shortlisted':0,
      'known_expected_active_evaluable':0,
      'new_no_reduction':0,'new_provider_blocked':0}
    for item in rows:
        expected=oracle.get(item['id'])
        hit=expected in item['active_ids'] if expected else None
        item['expected_known_posthoc']=bool(expected)
        item['expected_in_active_posthoc']=(hit if item['G_model_status']=='active_shortlist'
           and expected else None)
        item['accepted_correct_posthoc']=(
           item['accepted_id']==expected if expected and item['accepted_id'] else None)
        baseline=legacy.get(item['id'])
        item['legacy_closed_replay']={
          'active_count':baseline['active_count'],
          'original_physical_count':baseline['observed_body_count'],
          'expected_active_posthoc':(expected in baseline['active_physical_ids']
                                      if expected else None),
          'not_new_cold_result':True
          } if baseline else None
        if item['input_status']=='ready':counts['g_input_ready']+=1
        else:counts['g_input_missing']+=1
        if item['model_calls']>0:counts['new_model_attempted']+=1
        if item['G_model_status']=='active_shortlist':
            counts['new_response_with_active_group']+=1
            if expected:
                counts['known_expected_active_evaluable']+=1
                counts['correct_active_shortlisted' if hit else
                       'wrong_active_shortlisted']+=1
        if item['G_model_status']=='no_useful_reduction':counts['new_no_reduction']+=1
        if item['G_model_status']=='provider_failed':counts['new_provider_blocked']+=1
        if item['accepted_id'] and expected:
            counts['correct_accepted' if item['accepted_id']==expected else
                   'wrong_accepted']+=1
    report={'contract':'G-shortlist-retrospective-not-independent-holdout',
      'model':args.model,'oracle_read_only_by_posthoc_report':True,
      'counts':counts,'cases':rows}
    path=ROOT/('g26-progress-'+args.model+'.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'model':args.model,'counts':counts,
      'cases':[{
         'id':x['id'],'input':x['input_status'],
         'physical':x['initial_physical_count'],
         'G':x['G_model_status'],'active':x['active_count'],
         'reserve':x['reserve_count'],
         'expected_active':x['expected_in_active_posthoc'],
         'previous_active':(x['legacy_closed_replay'] or {}).get('active_count'),
         'previous_expected_hit':(x['legacy_closed_replay'] or {}).get('expected_active_posthoc'),
         'calls':x['model_calls'],'time_ms':x['total_method_ms']} for x in rows]},
         ensure_ascii=False))
if __name__=='__main__':
    main()
