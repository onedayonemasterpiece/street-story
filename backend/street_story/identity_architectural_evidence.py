"""LLM-first, provenance-grounded architectural publisher/OSM linkage.

No postal lexicon, number parser, Unicode suffix substitution, address-range
heuristic, building-name similarity scoring or automatic visual ranking.

The host gives the existing SOURCE/text model **literal received records**.
The model interprets same-address, historic-address, address-range,
multi-building complex, and physical facade scope. The host ONLY verifies
that every selected evidence pointer references the correct real received
article and actually observed physical OSM body/entrance.

A grounded link is not an identity on its own. The unchanged
freeze_architectural_text_proof still checks photo, SHA, article, candidate,
architectural citations, and unresolved alternatives; the LLM decides whether
the structure in SOURCE distinguishes the individual body.
"""
from __future__ import annotations

from .identity_candidate_policy import candidate_identity_eligible
from .identity_source_selection import observed_address_context
from .identity_subject_binding import article_candidate


def _physical(candidate):
    if not isinstance(candidate, dict):
        return False
    tags = (candidate.get('map_object') or {}).get('tags') or {}
    return (isinstance(candidate.get('candidate_id'), str)
        and candidate['candidate_id'].startswith('osm:')
        and candidate_identity_eligible(candidate) and not article_candidate(candidate)
        and not tags.get('entrance')
        and bool(tags.get('building') or tags.get('building:part')))


def literal_evidence_inventory(story, candidates, articles, *, candidate_ids=None):
    """Source inventory. The *model*, never Python, interprets the values.

    Only exact OSM IDs, physical way/relation vs entrance membership, source
    origins and raw strings are compiled here. All joined relationships are
    explicitly *unresolved*, not guessed from matching digits or spellings.
    """
    from .identity_architectural_context import _subject_addresses
    if not isinstance(articles, list) or not isinstance(candidates, list):
        raise ValueError('literal_architecture_inputs_required')
    observed = [*candidates, *(story.get('_identity_observed_candidates') or [])]
    subjects = {item['candidate_id']: item for item in observed if _physical(item)}
    ids = list(dict.fromkeys(candidate_ids if candidate_ids is not None else subjects))
    if any(cid not in subjects for cid in ids):
        raise ValueError('unobserved_physical_subject_in_semantic_link_inventory')
    context = observed_address_context(story, observed)

    osm_refs = {}
    physical_records = []
    for cid in ids:
        candidate = subjects[cid]
        tags = (candidate.get('map_object') or {}).get('tags') or {}
        # Anonymous bodies are still real received map evidence. A postal tag
        # is not a prerequisite for the model to reason about their scope.
        body_record = {'ref':f'o{len(osm_refs):04d}', 'candidate_id':cid, 'entry_id':cid,
            'kind':'observed_OSM_physical_body', 'literal_value':cid,
            'source_url':(candidate.get('map_object') or {}).get('source_url'),
            'provenance':'this_exact_observed_OSM_physical_subject'}
        osm_refs[body_record['ref']] = body_record
        references = [body_record]
        for address in _subject_addresses(context, candidate):
            item = {'ref':f'o{len(osm_refs):04d}',
                'candidate_id':cid,
                'entry_id':address['mapped_entry_id'],
                'kind':'observed_OSM_postal_entry',
                'literal_value':dict(address['address']),
                'provenance':('osm_own_building_postal_tags'
                    if address['mapped_entry_id'] == cid
                    else 'verified_osm_closed_way_entrance_membership')}
            osm_refs[item['ref']] = item
            references.append(item)
        for tag in ('old_addr:street', 'old_addr:housenumber',
                    'ref:prussia39', 'website:prussia39',
                    'wikidata', 'heritage', 'name'):
            if not isinstance(tags.get(tag), str) or not tags[tag].strip():
                continue
            item = {'ref':f'o{len(osm_refs):04d}',
                'candidate_id':cid, 'entry_id':cid,
                'kind':'observed_OSM_tag', 'tag':tag,
                'literal_value':tags[tag],
                'provenance':'this_exact_observed_OSM_physical_subject'}
            osm_refs[item['ref']] = item
            references.append(item)
        physical_records.append({
            'candidate_id':cid,
            'literal_observed_evidence':references,
            'map_geometry_received':bool(candidate.get('map_geometry')),
            'geometric_scope':'one observed OSM physical way/relation, not neighboring buildings',
            'identity_from_this_inventory':False})
    publisher_refs = {}
    publisher_records = []
    for article in articles:
        if (not isinstance(article, dict)
                or not isinstance(article.get('article_id'), str)
                or not isinstance(article.get('url'), str)
                or article.get('raw_body_sha256_verified') is not True
                or article.get('input_kind') != 'acquired_article_text'):
            raise ValueError('unverified_article_in_architecture_link_inventory')
        aid = article['article_id']
        sources = []
        literal_addresses = []
        if (article.get('address_provenance') == 'publisher_article_metadata_table'
                and isinstance(article.get('address'), str) and article['address'].strip()):
            literal_addresses.append(('observed_publisher_article_metadata',
                article['address']))
        for card in article.get('card_variants') or []:
            if (isinstance(card, dict) and card.get('article_id') == aid
                    and isinstance(card.get('address_text'), str)
                    and card['address_text'].strip()
                    and card.get('canonical_url') == article['url']):
                literal_addresses.append(('received_publisher_catalogue_card',
                    card['address_text']))
        # A genuine article URL/ID is also a grounding source for an explicit
        # OSM ref; no parsing street spelling is required to link the records.
        literal_addresses.insert(0, ('acquired_publisher_article_url', article['url']))
        for provenance, value in dict.fromkeys(literal_addresses):
            record = {'ref':f'p{len(publisher_refs):04d}',
                'article_id':aid, 'literal_value':value, 'provenance':provenance,
                'raw_article_sha256':article.get('source_sha256')}
            publisher_refs[record['ref']] = record
            sources.append(record)
        publisher_records.append({'article_id':aid,
            'actual_acquired_publisher_records':sources,
            'body_sha256':article.get('source_sha256'),
            'identity_from_this_inventory':False})
    return {'contract':'llm-first-literal-evidence-v1',
        'candidate_ids':ids,
        'articles':publisher_records,
        'physical_subjects':physical_records,
        'publisher_refs':publisher_refs, 'osm_refs':osm_refs,
        'publisher_address_interpretation':'LLM',
        'scope_interpretation':'LLM',
        'host_address_parser_used':False}


