"""One model-nominated regional lookup; acquired text is evidence, never identity."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math

import httpx

from .identity_telemetry import record_identity_event


def _regional_area(story):
    lat, lon = story.get('latitude'), story.get('longitude')
    return (not isinstance(lat, bool) and not isinstance(lon, bool)
        and isinstance(lat, (int, float)) and isinstance(lon, (int, float))
        and math.isfinite(lat+lon) and 54 <= lat <= 56 and 19 <= lon <= 23)


def regional_preparation_query(story, candidates):
    """One literal retrieval anchor, never a target or first mixed-pool street."""
    if not _regional_area(story):
        return None
    from .identity_source_selection import observed_address_context
    reverse = (story.get('_identity_search_context') or {}).get('reverse_address') or {}
    city = next((str(reverse[key]).strip() for key in ('city', 'town', 'village') if reverse.get(key)), '')
    streets = {str(reverse[key]).strip() for key in ('road', 'pedestrian', 'residential', 'street') if reverse.get(key)}
    if city and len(streets) == 1:
        from .identity_scene import scene_camera_context
        return {'city':city, 'street':next(iter(streets)),
            'provenance':'already_received_reverse_address',
            'anchor_kind':scene_camera_context(story)['position_status'],
            'target_identity_established':False}
    if streets:
        return None
    context = observed_address_context(story, candidates)
    addresses = [item['address'] for item in context['address_anchors']]
    addresses += [item.get('map_address') or {} for item in
        [*(story.get('_identity_observed_candidates') or []), *candidates] if isinstance(item, dict)]
    queries = {(str(address.get('city') or city).strip(), str(address.get('street') or '').strip())
        for address in addresses if address.get('street')}
    if len(queries) != 1:
        return None
    locality, street = next(iter(queries))
    return ({'city':locality, 'street':street, 'provenance':'unambiguous_observed_street',
        'anchor_kind':'retrieval_context', 'target_identity_established':False} if locality and street else None)


async def prepare_regional_catalogue(service, story, candidates, *, allow_network=True):
    """At most three seconds of optional inventory preparation, not a barrier.

    One observed continuation can expose omitted cards inside the same small
    preparation envelope. It never cascades through a street's page tree.
    """
    from .prussia39 import Prussia39Adapter, cached_get_available
    from .research_budget import ResearchTerminated
    query = regional_preparation_query(story, candidates)
    if query is None or not hasattr(getattr(service, 'store', None), 'cache_get'):
        return {}
    try:
        url = Prussia39Adapter.address_url(query['city'], query['street'])
    except (ValueError, UnicodeError):
        return {}
    research = json.loads(story.get('research_json') or '{}')
    generation = int(story.get('_identity_generation', research.get('identity_generation') or 0))
    query_key = hashlib.sha256(url.encode()).hexdigest()
    revision = int(story.get('_identity_research_control_revision',
        ((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0))
    scope = {'photo_sha256':story.get('photo_sha256'), 'generation':generation,
        'control_revision':revision, 'query_key':query_key}
    saved = story.get('_identity_regional_catalogue') or ((research.get('identity_article_discovery') or {})
        .get('search_plan') or {}).get('payload', {}).get('regional_catalogue') or {}
    if saved.get('scope') == scope:
        return saved
    receipt = {'policy':'prussia-catalogue-v1', 'scope':scope, 'query_scope':query,
        'status':'not_sent', 'results':[], 'pages':[], 'total_count':None, 'inventory_complete':False,
        'requested_url':url, 'method':'GET', 'query_key':query_key}
    if not allow_network and not cached_get_available(service.store, url):
        return dict(receipt, error_code='joint_image_planner_unavailable')
    wait = 3.0
    started = False
    adapter = None
    try:
        if hasattr(service, 'settings'):
            from .research_budget import require_remaining, reserve_work
            wait = min(wait, require_remaining(service, story['id'], 'identity'))
            if not cached_get_available(service.store, url):
                reserve_work(service, story['id'], 'pages', [url])
        record_identity_event(service, story['id'], 'identity_regional_preparation_started', {
            'query_key':query_key, 'anchor_kind':query['anchor_kind'], 'wait_limit_seconds':wait})
        started = True
        async with asyncio.timeout(wait), httpx.AsyncClient(timeout=wait, follow_redirects=False) as client:
            adapter = Prussia39Adapter(service.store, client)
            lookup = await adapter.address_search(query['city'], query['street'])
            receipt.update(lookup)
            receipt['pages'] = [lookup]
            # Retain every publisher row, including distinct address variants
            # for one SID. Pagination does not select or read article bodies.
            continuation = lookup.get('pagination_urls') or []
            if (lookup.get('status') == 'completed' and lookup.get('inventory_complete') is not True
                    and len(continuation) == 1 and isinstance(lookup.get('total_count'), int)
                    and lookup['total_count'] > len(lookup.get('results') or [])
                    and (allow_network or cached_get_available(service.store, continuation[0]))):
                if hasattr(service, 'settings'):
                    require_remaining(service, story['id'], 'identity')
                    if not cached_get_available(service.store, continuation[0]):
                        reserve_work(service, story['id'], 'pages', continuation)
                next_page = await adapter.search_page(continuation[0], previous_receipt=lookup)
                receipt['pages'].append(next_page)
                if next_page.get('status') == 'completed':
                    receipt['results'] = [*(lookup.get('results') or []), *(next_page.get('results') or [])]
                    receipt['received_row_count'] = len(receipt['results'])
                    receipt['unique_article_count'] = len({row['article_id'] for row in receipt['results']})
                    receipt['inventory_complete'] = (next_page.get('total_count') == lookup['total_count']
                        and len(receipt['results']) == lookup['total_count'])
                else:
                    receipt['continuation_outcome'] = next_page.get('status')
            if receipt.get('inventory_complete') is not True and receipt.get('results'):
                receipt['limitation'] = 'Only received cards may be selected; inventory is partial, not source exhaustion.'
    except TimeoutError:
        if adapter is not None and getattr(adapter, 'last_receipt', None):
            receipt['interrupted_http_receipt'] = {**adapter.last_receipt, 'outcome':'cancelled_drained'}
        receipt.update(status='completed' if receipt.get('results') else 'transport_failed',
            error_code='regional_preparation_wait_expired', inventory_complete=False,
            limitation='Preparation wait ended; no extra selector/judge is added for late inventory.')
    except ResearchTerminated as exc:
        receipt.update(status='completed' if receipt.get('results') else 'not_sent',
            error_code=exc.reason, inventory_complete=False)
    receipt['preparation_started'] = started
    story['_identity_regional_catalogue'] = receipt
    record_identity_event(service, story['id'], 'identity_regional_preparation_completed', {
        'query_key':query_key, 'status':receipt['status'], 'received_rows':len(receipt.get('results') or []),
        'total_count':receipt.get('total_count'), 'inventory_complete':receipt['inventory_complete'],
        'error_code':receipt.get('error_code')})
    return receipt


def catalogue_model_context(catalogue):
    if not catalogue:
        return {}
    # Metadata only. An annotation is not a fetched/verified article body.
    context = {key:catalogue.get(key) for key in ('status','query_scope','total_count','received_row_count',
        'unique_article_count','inventory_complete','pagination_urls','limitation','error_code')}
    context['results'] = [{key:row[key] for key in ('article_id','canonical_url','coordinates') if key in row}
        | {key:str(row.get(key) or '')[:limit] for key,limit in
            (('title',120),('address_text',240),('annotation',160))}
        | {'metadata_excerpt':True} for row in catalogue.get('results') or []]
    return context


def regional_selection_schema(candidate_ids, catalogue):
    ids = list(dict.fromkeys(row['article_id'] for row in catalogue.get('results') or []))
    return {'type':'array', 'maxItems':2, 'items':{'type':'object', 'properties':{
        'article_id':{'type':'string','enum':ids}, 'candidate_id':{'type':'string','enum':list(candidate_ids)},
        'scope':{'type':'string','maxLength':400}, 'binding_basis':{'type':'string','maxLength':400},
        'physical_binding_resolved':{'type':'boolean'}},
        'required':['article_id','candidate_id','scope','binding_basis','physical_binding_resolved'],
        'additionalProperties':False}}


async def acquire_selected_regional_text(service, story, candidates, selections, catalogue):
    """Read only model-selected received canonical cards, without ranking."""
    from jsonschema import Draft202012Validator
    from .identity_candidate_policy import candidate_identity_eligible
    from .identity_subject_binding import article_candidate
    from .prussia39 import Prussia39Adapter
    catalog = {item.get('candidate_id'):item for item in
        [*(story.get('_identity_observed_candidates') or []), *candidates] if isinstance(item, dict)}
    receipt = {'kind':'selected_prussia39', 'status':'not_sent', 'catalogue':catalogue, 'selected_cards':[]}
    if not selections:
        return [], receipt
    if not Draft202012Validator(regional_selection_schema(catalog, catalogue)).is_valid(selections):
        return [], dict(receipt, reason='selected_card_not_received_or_malformed')
    cards = {}
    for card in catalogue.get('results') or []:
        cards.setdefault(card['article_id'], []).append(card)
    seen, choices = set(), []
    for selection in selections:
        candidate = catalog.get(selection['candidate_id'])
        aid = selection['article_id']
        if (aid in seen or not candidate or not str(candidate.get('candidate_id') or '').startswith('osm:')
                or not candidate_identity_eligible(candidate) or article_candidate(candidate)
                or (candidate.get('map_object') or {}).get('tags', {}).get('entrance')
                or selection['physical_binding_resolved'] is not True
                or not selection['scope'].strip() or not selection['binding_basis'].strip()):
            return [], dict(receipt, reason='closed_physical_nomination_required')
        seen.add(aid)
        choices.append((cards[aid][0]['canonical_url'], selection, cards[aid]))
    timeout = 20.0
    if hasattr(service, 'settings'):
        from .research_budget import require_remaining, reserve_work
        timeout = min(timeout, require_remaining(service, story['id'], 'identity'))
        reserve_work(service, story['id'], 'pages', [url for url, _, _ in choices])
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        adapter = Prussia39Adapter(service.store, client)
        pages = await asyncio.gather(*(adapter.article(url) for url, _, _ in choices))
    articles = []
    for page, (_url, selection, variants) in zip(pages, choices):
        text = page.get('normalized_text') or page.get('text') or ''
        receipt['selected_cards'].append({'article_id':selection['article_id'], 'status':page.get('status')})
        if page.get('status') != 'completed' or not text.strip() or page.get('raw_body_sha256_verified') is not True:
            continue
        text = text[:12_000]
        articles.append({'article_id':page['article_id'], 'url':page['canonical_url'],
            'source_sha256':page['raw_content_sha256'], 'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'text':text, 'raw_body_sha256_verified':True, 'input_kind':'acquired_article_text',
            'title':page.get('title') or variants[0].get('title') or '', 'scope':selection['scope'],
            'binding_basis':selection['binding_basis'], 'lookup_candidate_ids':[selection['candidate_id']],
            'card_variants':variants, 'fetched_at':page.get('fetched_at'), 'cache_hit':page.get('cache_hit',False)})
    receipt['status'] = 'completed' if len(articles) == len(choices) else 'partial' if articles else 'unavailable'
    return articles, receipt


async def acquire_selected_wikipedia_text(service, story, candidates, payload, wiki_pages):
    """Read only closed, explicit page/physical nominations, never rank a page.

    A metadata extract is not article text. The ordinary article reader handles
    acquisition and extraction; this adapter separately verifies its actual
    cached raw bytes before exporting a proof-ready SOURCE/text receipt.
    """
    from .article_media import public_url
    from .identity_candidate_policy import candidate_identity_eligible
    from .identity_subject_binding import article_candidate
    selected = payload.get('selected_wikipedia_page_ids') if isinstance(payload, dict) else None
    if not selected:
        return [], {}
    receipt = {'kind': 'selected_wikipedia', 'status': 'not_sent', 'articles': []}
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 2
            or any(not isinstance(pid, str) for pid in selected) or len(set(selected)) != len(selected)):
        return [], dict(receipt, reason='one_or_two_explicit_pages_required')
    pages = {str(page['pageid']): page for page in wiki_pages
        if isinstance(page, dict) and page.get('pageid')}
    catalog = {item.get('candidate_id'): item for item in
        [*(story.get('_identity_observed_candidates') or []), *candidates] if isinstance(item, dict)}
    bindings = payload.get('subject_article_bindings') or []
    if not isinstance(bindings, list):
        return [], dict(receipt, reason='physical_binding_missing')
    choices = []
    for pid in selected:
        page = pages.get(pid)
        url = public_url(str((page or {}).get('url') or ''))
        if not page or not url:
            return [], dict(receipt, reason='selected_page_not_received_with_public_url')
        matches = [binding for binding in bindings if isinstance(binding, dict)
            and binding.get('article_id') == 'wiki:' + pid]
        if len(matches) != 1:
            return [], dict(receipt, reason='physical_binding_missing_or_ambiguous')
        binding = matches[0]
        candidate = catalog.get(binding.get('candidate_id'))
        if (not candidate or not str(candidate.get('candidate_id') or '').startswith('osm:')
                or not candidate_identity_eligible(candidate) or article_candidate(candidate)
                or (candidate.get('map_object') or {}).get('tags', {}).get('entrance')
                or binding.get('physical_binding_resolved') is not True
                or not isinstance(binding.get('scope'), str) or not binding['scope'].strip()
                or not isinstance(binding.get('binding_basis'), str) or not binding['binding_basis'].strip()):
            return [], dict(receipt, reason='closed_physical_nomination_required')
        choices.append((pid, url, binding))
    reader = getattr(getattr(getattr(service, 'providers', None), 'gemini', None), '_fetch_page_documents', None)
    if not callable(reader):
        return [], dict(receipt, reason='article_reader_unavailable')
    urls = list(dict.fromkeys(url for _, url, _ in choices))
    timeout = 20.0
    if hasattr(service, 'settings'):
        from .research_budget import require_remaining, reserve_work
        timeout = min(timeout, require_remaining(service, story['id'], 'identity'))
        reserve_work(service, story['id'], 'pages', urls)
    context = {'research_sources': [{'url': url, 'title': pages[pid].get('title') or ''}
        for pid, url, _ in choices]}
    try:
        async with asyncio.timeout(timeout):
            documents = await reader(urls, context)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        return [], dict(receipt, status='transport_failed', reason=type(exc).__name__)
    articles, outcomes = [], []
    for pid, url, binding in choices:
        document = documents.get(url) or {}
        text, final = document.get('normalized_text'), public_url(str(document.get('final_url') or ''))
        status = document.get('read_status')
        outcome = {'article_id': 'wiki:' + pid, 'requested_url': url, 'read_status': status}
        outcomes.append(outcome)
        if (status != 'complete' or not isinstance(text, str) or not text.strip() or not final):
            outcome['status'] = 'unavailable' if not document else 'partial_or_invalid'
            continue
        # Frozen SQL text versions need not contain a raw-body hash. Verify
        # the actual ordinary acquisition cache, without a replacement read.
        verified = None
        for cached_url in dict.fromkeys((final, url)):
            key = 'public-article-acquisition-v1:' + hashlib.sha256(cached_url.encode()).hexdigest()
            entry = service.store.cache_get(key) or {}
            try:
                raw = base64.b64decode(entry.get('body', ''), validate=True)
            except (ValueError, TypeError):
                continue
            raw_hash = hashlib.sha256(raw).hexdigest()
            if (raw and entry.get('final_url') == final and entry.get('sha256') == raw_hash
                    and (not document.get('raw_content_sha256') or document['raw_content_sha256'] == raw_hash)):
                verified = (raw_hash, entry)
                break
        if verified is None:
            outcome['status'] = 'raw_cache_integrity_failed'
            continue
        raw_hash, entry = verified
        # Verify that the cached body actually produced this text, including
        # the SQL-reuse case where its original raw hash was not persisted.
        from bs4 import UnicodeDammit
        from .providers import _read_article_text
        decoded = UnicodeDammit(raw, is_html=True)
        if decoded.unicode_markup is None:
            outcome['status'] = 'raw_cache_encoding_failed'
            continue
        actual_text, limited = _read_article_text(decoded.unicode_markup)
        if limited or actual_text != text:
            outcome['status'] = 'raw_cache_text_mismatch'
            continue
        text = text[:12_000]
        article = {'article_id': 'wiki:' + pid, 'url': final, 'text': text,
            'source_sha256': raw_hash, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'raw_body_sha256_verified': True, 'input_kind': 'acquired_article_text',
            'title': pages[pid].get('title') or '', 'scope': binding['scope'],
            'lookup_candidate_ids': [binding['candidate_id']], 'fetched_at': entry.get('acquired_at'),
            'source_encoding': decoded.original_encoding, 'read_status': status}
        if document.get('source_version_id'):
            article['source_version_id'] = document['source_version_id']
        articles.append(article)
        outcome['status'] = 'completed'
    receipt.update(status='completed' if len(articles) == len(choices) else 'partial' if articles else 'unavailable',
                   selected_page_ids=selected, articles=outcomes)
    record_identity_event(service, story['id'], 'identity_selected_wikipedia_text_completed', {
        'selected_page_ids': selected, 'status': receipt['status'], 'article_count': len(articles)})
    return articles, receipt


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
            if lookup.get('status') == 'completed' and results:
                lookup['limitation'] = ('Broad inventory arrived after the initial joint call. '
                    'No automatic first-two selection or third paid selector/judge; metadata remains retained.')
            return [], lookup
        if hasattr(service, 'settings'):
            from .research_budget import reserve_work
            reserve_work(service, story['id'], 'pages', [item['canonical_url'] for item in results])
        urls = list(dict.fromkeys(item['canonical_url'] for item in results))
        pages = await asyncio.gather(*(adapter.article(url) for url in urls))
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
