"""Observed-URL selection, never source or physical-identity invention."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .opencode_research import SEARCH_SCHEMA, selected_search_sources


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