def physical_link_schema(article_ids, candidate_ids, publisher_refs, osm_refs):
    """Compact model response, admitted only through observed pointer tables."""
    return {'type':'array','maxItems':2,'items':{
        'type':'object', 'properties':{
            'article_id':{'type':'string','enum':list(article_ids)},
            'candidate_id':{'type':'string','enum':list(candidate_ids)},
            'publisher_ref':{'type':'string','enum':list(publisher_refs) or ['']},
            'osm_ref':{'type':'string','enum':list(osm_refs) or ['']},
            'relationship':{'type':'string','enum':[
                'same_individual_physical_body','historical_address_relation',
                'documented_complex_component','explicit_OSM_to_publisher_reference',
                'ambiguous_or_insufficient']},
            'subject_scope':{'type':'string','enum':[
                'specific_photographed_OSM_body',
                'historical_complex_only','unresolved']},
            'architectural_scope_explanation':{'type':'string','maxLength':500},
            'postal_interpretation':{'type':'string','maxLength':360}},
        'required':['article_id','candidate_id','publisher_ref','osm_ref',
            'relationship','subject_scope','architectural_scope_explanation',
            'postal_interpretation'],
        'additionalProperties':False}}


def validate_model_physical_links(inventory, model_links, decision):
    """Validate pointers/provenance and declared scope, not postal semantics.

    A model's *claim* that 6 is historically 6A is not automatically factual.
    It must disclose both literal records and the architectural/physical
    scope it believes they represent, which can be inspected, challenged and
    preserved in evidence. A complex/unresolved scope cannot become a body.
    """
    if not isinstance(inventory, dict) or inventory.get('contract') != 'llm-first-literal-evidence-v1':
        raise ValueError('missing_literal_source_evidence_inventory')
    if not isinstance(model_links, list) or not isinstance(decision, dict):
        raise ValueError('model_evidence_links_malformed')
    bound = {item['article_id'] for item in (decision.get('article_bindings') or [])}
    cid = decision.get('candidate_id')
    if len(model_links) != len(bound) or len({x.get('article_id') for x in model_links
            if isinstance(x, dict)}) != len(bound):
        return {'applicable':True,'supported':False,
            'reason':'each_positive_binding_requires_unique_model_source_link'}
    verified = []
    for link in model_links:
        if not isinstance(link, dict):
            return {'applicable':True,'supported':False,'reason':'invalid_model_link_row'}
        aid = link.get('article_id')
        if aid not in bound or link.get('candidate_id') != cid:
            return {'applicable':True,'supported':False,'reason':'foreign_model_article_or_physical_ID'}
        pub = inventory['publisher_refs'].get(link.get('publisher_ref'))
        osm = inventory['osm_refs'].get(link.get('osm_ref'))
        if not pub or pub['article_id'] != aid:
            return {'applicable':True,'supported':False,
                'reason':'publisher_ref_does_not_point_to_bound_acquired_article'}
        if not osm or osm['candidate_id'] != cid:
            return {'applicable':True,'supported':False,
                'reason':'osm_ref_does_not_belong_to_nominated_physical_body'}
        if (link.get('subject_scope') != 'specific_photographed_OSM_body'
                or link.get('relationship') in (None,'ambiguous_or_insufficient')
                or not str(link.get('architectural_scope_explanation') or '').strip()):
            return {'applicable':True,'supported':False,
                'reason':'llm_did_not_resolve_individual_physical_body'}
        verified.append({'article_id':aid,'candidate_id':cid,
            'publisher_ref':pub['ref'], 'osm_ref':osm['ref'],
            'source_provenance':pub['provenance'],
            'osm_provenance':osm['provenance'],
            'model_relationship':link['relationship'],
            'model_scope':link['subject_scope'],
            'model_architectural_scope_explanation':link['architectural_scope_explanation'],
            'literal_address_judgment_was_semantic_not_host_inferred':True})
    return {'applicable':True,'supported':True,
        'reason':'model_physical_scope_grounded_in_real_source_and_OSM_pointers',
        'verified_bindings':verified,
        'semantics_decided_by':'SOURCE+TEXT_llm',
        'host_address_parser_used':False}
