"""One model-nominated regional lookup; acquired text is evidence, never identity."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re

import httpx

from .identity_telemetry import record_identity_event


def _regional_area(story, candidates=()):
    """Publisher applicability from *observed* region data, not camera-only GPS.

    An original may have no EXIF GPS while OSM has an observed footprint,
    literal address or a mapped entrance with Kaliningrad locality. None of
    these inputs proves that this building appears in SOURCE.
    """
    def regional_point(point):
        if not isinstance(point, dict):
            return False
        lat, lon = point.get('latitude'), point.get('longitude')
        try:
            return (not isinstance(lat, bool) and not isinstance(lon, bool)
                and math.isfinite(float(lat)) and math.isfinite(float(lon))
                and 54 <= float(lat) <= 56 and 19 <= float(lon) <= 23)
        except (ValueError, TypeError, OverflowError):
            return False

    if regional_point(story):
        return True
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if regional_point(candidate.get('map_coordinates')):
            return True
        address = candidate.get('map_address') or {}
        if not isinstance(address, dict):
            continue
        # The regional publisher is only tried without GPS when its own
        # region is explicitly evidenced by OSM address metadata.
        if any('калининград' in str(address.get(key) or '').casefold()
                or 'kaliningrad' in str(address.get(key) or '').casefold()
                for key in ('city', 'state', 'region')):
            return True
    return False


def _physical_subject(candidate):
    from .identity_candidate_policy import candidate_identity_eligible
    tags = (candidate.get('map_object') or {}).get('tags') or {}
    return (str(candidate.get('candidate_id') or '').startswith('osm:')
        and candidate_identity_eligible(candidate) and not tags.get('entrance')
        and any(tags.get(key) for key in ('building', 'building:part')))


def _subject_addresses(context, candidate):
    """All *literal* footprint addresses and verified closed-way entrances.

    A directly addressed building may still have other numbered doors. The
    old first-direct-return hid those doors, creating camera-street bias and
    missing publisher search leads. We never infer a compound house number
    from two distinct entrance numbers.
    """
    cid = candidate['candidate_id']
    anchors = [anchor for anchor in context['address_anchors']
        if anchor['mapped_entry_id'] == cid]
    for membership in context['building_address_memberships']:
        if membership['physical_candidate_id'] == cid:
            anchors.extend(membership['address_entries'])
    return list({entry['mapped_entry_id']:entry for entry in anchors}.values())


def _reverse_address(story):
    research = json.loads(story.get('research_json') or '{}')
    return ((story.get('_identity_search_context') or {}).get('reverse_address') or
        ((research.get('osm') or {}).get('reverse') or {}).get('address') or {})


def regional_preparation_query(story, candidates):
    """An unambiguous supplied physical scope, never the camera's street."""
    from .identity_source_selection import observed_address_context
    subjects = {item['candidate_id']: item for item in candidates
        if isinstance(item, dict) and _physical_subject(item)}
    if not subjects or not _regional_area(story, list(subjects.values())):
        return None
    context = observed_address_context(story, candidates)
    reverse = _reverse_address(story)
    city = next((str(reverse[key]).strip() for key in ('city', 'town', 'village') if reverse.get(key)), '')
    subject_entries = [_subject_addresses(context, candidate) for candidate in subjects.values()]
    if any(not entries for entries in subject_entries):
        return None
    entries = [anchor for body_entries in subject_entries for anchor in body_entries]
    queries = {(str(anchor['address'].get('city') or city).strip(),
        str(anchor['address'].get('street') or '').strip()) for anchor in entries}
    if len(queries) != 1:
        return None
    locality, street = next(iter(queries))
    return ({'city':locality, 'street':street, 'provenance':'physical_subject_addresses',
        'anchor_kind':'subject_retrieval_context', 'target_identity_established':False,
        'candidate_ids':list(subjects), 'address_entry_ids':list(dict.fromkeys(
            anchor['mapped_entry_id'] for anchor in entries)),
        'membership_policy':'Own literal footprint address or verified closed-way entrance membership only.',
        **({'camera_reverse_road_hint':str(reverse['road'])} if reverse.get('road') else {})}
        if locality and street else None)


