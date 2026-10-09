"""Strictly POST-INFERENCE private scorer for 26-case G-v3.

This reads accepted model receipts already closed BEFORE opening manual
expected physical IDs. No oracle value or derived label appears in model
requests, schemas, map labels, detail choice or transport. A physically
unconfirmed correct lead is distinct from autonomous G accepted identity.
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT=Path('/home/dev/artifacts/street-story/20261009-G-spatial-v3')
PRIVATE=Path('/home/dev/artifacts/street-story/20261009T111952Z-architecture-t-full-corpus-20261009/t-evidence-matrix-26.json')
META=PRIVATE.parent/'source-inventory-26.json'
ALL=[102,*range(104,113),*range(118,134)]


def main():
    manual=json.loads(PRIVATE.read_text())
    truth={int(x['photo']):((x.get('independent_post_inference_control') or {}).get(
        'manual_target_physical_id')) for x in manual['cases']}
    source={x['message_id']:x for x in json.loads(META.read_text())['items']}
    assert set(truth)==set(ALL)==set(source)
    outcome=[]
    for cid in ALL:
        case=ROOT/'cases'/str(cid)
        source_ready=(case/'input-receipt.json').is_file()
        if not source_ready:
            outcome.append({'photo':cid,'input':'missing_geopoint',
                'grade':'missing_required_input','model_calls_confirmed_or_possibly_sent':0})
            continue
        receipt=json.loads((case/'input-receipt.json').read_text())
        modes=[case/'model-G-v3',
            case/'model-G-v3-gemini-3.1-flash-lite',
            case/'model-G-v3-gemini-3.5-flash-lite',
            case/'model-G-v3-detail',
            case/'model-G-v3-gemini-3.1-flash-lite-detail',
            case/'model-G-v3-gemini-3.5-flash-lite-detail']
        actual=[]
        for d in modes:
            mark=d/'provider-intent.json'
            if not mark.is_file():continue
            r=json.loads(mark.read_text())
            result_file=d/'result.json'
            answer=json.loads(result_file.read_text()) if result_file.is_file() else {}
            actual.append((d.name,r,answer))
        closed=[x for x in actual if x[1].get('phase')=='response_closed']
        chosen=closed[-1] if closed else None
        tested=bool(closed)
        accepted=bool(chosen and chosen[2].get('accepted'))
        physical=chosen[2].get('candidate_id') if chosen else None
        reference=truth[cid]
        known=isinstance(reference,str) and reference.startswith('osm:')
        correct=(physical==reference if known and physical else None)
        if not tested:
            grade='no_closed_visual_model_result'
        elif accepted and correct is True:
            grade='correct_accepted_G_v3'
        elif accepted and correct is False:
            grade='wrong_accepted_G_v3'
        elif accepted and correct is None:
            grade='accepted_not_gradable'
        elif physical and correct is True:
            grade='correct_unconfirmed'
        elif physical and correct is False:
            grade='wrong_unconfirmed'
        else:
            grade='model_unknown_or_no_physical_nominee'
        sends=sum(1 for _,mark,_ in actual if mark.get('provider_send_state') in {
                'possibly_sent','response_closed'})
        # Original-old-106 Gemini3.8 local reservation may have an unknown
        # class but no SDK callback; count as unconfirmed, not a forced send.
        events=[{'model':m.get('model'),'phase':m.get('phase'),
              'send_state':m.get('provider_send_state')}
               for _,m,_ in actual]
        row={'photo':cid,'input':receipt.get('status'),
            'physical_scope_model':physical,
            'model':chosen[1].get('model') if chosen else None,
            'model_decision':chosen[2].get('model_decision') if chosen else None,
            'g3_acceptance':accepted,'grade':grade,
            'model_calls_confirmed_or_possibly_sent':sends,
            'provider_events':events,
            'time_all_method_s':chosen[2].get('total_method_s') if chosen else None,
            'host_reason_codes':chosen[2].get('reason_codes') if chosen else []}
        outcome.append(row)
    summary={grade:sum(x['grade']==grade for x in outcome) for grade in sorted(
        {x['grade'] for x in outcome})}
    aggregate={'contract':'G-v3-sealed-POST-inference-grading',
       'corpus_size':len(ALL),'geo_applicable':sum(x['input']=='ready' for x in outcome),
       'correct_accepted':summary.get('correct_accepted_G_v3',0),
       'wrong_accepted':summary.get('wrong_accepted_G_v3',0),
       'correct_unconfirmed':summary.get('correct_unconfirmed',0),
       'wrong_unconfirmed':summary.get('wrong_unconfirmed',0),
       'UNKNOWN_or_no_result':sum(v for k,v in summary.items()
         if k in {'model_unknown_or_no_physical_nominee','no_closed_visual_model_result'}),
       'no_geo':summary.get('missing_required_input',0),
       'total_recorded_provider_sends':sum(x['model_calls_confirmed_or_possibly_sent'] for x in outcome),
       'categories':summary,
       'manual_oracle_never_inferred_input':True}
    out=ROOT/'g26-sealed-quality-report.json'
    out.write_text(json.dumps({'summary':aggregate,'cases':outcome},
        ensure_ascii=False,indent=2))
    print(json.dumps({'summary':aggregate,'evaluated':[
        {k:v for k,v in x.items() if k in ('photo','input','grade','model','physical_scope_model',
            'model_decision','g3_acceptance','time_all_method_s','model_calls_confirmed_or_possibly_sent')}
          for x in outcome]},ensure_ascii=False))


if __name__=='__main__':
    main()
