"""Observed-URL selection, never source or physical-identity invention."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .opencode_research import SEARCH_SCHEMA, selected_search_sources


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