async def prepare_regional_catalogue(service, story, candidates, *, allow_network=True):
    """Prepare inventory within the publisher's ordinary bounded read envelope.

    One observed continuation can expose omitted cards inside the same small
    preparation envelope. It never cascades through a street's page tree.
    """
    from .prussia39 import Prussia39Adapter, READ_TIMEOUT_SECONDS, cached_get_available
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
        'control_revision':revision, 'query_key':query_key,
        'subject_scope_sha256':hashlib.sha256(json.dumps(query, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    saved = story.get('_identity_regional_catalogue') or ((research.get('identity_article_discovery') or {})
        .get('search_plan') or {}).get('payload', {}).get('regional_catalogue') or {}
    if saved.get('scope') == scope:
        return saved
    receipt = {'policy':'prussia-catalogue-v1', 'scope':scope, 'query_scope':query,
        'status':'not_sent', 'results':[], 'pages':[], 'total_count':None, 'inventory_complete':False,
        'requested_url':url, 'method':'GET', 'query_key':query_key}
    if not allow_network and not cached_get_available(service.store, url):
        return dict(receipt, error_code='joint_image_planner_unavailable')
    # This retrieval overlaps SOURCE/map preparation and precedes model admission.
    # Cancelling it earlier than the actual reader's envelope repeatedly deprived
    # the first joint call of cold cards, leaving no call for selected text.
    wait = READ_TIMEOUT_SECONDS
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
    if receipt.get('results'):
        receipt['physical_address_links'] = catalogue_physical_address_links(
            story, candidates, receipt['results'])
        receipt['observed_OSM_literal_addresses'] = _catalogue_observed_osm_addresses(
            story,candidates)
        receipt['physical_prefetch_plan'] = bounded_physically_linked_article_ids(receipt)
    story['_identity_regional_catalogue'] = receipt
    record_identity_event(service, story['id'], 'identity_regional_preparation_completed', {
        'query_key':query_key, 'status':receipt['status'], 'received_rows':len(receipt.get('results') or []),
        'total_count':receipt.get('total_count'), 'inventory_complete':receipt['inventory_complete'],
        'error_code':receipt.get('error_code')})
    return receipt



def catalogue_physical_address_links(story, candidates, received_cards):
    """Publisher cards kept LITERAL, not joined to OSM by a regex.

    Legacy function name retained for compatibility with catalogue receipts.
    It no longer guesses identity, matches houses, interprets ranges,
    transliterates street labels, or ranks the received descriptions.
    """
    del story, candidates
    if not isinstance(received_cards, list):
        return {}
    groups = {}
    for card in received_cards:
        if (not isinstance(card, dict)
                or not isinstance(card.get('article_id'), str)
                or not isinstance(card.get('canonical_url'), str)):
            continue
        group=groups.setdefault(card['article_id'],{
            'publisher_modern_address_metadata':[],
            'publisher_observed_titles':[],
            'source_url':card['canonical_url'],
            'address_semantics_not_interpreted':True,
            'identity_inferred':False})
        if group['source_url']!=card['canonical_url']:
            continue
        for key,target in [('address_text','publisher_modern_address_metadata'),
                           ('title','publisher_observed_titles')]:
            value=card.get(key)
            if isinstance(value,str) and value.strip() and value.strip() not in group[target]:
                group[target].append(value.strip())
    return groups


def _catalogue_observed_osm_addresses(story,candidates):
    """Independent observed OSM address inventory; no postal matching."""
    from .identity_source_selection import observed_address_context
    observed=[*candidates,*(story.get('_identity_observed_candidates') or [])]
    bodies={c['candidate_id']:c for c in observed
        if isinstance(c,dict) and _physical_subject(c)}
    context=observed_address_context(story,observed)
    return [{'candidate_id':cid,
        'observed_literal_OSM_addresses':[{
            'entry_id':entry['mapped_entry_id'], 'address':entry['address'],
            'provenance':('osm_physical_own_address'
                if entry['mapped_entry_id']==cid
                else 'verified_osm_closed_way_entrance_membership')}
            for entry in _subject_addresses(context,body)],
        'physical_identity_inferred':False}
        for cid,body in bodies.items()]


def bounded_physically_linked_article_ids(catalogue, *, max_articles=2):
    """No host ranking: fetch 1..2 genuinely received articles, else let LLM choose.

    When >max_articles, return the COMPLETE received ID set for model
    selection, never a guessed first two based on address spellings.
    """
    rows=(catalogue or {}).get('results') or []
    ids=list(dict.fromkeys(row['article_id'] for row in rows
        if isinstance(row,dict) and isinstance(row.get('article_id'),str)))
    if not isinstance(max_articles,int) or isinstance(max_articles,bool) or max_articles<1:
        raise ValueError('invalid_architectural_article_prefetch_limit')
    return {'candidate_article_ids':ids,
        'prefetch_article_ids':ids if len(ids)<=max_articles else [],
        'ambiguous_excess_article_count':max(0,len(ids)-max_articles),
        'physical_identity_inferred':False,
        'selection_policy':'No host address ranking. LLM chooses from all actually received publisher cards.'}


def catalogue_model_context(catalogue):
    if not catalogue:
        return {}
    context={key:catalogue.get(key) for key in (
        'status','query_scope','total_count','received_row_count',
        'unique_article_count','inventory_complete','pagination_urls','limitation','error_code')}
    records=catalogue.get('physical_address_links') or {}
    context['results']=[{key:row[key] for key in (
            'article_id','canonical_url','coordinates') if key in row}
        | {key:str(row.get(key) or '')[:limit] for key,limit in (
            ('title',120),('address_text',240),('annotation',160))}
        | {'metadata_excerpt':True,
            'original_publisher_postal_metadata_not_an_identity':(
                records.get(row.get('article_id')) or {}).get(
                    'publisher_modern_address_metadata',[])}
        for row in catalogue.get('results') or []]
    context['independent_observed_OSM_postal_records_not_ranked']=(
        catalogue.get('observed_OSM_literal_addresses') or [])
    context['physical_prefetch_plan']=catalogue.get('physical_prefetch_plan') or {}
    context['coverage_policy']=(
        'Actual SOURCE pixels and publisher articles must be compared by the LLM. '
        'Raw publisher and OSM addresses are separate lists; no host string match, '
        'street-suffix fix, numerical interval parse or target identity is applied. '
        'Inventory completeness is scoped only to this original literal publisher query.')
    return context


def regional_selection_schema(candidate_ids, catalogue):
    ids = list(dict.fromkeys(row['article_id'] for row in catalogue.get('results') or []))
    return {'type':'array', 'maxItems':2, 'items':{'type':'object', 'properties':{
        'article_id':{'type':'string','enum':ids}, 'candidate_id':{'type':'string','enum':list(dict.fromkeys([*candidate_ids, '']))},
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
        # Article acquisition must not depend on the model already proving
        # the physical building. In particular a historical complex or SOURCE
        # without GPS can provide useful body text before the corpus/wings
        # are resolved. A nominated observed body is a *lead*, never identity.
        valid_lead = (selection['candidate_id'] == '' or
            candidate is not None and str(candidate.get('candidate_id') or '').startswith('osm:')
            and candidate_identity_eligible(candidate) and not article_candidate(candidate)
            and not (candidate.get('map_object') or {}).get('tags', {}).get('entrance'))
        if (aid in seen or not valid_lead
                or not selection['scope'].strip() or not selection['binding_basis'].strip()):
            return [], dict(receipt, reason='unobserved_source_lead_or_invalid_selection')
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
            'address':page.get('address_text') or '',
            'address_provenance':page.get('address_provenance') or '',
            'binding_basis':selection['binding_basis'],
            'lookup_candidate_ids':[selection['candidate_id']] if selection['candidate_id'] else [],
            'initial_physical_binding_hypothesis':selection['physical_binding_resolved'],
            'physical_identity_inferred':False,
            'card_variants':variants, 'fetched_at':page.get('fetched_at'), 'cache_hit':page.get('cache_hit',False)})
    receipt['status'] = 'completed' if len(articles) == len(choices) else 'partial' if articles else 'unavailable'
    return articles, receipt



async def acquire_architectural_pool_text(service, story, publisher_catalogue,
        selected_article_ids, *, max_articles=8):
    """Read 1..8 *actual received* publisher bodies without pre-proving a wing.

    An article retrieval choice is not an acceptance decision. Preserve
    physical uncertainty and true publisher modern addresses in each article
    so the later SOURCE+TEXT model can decide which building/corpus fits.
    Never silently drop overflow articles or pick first two catalog rows.
    """
    from .prussia39 import Prussia39Adapter
    from .research_budget import require_remaining, reserve_work
    if (not isinstance(selected_article_ids,list) or not selected_article_ids
            or len(selected_article_ids)>max_articles
            or len(set(selected_article_ids))!=len(selected_article_ids)):
        raise ValueError('bounded_distinct_publisher_articles_required')
    rows=(publisher_catalogue or {}).get('results') or []
    by_id={}
    for row in rows:
        if isinstance(row,dict) and isinstance(row.get('article_id'),str):
            by_id.setdefault(row['article_id'],[]).append(row)
    if any(aid not in by_id for aid in selected_article_ids):
        raise ValueError('unreceived_article_id')
    urls=[by_id[aid][0].get('canonical_url') for aid in selected_article_ids]
    if any(not isinstance(url,str) or not url for url in urls):
        raise ValueError('publisher_article_url_missing')
    timeout=22.0
    if hasattr(service,'settings'):
        timeout=min(timeout,require_remaining(service,story['id'],'identity'))
        reserve_work(service,story['id'],'pages',list(dict.fromkeys(urls)))
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=False) as client:
        adapter=Prussia39Adapter(service.store,client)
        gate=asyncio.Semaphore(4)
        async def read(url):
            async with gate:
                return await adapter.article(url)
        fetched=await asyncio.gather(*(read(url) for url in urls))
    result=[]
    receipt={'kind':'bounded_multiarticle_architecture',
        'publisher_query_scope':publisher_catalogue.get('query_scope'),
        'chosen_article_ids':list(selected_article_ids),
        'article_acquisitions':[], 'identity_inferred':False}
    for aid,page in zip(selected_article_ids,fetched):
        variants=by_id[aid]
        receipt['article_acquisitions'].append({'article_id':aid,'status':page.get('status'),
            'source_sha256':page.get('source_sha256')})
        if (page.get('status')!='completed'
                or page.get('article_id')!=aid
                or page.get('raw_body_sha256_verified') is not True
                or not (page.get('text') or '').strip()):
            continue
        body=page['text'][:12_000]
        result.append({'article_id':aid, 'url':page['canonical_url'],
            'source_sha256':page['raw_content_sha256'],
            'text_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'text':body,'raw_body_sha256_verified':True,
            'input_kind':'acquired_article_text',
            'title':page.get('title') or variants[0].get('title') or '',
            'source_image_links':list(page.get('source_image_links') or []),
            'address':page.get('address_text') or '',
            'address_provenance':page.get('address_provenance') or '',
            'card_variants':variants, 'lookup_candidate_ids':[],
            'physical_identity_inferred':False,
            'scope':'Publisher body acquired for semantic physical comparison; no corpus implied.',
            'fetched_at':page.get('fetched_at'),
            'cache_hit':page.get('cache_hit',False)})
    receipt['status']=('completed' if len(result)==len(selected_article_ids)
        else 'partial' if result else 'unavailable')
    receipt['articles_read']=len(result)
    return result,receipt


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



def literal_address_card_selection(rows, street, house_number):
    """Retrieve publisher cards with a *complete* literal modern address group.

    Street and house numbers must come from the actual received card metadata.
    The publisher may spell one OSM compound entrance 22/24 as the explicit
    pair "22, 24". This permits reading its article, NOT proving that a specific
    wing has that address. A query for 22 alone never selects a 22,24 complex.
    No title or partial street search response is allowed to impersonate an
    address. The LLM remains responsible for physical/architectural meaning.
    """
    if not isinstance(rows, list) or not isinstance(street, str) or not isinstance(house_number, str):
        return []
    token = re.compile(r"[^\W_]+", re.UNICODE)
    road_labels = {'ул', 'улица', 'пр', 'проспект', 'пер', 'переулок'}
    street_words = [word for word in token.findall(street.casefold()) if word not in road_labels]
    requested = house_number.casefold().strip()
    house_pattern = r"\d+[^\W\d_]*(?:/\d+[^\W\d_]*)?"
    if (not street_words or not requested or not
            re.fullmatch(house_pattern + r"(?:\s*,\s*" + house_pattern + r")*", requested)):
        return []
    requested_group = re.split(r"\s*[/,]\s*", requested)
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get('address_text')
        if not isinstance(raw, str) or not raw.strip() or not row.get('canonical_url'):
            continue
        text = raw.casefold()
        tokens = list(token.finditer(text))
        for i in range(len(tokens) - len(street_words) + 1):
            if [match.group() for match in tokens[i:i + len(street_words)]] != street_words:
                continue
            # Only the immediate postal-number group after this literal street,
            # not a year or incidental number elsewhere in the description.
            tail = text[tokens[i + len(street_words) - 1].end():]
            tail = re.sub(
                r"^\s*(?:улица|ул\.?|проспект|пр\.?|переулок|пер\.?)?\s*[,.;]?\s*"
                r"(?:(?:д\.?|дом|№)\s*)?", "", tail)
            match = re.match(house_pattern + r"(?:\s*,\s*" + house_pattern + r")*", tail)
            if match and re.split(r"\s*[/,]\s*", match.group()) == requested_group:
                selected.append(row)
                break
    return selected

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
    reverse = _reverse_address(story)
    locality = next((reverse[key] for key in ('city', 'town', 'village') if reverse.get(key)), '')
    anchors = {item['mapped_entry_id']: item['address'] for item in context['address_anchors']}
    bound_entries = [entry for cid in ids for entry in _subject_addresses(context, catalog[cid])]
    addresses = [address for cid in ids for address in (
        [entry['address'] for entry in _subject_addresses(context, catalog[cid])]
        or [catalog[cid].get('map_address') or {}])]
    entry_id = request.get('address_entry_id')
    if entry_id:
        memberships = {item['physical_candidate_id']: {entry['mapped_entry_id'] for entry in item['address_entries']}
            for item in context['building_address_memberships']}
        if entry_id not in anchors or any(entry_id != cid and entry_id not in memberships.get(cid, set()) for cid in ids):
            return [], {'status': 'not_sent', 'reason': 'address_entry_not_bound_to_nominated_footprint'}
        addresses = [anchors[entry_id]]
    # Publisher region is anchored to observed candidate/entrance metadata,
    # never an invented location or the camera's reverse-geocoded street.
    if not _regional_area(story, [
            catalog[cid] for cid in ids if cid in catalog
        ] + [
            {'map_address': address} for address in addresses
        ]):
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
        lookup['query_scope'] = {'route':request['route'], 'candidate_ids':ids,
            'address_entry_ids':[entry_id] if entry_id else list(dict.fromkeys(
                entry['mapped_entry_id'] for entry in bound_entries)),
            'provenance':'received_physical_subject', 'target_identity_established':False,
            **({'city':city, 'street':street, 'house_number':number} if request['route'] == 'address'
                else {'coordinates':points[0], 'position_kind':'subject_point_not_camera'})}
        results = lookup.get('results') or []
        record_identity_event(service, story['id'], 'identity_regional_lookup_completed', {
            'route': request['route'], 'status': lookup.get('status'), 'result_count': len(results),
            'candidate_ids': ids, 'cache_hit': lookup.get('cache_hit', False)})
        # A publisher search can return many cards or a partial page even when
        # one exact modern address is clearly labelled. Read only that received
        # literal metadata match; never take the first two or claim a full index.
        # Even a singleton "complete" address response can contain a neighboring
        # sight: catalogue completion is scoped to the publisher query, not proof
        # that the listed address is the requested physical subject.
        chosen = []
        if request['route'] == 'address' and lookup.get('status') == 'completed':
            labelled = literal_address_card_selection(results, street, number)
            distinct = list(dict.fromkeys(row['canonical_url'] for row in labelled))
            lookup['literal_address_selection'] = {
                'policy': 'exact_received_catalogue_address_metadata_v1',
                'matched_rows': len(labelled), 'selected_urls': len(distinct),
                'identity_inferred': False, 'unmatched_rows': len(results) - len(labelled),
                'inventory_complete': lookup.get('inventory_complete') is True}
            if 1 <= len(distinct) <= 2:
                chosen = labelled
        elif (request['route'] == 'coordinate' and lookup.get('status') == 'completed'
                and lookup.get('inventory_complete') is True and 1 <= len(results) <= 2):
            # Coordinate search supplies proximity candidates, not a confirmed
            # postal address. The visual/text model must establish identity.
            chosen = results
        if lookup.get('status') != 'completed' or not chosen:
            if lookup.get('status') == 'completed' and results:
                lookup['limitation'] = ('Received catalogue remains fully available; no first-two shortcut. '
                    'No closed unambiguous address card was available for an automatic body read.')
            return [], lookup
        if hasattr(service, 'settings'):
            from .research_budget import reserve_work
            reserve_work(service, story['id'], 'pages', [item['canonical_url'] for item in chosen])
        urls = list(dict.fromkeys(item['canonical_url'] for item in chosen))
        pages = await asyncio.gather(*(adapter.article(url) for url in urls))
    # Retain the publisher's *actual displayed modern addresses*. The article
    # reader may return only historical body prose and a blank address field.
    # Dropping this catalogue metadata previously concealed complex/single
    # house ambiguity from the SOURCE/T model.
    metadata_by_url = {url: [dict(row) for row in chosen
        if row.get('canonical_url') == url] for url in urls}
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
            'source_image_links':list(page.get('source_image_links') or []),
            'address_provenance': page.get('address_provenance') or '',
            'coordinates': page.get('coordinates'), 'fetched_at': page.get('fetched_at'),
            'cache_hit': page.get('cache_hit', False), 'lookup_candidate_ids': ids,
            'card_variants': metadata_by_url.get(page['canonical_url'], []),
            'lookup_scope': lookup.get('query_scope'),
            'physical_binding_claimed': False})
    return articles, lookup
