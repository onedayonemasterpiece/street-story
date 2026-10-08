"""One model-nominated regional lookup; acquired text is evidence, never identity."""
from __future__ import annotations

import asyncio
import hashlib

import httpx

from .identity_telemetry import record_identity_event


def lookup_schema(candidate_ids):
    return {'type': 'object', 'properties': {
        'route': {'type': 'string', 'enum': ['none', 'address', 'coordinate']},
        'candidate_ids': {'type': 'array', 'maxItems': 2, 'uniqueItems': True,
            'items': {'type': 'string', 'enum': list(candidate_ids)}},
        'address_entry_id': {'type': 'string', 'maxLength': 100},
        'reason': {'type': 'string', 'maxLength': 400}},
        'required': ['route', 'candidate_ids', 'reason'], 'additionalProperties': False}


async def acquire_regional_text(service, story, candidates, request):
    """Read at most two cards from one narrow literal query; no target scoring.

    A publisher's ambiguous inventory is retained, rather than truncated to two
    convenient results. A different street or title is never substituted.
    """
    from jsonschema import Draft202012Validator
    from .identity_source_selection import observed_address_context
    from .prussia39 import Prussia39Adapter
    catalog = {item.get('candidate_id'): item for item in
        [*(story.get('_identity_observed_candidates') or []), *candidates]}
    if not request or not Draft202012Validator(lookup_schema(list(catalog))).is_valid(request):
        return [], {}
    ids = request['candidate_ids']
    if request['route'] == 'none' or not ids or not request['reason'].strip():
        return [], {}
    context = observed_address_context(story, candidates)
    reverse = (story.get('_identity_search_context') or {}).get('reverse_address') or {}
    locality = next((reverse[key] for key in ('city', 'town', 'village') if reverse.get(key)), '')
    anchors = {item['mapped_entry_id']: item['address'] for item in context['address_anchors']}
    addresses = [anchors.get(cid) or catalog[cid].get('map_address') or {} for cid in ids]
    entry_id = request.get('address_entry_id')
    if entry_id:
        memberships = {item['physical_candidate_id']: {entry['mapped_entry_id'] for entry in item['address_entries']}
            for item in context['building_address_memberships']}
        if entry_id not in anchors or any(entry_id != cid and entry_id not in memberships.get(cid, set()) for cid in ids):
            return [], {'status': 'not_sent', 'reason': 'address_entry_not_bound_to_nominated_footprint'}
        addresses = [anchors[entry_id]]
    lat, lon = story.get('latitude'), story.get('longitude')
    # This is the documented catalog's region, not a default location or answer.
    if lat is None or lon is None or not (54 <= float(lat) <= 56 and 19 <= float(lon) <= 23):
        return [], {'status': 'not_applicable', 'reason': 'regional_catalog_outside_observed_area'}
    timeout = 25.0
    if hasattr(service, 'settings'):
        from .research_budget import require_remaining
        timeout = min(timeout, require_remaining(service, story['id'], 'identity'))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        adapter = Prussia39Adapter(service.store, client)
        if request['route'] == 'address':
            queries = {(str(address.get('city') or locality).strip(), str(address.get('street') or '').strip(),
                str(address.get('house_number') or '').strip()) for address in addresses}
            if len(queries) != 1:
                return [], {'status': 'ambiguous_query', 'reason': 'one_lookup_requires_one_literal_address'}
            city, street, number = next(iter(queries))
            if not all((city, street, number)):
                return [], {'status': 'not_sent', 'reason': 'literal_address_incomplete'}
            lookup = await adapter.address_search(city, f'{street}, {number}')
        else:
            points = [catalog[cid].get('map_coordinates') or {} for cid in ids]
            if len(points) != 1 or not all(key in points[0] for key in ('latitude', 'longitude')):
                return [], {'status': 'not_sent', 'reason': 'one_lookup_requires_one_observed_point'}
            lookup = await adapter.coordinate_search(points[0]['latitude'], points[0]['longitude'])
        results = lookup.get('results') or []
        record_identity_event(service, story['id'], 'identity_regional_lookup_completed', {
            'route': request['route'], 'status': lookup.get('status'), 'result_count': len(results),
            'candidate_ids': ids, 'cache_hit': lookup.get('cache_hit', False)})
        if (lookup.get('status') != 'completed' or lookup.get('inventory_complete') is not True
                or not 1 <= len(results) <= 2):
            return [], lookup
        if hasattr(service, 'settings'):
            from .research_budget import reserve_work
            reserve_work(service, story['id'], 'pages', [item['canonical_url'] for item in results])
        pages = await asyncio.gather(*(adapter.article(item['canonical_url']) for item in results))
    articles = []
    for page in pages:
        text = page.get('normalized_text') or page.get('text') or ''
        if (page.get('status') != 'completed' or not text.strip()
                or page.get('raw_body_sha256_verified') is not True):
            continue
        # The exact transmitted excerpt and complete frozen source version are
        # distinct. The full publisher body remains in the existing reader cache.
        text = text[:12_000]
        articles.append({'article_id': page['article_id'], 'url': page['canonical_url'],
            'source_sha256': page['raw_content_sha256'],
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'raw_body_sha256_verified': True, 'input_kind': 'acquired_article_text',
            'title': page.get('title') or '', 'address': page.get('address_text') or '',
            'coordinates': page.get('coordinates'), 'fetched_at': page.get('fetched_at'),
            'cache_hit': page.get('cache_hit', False), 'lookup_candidate_ids': ids})
    return articles, lookup
