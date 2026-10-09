"""One G model call => accepted body OR active shortlist OR no useful reduction.

This is a response adapter for the EXISTING SOURCE+OSM G call, not an
independent planner/reviewer. An image-using model must choose which physical
bodies remain plausible, and describe unresolved SOURCE distinctions. The
host resolves only immutable received OSM labels and retains everything else
in reversible reserve. Do not equate unreviewed with contradicted or promote a
single shortlist member to POI identity. SOURCE/T factual correctness is not
asserted by this adapter; the full product acceptance stays with Codex.
"""
from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator

from .identity_spatial_choice import visual_spatial_choice_schema, check_spatial_choice


def _physical_metadata(entry):
    """Only literal observed OSM fields, no historic-name resolution."""
    item=entry or {}
    tags={
        **(item.get('tags') or {}),
        **((item.get('map_object') or {}).get('tags') or {}),
    }
    address={key:tags[key] for key in (
        'addr:street','addr:housenumber','addr:city','addr:postcode',
        'addr:unit','addr:entrance') if isinstance(tags.get(key),str)}
    sources={key:tags[key] for key in (
        'wikipedia','wikidata','website','contact:website','source',
        'heritage:website','ref:whc') if isinstance(tags.get(key),str)}
    entrance_ids=item.get('building_entrance_node_ids') or []
    return {'literal_address':address,'observed_source_links':sources,
        'observed_entrance_node_ids':entrance_ids[:20] if isinstance(entrance_ids,list) else [],
        'osm_type':item.get('type') or
            (item.get('map_object') or {}).get('type')}


