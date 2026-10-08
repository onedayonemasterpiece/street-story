"""Observed-URL selection, never source or physical-identity invention."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .opencode_research import SEARCH_SCHEMA, selected_search_sources


IDENTITY_SOURCE_POLICY = (
    'The target is the present-day physical building in SOURCE. A street/address/name is only a search hypothesis. '
    'Use supplied observed locality, complete address anchors and exact building/entrance memberships to compare '
    'plausible alternatives. Preserve city, street type and house-number suffix/range in every query. '
    'Prefer concrete records and modern exterior photographs of those address hypotheses. '
    'A building reported demolished/destroyed or visible only in archival photographs cannot provide a modern '
    'reference for the surviving SOURCE building; reject it as an identity lead unless the source explicitly '
    'documents the current physical replacement/restoration and shows its modern exterior. '
    'Reject same-named streets in another locality, general architecture/district summaries, and broad landmark '
    'lists when they do not show a plausible current physical subject. '
    'Do not promote an entrance, tenant, address label, article title or historical name to building identity. '
    'A housing/repair record with the exact address may be a useful lead; require accessible modern exterior '
    'evidence before comparing it. Empty selection is valid. These preferences are not identity proof. '
)


def observed_address_context(story, candidates=()):
    """Literal anchors and exact OSM memberships, never inferred house addresses."""
    research = json.loads((story or {}).get('research_json') or '{}')
    observed = (story or {}).get('_identity_observed_candidates') or (
        research.get('visual_identity') or {}).get('observed_candidates') or []
    entries = {item['candidate_id']: item for item in [*observed, *candidates]
        if isinstance(item, dict) and item.get('candidate_id')}
    for item in (research.get('osm') or {}).get('observed_pool') or []:
        if item.get('type') not in {'node', 'way', 'relation'} or item.get('id') is None:
            continue
        cid = f"osm:{item['type']}:{item['id']}"
        tags = item.get('tags') or {}
        literal_address = {key: tags[value] for key, value in
            [('city', 'addr:city'), ('street', 'addr:street'), ('house_number', 'addr:housenumber')]
            if tags.get(value)}
        if cid in entries:
            # City tags can be omitted by the compact map DTO. Restore only
            # literal tags of this same observed ID, never another entry's city.
            entries[cid] = {**entries[cid], 'map_address': {
                **literal_address, **(entries[cid].get('map_address') or {})}}
            continue
        entries[cid] = {'candidate_id': cid, 'distance_m': item.get('distance_m'),
            'map_address': literal_address, 'map_object': {'tags': tags}}
    anchors = []
    for cid, entry in entries.items():
        address = entry.get('map_address') or {}
        if not address.get('street') or not address.get('house_number'):
            continue
        tags = (entry.get('map_object') or {}).get('tags') or {}
        anchors.append({'mapped_entry_id': cid, 'address': dict(address),
            'map_coordinates': entry.get('map_coordinates'), 'distance_m': entry.get('distance_m'),
            'entry_kind': 'entrance' if tags.get('entrance') else 'building' if tags.get('building') else 'mapped_entry',
            'scope': 'mapped_entry_only'})
    def distance(anchor):
        try:
            return float(anchor.get('distance_m'))
        except (TypeError, ValueError):
            return float('inf')
    anchors.sort(key=lambda anchor: (distance(anchor), anchor['mapped_entry_id']))
    by_id = {anchor['mapped_entry_id']: anchor for anchor in anchors}
    buildings = []
    for cid, entry in entries.items():
        membership = (entry.get('map_object') or {}).get('building_entrances') or {}
        if membership.get('proof') != 'osm_closed_way_node_membership':
            continue
        linked = [by_id[key] for key in membership.get('candidate_ids') or [] if key in by_id]
        if linked:
            buildings.append({'physical_candidate_id': cid, 'address_entries': linked,
                'membership_proof': dict(membership), 'boundary_distance_m': entry.get('boundary_distance_m'),
                'footprint_bearing_interval': entry.get('footprint_bearing_interval'),
                'camera_alignment': entry.get('camera_alignment')})
    localities = list(dict.fromkeys([*regional_source_profile(story, candidates)['observed_localities'],
        *(str(anchor['address'][key]).strip() for anchor in anchors
            for key in ('city', 'town', 'village', 'state', 'country') if anchor['address'].get(key))]))
    return {'observed_localities': localities,
        'address_anchors': anchors, 'building_address_memberships': buildings,
        'policy': 'Distances order observations only. Membership joins these mapped entries to this observed '
            'building footprint; it does not identify SOURCE or imply one combined house-number range.'}


def regional_source_profile(story, candidates=()):
    """Source preferences from observed map locality, never photo answers."""
    context = (story or {}).get('_identity_search_context') or {}
    research = json.loads((story or {}).get('research_json') or '{}')
    addresses = [context.get('reverse_address') or {},
                 ((research.get('osm') or {}).get('reverse') or {}).get('address') or {}]
    addresses.extend(item.get('map_address') or {} for item in
                     [*(context.get('nearby') or []), *candidates])
    localities = list(dict.fromkeys(str(address[key]).strip() for address in addresses
        if isinstance(address, dict) for key in ('city', 'town', 'village', 'state', 'region', 'country')
        if address.get(key)))
    profile = {'observed_localities': localities,
        'preferences': ['official physical-object or operator pages with address evidence',
                        'municipal heritage registers', 'housing records and documented repairs'],
        'policy': 'Preferences are discovery hints, not truth or an allowlist; preserve subject and date distinctions.'}
    # This is a regional catalog, not a building/name/address lookup. Match only
    # locality fields already observed in map records; unknown geography stays unknown.
    if any('калининград' in value.casefold() or 'kaliningrad' in value.casefold() for value in localities):
        profile['regional_sources'] = [
            {'domain': 'prussia39.ru', 'use': 'local history, aliases and modern reference photographs'},
            {'domain': 'fkr39.ru', 'use': 'building repair records and dated reports'},
            {'domain': 'visit-kaliningrad.ru', 'use': 'object pages and photographs'},
            {'domain': 'gov39.ru', 'use': 'heritage registers and official documents'},
            {'domain': 'klgd.ru', 'use': 'municipal records'},
            {'domain': 'kgd.ru', 'use': 'dated local reporting'},
            {'domain': 'newkaliningrad.ru', 'use': 'dated local reporting'}]
    return profile


def indexed_selection_schema(count):
    return {'type': 'object', 'properties': {
        'summary': {'type': 'string', 'maxLength': 2000},
        'selected_sources': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'source_index': {'type': 'integer', 'enum': list(range(count))},
            'reason': {'type': 'string', 'maxLength': 400}},
            'required': ['source_index', 'reason'], 'additionalProperties': False}}},
        'required': ['summary', 'selected_sources'], 'additionalProperties': False}


def indexed_model_selection(observed, payload):
    # Only the model chooses pages. Code dereferences its inventory indices,
    # preserving exact observed URL bytes rather than asking it to copy URLs.
    if not Draft202012Validator(indexed_selection_schema(len(observed))).is_valid(payload):
        return [], {'status': 'selection_unavailable', 'code': 'malformed_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0}
    return model_selection(observed, {'summary': payload['summary'], 'selected_sources': [
        {'url': observed[item['source_index']]['url'], 'reason': item['reason']}
        for item in payload['selected_sources']]})


def model_selection(observed, payload):
    if (not Draft202012Validator(SEARCH_SCHEMA).is_valid(payload)
            or any(not item['reason'].strip() for item in payload['selected_sources'])):
        return [], {'status': 'selection_unavailable', 'code': 'malformed_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0}
    chosen, rejected = selected_search_sources(observed, payload)
    if rejected and not chosen:
        return [], {'status': 'selection_unavailable', 'code': 'unobserved_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0, 'unobserved_count': rejected}
    return chosen, {'status': 'model_selected', 'discovered_count': len(observed),
                    'selected_count': len(chosen), 'unobserved_count': rejected}


def response_selection(observed, text):
    text = str(text or '').strip()
    if text.startswith('```json') and text.endswith('```'):
        text = text[7:-3].strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    return model_selection(observed, payload)
