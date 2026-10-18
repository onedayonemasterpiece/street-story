"""Lossless bounded delivery of frozen discovery history, never a search gate."""
from __future__ import annotations

import hashlib
import json

SECTIONS = ('prior_search_leads', 'prior_search_queries')
BYTE_BUDGET = 24000


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def prompt_context(context):
    wire, memory = dict(context), {}
    for section in SECTIONS:
        items = context.get(section)
        if isinstance(items, list) and len(_encoded(items)) > BYTE_BUDGET:
            sha = hashlib.sha256(_encoded(items)).hexdigest()
            memory[section] = {'items': items, 'sha256': sha}
            wire[section] = {'delivery': 'frozen_paged_history', 'item_count': len(items),
                'sha256': sha, 'section': section, 'read_tool': 'read_research_memory',
                'meaning': 'All observations retained; use the read tool for this exact section. '
                    'Discovery is not evidence or a reason to reread every result.'}
    return wire, memory


def memory_page(memory, section, cursor=None):
    entry = memory.get(section)
    if entry is None:
        raise ValueError('research_memory_section_unavailable')
    if hashlib.sha256(_encoded(entry['items'])).hexdigest() != entry['sha256']:
        raise ValueError('research_memory_integrity_invalid')
    offset = 0
    if cursor is not None:
        try:
            sha, index = cursor.split(':')
            offset = int(index)
            if sha != entry['sha256'] or not 0 <= offset < len(entry['items']):
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('research_memory_cursor_invalid') from exc
    items, size = [], 0
    for item in entry['items'][offset:]:
        cost = len(_encoded(item))
        if items and size + cost > BYTE_BUDGET:
            break
        items.append(item)
        size += cost
    next_offset = offset + len(items)
    more = next_offset < len(entry['items'])
    return {'section': section, 'sha256': entry['sha256'], 'offset': offset,
        'items': items, 'total_items': len(entry['items']), 'has_more': more,
        'next_cursor': f'{entry["sha256"]}:{next_offset}' if more else None,
        'evidence_role': 'prior_discovery_context_not_evidence'}