def project_g_funnel(model_output, options_packet, observed_entries, *,
                     source_sha256, model_source_sha256, actual_source_sha256,
                     actual_map_sha256):
    """Validate and persist reversible, source-bounded model-selected active bodies.

    New v3 responses may supply active_hypotheses with arbitrary scene-sized
    cardinality (bounded only by transport safety). Older CLOSED image answers
    without that field can be replayed: model candidate plus earlier model's
    alternatives become a *historical exploratory group*, NOT a new model run.
    Explicit model contradictions remain documented in reserve with their
    conditions; no global blacklist is created. The next T question is
    model-authored, not a host-derived decision.
    """
    def invalid(code):
        return {'status':'invalid','accepted':False,'accepted_id':None,
            'initial_body_count':0,'active_count':0,'reserve_count':0,
            'active':[],'reserve':[],'reason_codes':[code],
            'next_step':'T','t_distinguishing_question':None}
    if (not isinstance(model_output,dict) or
            not Draft202012Validator(visual_spatial_choice_schema()).is_valid(model_output)):
        return invalid('invalid_model_response')
    packet=options_packet or {}
    labels={}
    for label,cid in (packet.get('private_label_to_osm_id') or {}).items():
        if not (str(label).isdecimal() and isinstance(cid,str) and
                cid.startswith(('osm:way:','osm:relation:'))):
            return invalid('invalid_original_osm_label_mapping')
        labels[int(label)]=cid
    index_labels={row[0] for row in packet.get('all_received_physical_bodies') or []
                  if isinstance(row,(tuple,list)) and row and type(row[0]) is int}
    if not labels or set(labels)!=index_labels:
        return invalid('physical_osm_inventory_incomplete')
    if (packet.get('version')!='street_story.g_spatial_options.v3' or
            packet.get('map_sha256')!=actual_map_sha256 or
            source_sha256!=actual_source_sha256 or
            not isinstance(model_source_sha256,str) or
            len(model_source_sha256)!=64):
        return invalid('original_source_map_binding_failed')
    index_count=len(labels)
    decision=model_output['decision']
    requested=model_output.get('active_hypotheses')
    legacy=requested is None
    active_rows=[]
    if not legacy:
        for item in requested:
            label=item['label']
            if label not in labels:
                return invalid('active_label_not_in_original_map')
            if any(row['label']==label for row in active_rows):
                return invalid('duplicate_active_physical_label')
            active_rows.append({'label':label,
                'source_match':item['source_match'],
                'what_remains_uncertain':item['what_remains_uncertain']})
    elif decision not in {'unknown','no_reduction'}:
        model_label=model_output.get('candidate_label')
        if model_label in labels:
            active_rows.append({'label':model_label,
                'source_match':'earlier_closed_model_primary_nomination',
                'what_remains_uncertain':'legacy_single_candidate_not_a_new_shortlist'})
        for alt in model_output.get('contrasted_alternatives') or []:
            label=alt.get('label')
            if label in labels and not any(r['label']==label for r in active_rows):
                active_rows.append({'label':label,
                    'source_match':'earlier_closed_model_named_alternative',
                    'what_remains_uncertain':alt.get('source_vs_map_difference') or ''})

    contradictions={}
    for item in model_output.get('explicit_contradictions') or []:
        label=item['label']
        if label not in labels:
            return invalid('contradiction_not_in_original_osm')
        contradictions[label]={
            'source_vs_map_conflict':item['source_vs_map_conflict'],
            'conditions':item['conditions']}
    if any(row['label'] in contradictions for row in active_rows):
        return invalid('same_building_active_and_explicitly_contradicted')

    # Empty scene-sized selection means no reduction, never zero candidates.
    # This is a model outcome, not a geometric nearest-distance default.
    no_reduction=decision in {'unknown','no_reduction'} or not active_rows
    if no_reduction:active_rows=[]
    entries={e.get('candidate_id'):e for e in observed_entries or []
             if isinstance(e,dict) and isinstance(e.get('candidate_id'),str)}
    active_set={row['label'] for row in active_rows}
    def mapped(label):
        cid=labels[label]
        return {'label':label,'candidate_id':cid,**_physical_metadata(entries.get(cid))}
    active=[{**mapped(row['label']),
             'source_match':row['source_match'],
             'unresolved_difference':row['what_remains_uncertain']}
            for row in active_rows]
    reserve=[{**mapped(label),
             'review_state':('explicit_model_contradiction'
                   if label in contradictions else 'not_rejected_not_active'),
             'contradiction':contradictions.get(label)}
             for label in sorted(labels) if label not in active_set]

    checked=None
    if decision=='accept' and len(active)==1 and (
            model_output.get('candidate_label')==active[0]['label']):
        checked=check_spatial_choice(model_output,packet,
              source_sha256=source_sha256,
              model_source_sha256=model_source_sha256,
              actual_source_sha256=actual_source_sha256,
              actual_map_sha256=actual_map_sha256)
    accepted=bool(checked and checked.get('accepted') and
                  not checked.get('reason_codes'))
    # Deliberately never infer identity merely from a one-member shortlist.
    status=('accepted_identity_proposal' if accepted else
            'no_useful_reduction' if no_reduction else 'active_shortlist')
    if status=='active_shortlist' and len(active)>=index_count:
        status='no_useful_reduction'
    next_step=model_output.get('next_useful_step') or (
        'none' if accepted else 'T')
    if not accepted and next_step=='none':
        next_step='T'
    result={'status':status,'accepted':accepted,
        'accepted_id':checked.get('candidate_id') if accepted else None,
        'accepted_proof':checked.get('proof') if accepted else None,
        'initial_body_count':index_count,
        'active_count':len(active) if status!='no_useful_reduction' else 0,
        'reserve_count':len(reserve) if status!='no_useful_reduction' else index_count,
        'active':active if status!='no_useful_reduction' else [],
        'reserve':reserve if status!='no_useful_reduction' else [
            {**mapped(label),'review_state':('explicit_model_contradiction'
                if label in contradictions else 'not_rejected_not_active'),
             'contradiction':contradictions.get(label)}
            for label in sorted(labels)],
        'model_scene_pattern':model_output.get('source_pattern'),
        'model_uncertainties':model_output.get('uncertainties') or [],
        'next_step':next_step,
        't_distinguishing_question':model_output.get('t_distinguishing_question') or None,
        'detail_requested_labels':model_output.get('request_detail_labels') or [],
        'model_explicit_contradiction_count':len(contradictions),
        'model_answer_sha256':hashlib.sha256(json.dumps(
            model_output,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'reason_codes':(['no_active_shortlist_from_model'] if no_reduction else []),
        'source_and_map_bound':True,
        'active_group_chosen_by_model':not legacy,
        'legacy_closed_response_replay':legacy,
        'other_bodies_retained_for_reconsideration':True,
        'canonical_POI_memory_updated':False}
    # Large untouched reserve stays in the machine-readable receipt. T only
    # receives active + observed literals + the distinguishing question.
    result['downstream_T']={
        'scope':'source_osm_shortlist_unconfirmed' if not accepted else 'accepted_G_candidate_for_codex_review',
        'active_physical_candidates':active if status!='no_useful_reduction' else [],
        'reserved_count':result['reserve_count'],
        'expandable_on_new_evidence':True,
        'next_distinguishing_question':result['t_distinguishing_question'],
        'model_scene_pattern':result['model_scene_pattern'],
        'next_step':next_step,
        'acceptance_not_implied_by_single_active_body':True}
    return result
