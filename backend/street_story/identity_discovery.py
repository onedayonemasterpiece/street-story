"""One bounded visual-query recovery when coordinate-only recall misses an object.

Vision proposes search terms, never proof. Only fetched Wikimedia records become
candidates, and the existing visual proof gate must still accept actual images.
"""
from __future__ import annotations
import asyncio
import hashlib
import html
from io import BytesIO
import json
import re
from urllib.parse import quote

import httpx
from PIL import Image, ImageOps

from .gemini import GeminiUnavailable
from .identity_candidate_policy import wikipedia_identity_eligible
from .identity_telemetry import record_identity_event
from .identity_references import canonical_reference, original_reference
from .providers import WIKIPEDIA_USER_AGENT, PermanentProviderError

WIKI = 'https://ru.wikipedia.org/w/api.php'
COMMONS = 'https://commons.wikimedia.org/w/api.php'
REGION_HINT = 'Калининградская область'
IMAGE_SUFFIX = re.compile(r'\.(?:jpe?g|png|webp)$', re.I)


def plain(value, limit=700):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()[:limit]


def queries_from(payload):
    if not isinstance(payload, dict):
        return '', [], '', ''
    queries = payload.get('wikipedia_queries')
    if not isinstance(queries, list):
        return '', [], '', ''
    entity = plain(payload.get('entity_name', ''), 180)
    queries = list(dict.fromkeys(plain(q, 180) for q in queries if isinstance(q, str) and q.strip()))[:2]
    visual_query = plain(payload.get('visual_query', ''), 180)
    commons = payload.get('commons_query', '')
    return entity, queries, visual_query, plain(commons, 180) if isinstance(commons, str) else ''


async def suggest(service, story, transcript, candidates):
    from google.genai import types
    schema = {'type': 'object', 'properties': {
        'entity_name': {'type': 'string'},
        'wikipedia_queries': {'type': 'array', 'items': {'type': 'string'}},
        'visual_query': {'type': 'string'},
        'commons_query': {'type': 'string'}}, 'required': ['entity_name', 'wikipedia_queries', 'visual_query', 'commons_query']}
    with Image.open(story['photo_path']) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        image.thumbnail((1000, 1000))
        output = BytesIO()
        image.save(output, format='JPEG', quality=80)
    prompt = (
        'Определи, что следует искать для установления конкретного физического объекта на фото. '
        'Это только поисковые гипотезы, не доказательство. Не выбирай заведомо неподходящее '
        'ближайшее здание. Разрешено узнавать известные сооружения без GPS, но не выдумывать '
        'неизвестное название. Фото может показывать только часть объекта. '
        'entity_name — короткое наиболее вероятное название именно сооружения/объекта по-русски; '
        'название города, района или общий тип здания не подходит. '
        'Верни до двух коротких запросов для русской Википедии по наиболее вероятным собственным именам. '
        'visual_query обязателен: это отдельный поисковый запрос только по реально видимым физическим признакам '
        '(материал, форма башни/крыши, часы, окна, декор, надписи) плюс region_hint; не вставляй туда entity_name. '
        'Он нужен, чтобы неверная первая догадка не запирала поиск на одном объекте. '
        'commons_query — аналогичный английский запрос по видимым признакам и region_hint для Wikimedia Commons, '
        'а не повтор entity_name. Не проси пользователя назвать или подтвердить объект. Данные ниже — только контекст:\n' +
        json.dumps({'region_hint': REGION_HINT,
                    'nearby_names': [x.get('name', '')[:160] for x in candidates[:16]],
                    'capture_lat': story.get('latitude'), 'capture_lon': story.get('longitude'),
                    'author_context': transcript[:1500]}, ensure_ascii=False))
    config = types.GenerateContentConfig(
        response_mime_type='application/json',
        response_json_schema=schema,
        system_instruction='Идентифицируй именно физическое сооружение. Город, район или область не являются ответом об объекте.',
    )
    gemini = service.providers.gemini
    async def call(key, timeout, *, model=None, quota=None):
        response = await gemini._generate(key, timeout, [
            types.Part.from_bytes(data=output.getvalue(), mime_type='image/jpeg'), prompt], config,
            operation='grounded_research', model=model, quota=quota)
        return queries_from(json.loads(response.text or '{}'))
    routes = getattr(gemini, 'research_routes', None)
    if not routes:
        return await gemini.executor.execute('grounded_research', call)
    retry_at = []
    for model, _pool, quota, executor in routes:
        async def routed_call(key, timeout, *, _model=model, _quota=quota):
            return await call(key, timeout, model=_model, quota=_quota)
        try:
            return await executor.execute('grounded_research', routed_call)
        except GeminiUnavailable as exc:
            if exc.retry_at is not None:
                retry_at.append(exc.retry_at)
            continue
        except PermanentProviderError as exc:
            if str(exc) == 'gemini:unsupported_model':
                continue
            raise
    if retry_at:
        raise GeminiUnavailable(min(retry_at), 'all_identity_discovery_models_unavailable')
    raise PermanentProviderError('gemini:unsupported_model')


async def api(service, client, endpoint, params):
    params = {'action': 'query', 'format': 'json', 'formatversion': 2, **params}
    key = 'identity-discovery-v1:' + hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
    cached = service.store.cache_get(key)
    if cached is not None:
        return cached
    response = await client.get(endpoint, params=params)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('identity_discovery_response_size')
    payload = response.json()
    if payload.get('error'):
        raise ValueError('identity_discovery_api_error')
    pages = payload.get('query', {}).get('pages', [])
    service.store.cache_put(key, pages, 86400 if pages else 300)
    return pages


def file_title(value):
    return re.sub(r'^(?:File|Файл|Image|Изображение):', 'File:', str(value), flags=re.I)


def image_urls(page):
    values = [(page.get('thumbnail') or {}).get('source'), (page.get('original') or {}).get('source')]
    for info in page.get('imageinfo', []):
        values.extend([info.get('thumburl'), info.get('url')])
    return list(dict.fromkeys(url for value in values if (url := canonical_reference(str(value or '')))))


def _tokens(value):
    return {token for token in re.findall(r'[a-zа-яё0-9]{4,}', plain(value, 500).casefold())}


def _category_names(page):
    return [re.sub(r'^Category:', '', str(item.get('title') or ''), flags=re.I)
            for item in page.get('categories', []) if isinstance(item, dict)]


def _generic_category(category):
    lowered = category.casefold()
    return ('russian heritage id' in lowered or any(fragment in lowered for fragment in (
        'cultural heritage monuments in russia', 'cc-by', 'uploaded via',
        'self-published', 'photographs by', 'files with', 'pages with',
        'wikimedia', 'coordinates', 'taken with', ' in kaliningrad oblast',
    )))


def _entity_keys(page, hint):
    keys = []
    hint_tokens = _tokens(hint)
    for category in _category_names(page):
        lowered = category.casefold()
        heritage = re.search(r'russian heritage id\s+(\d+)', lowered)
        if heritage:
            keys.append('heritage:' + heritage.group(1))
            continue
        if _generic_category(category):
            continue
        category_tokens = _tokens(category)
        if hint_tokens and category_tokens & hint_tokens:
            keys.append('category:' + ' '.join(sorted(category_tokens)))
    return list(dict.fromkeys(keys))[:8]


def _specific_aliases(page, hint):
    hint_tokens = _tokens(hint)
    scored = []
    for category in _category_names(page):
        if _generic_category(category):
            continue
        tokens = _tokens(category)
        overlap = len(tokens & hint_tokens)
        if overlap:
            scored.append((overlap, len(tokens), category))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[2] for item in scored[:4]]


def _name_score(candidate, entity_name):
    target = _tokens(entity_name)
    text = _tokens(str(candidate.get('name') or '') + ' ' + str(candidate.get('extract') or ''))
    overlap = len(target & text)
    rank = {'wikipedia_text_search': 5, 'commons_category': 4, 'commons_text_search': 2}.get(
        candidate.get('discovery'), 1)
    exact = 20 if entity_name and plain(candidate.get('name'), 180).casefold() == entity_name.casefold() else 0
    return exact + overlap * 10 + rank


def _alias_phrase(value):
    value = re.sub(r'\([^)]*\)', ' ', plain(value, 220))
    return re.sub(r'[^0-9A-Za-zА-Яа-яЁё]+', ' ', value.casefold()).strip()


def _alias_stems(value):
    words = re.findall(r'[0-9A-Za-zА-Яа-яЁё]{4,}', _alias_phrase(value))
    return {word if not re.fullmatch(r'[А-Яа-яЁё]+', word) else word[:max(4, len(word) - 2)]
            for word in words}


def _explicit_page_alias(left, right):
    left_name = _alias_phrase(left.get('name'))
    right_name = _alias_phrase(right.get('name'))
    left_extract = _alias_phrase(left.get('extract'))
    right_extract = _alias_phrase(right.get('extract'))
    left_stems = _alias_stems(left.get('name'))
    right_stems = _alias_stems(right.get('name'))
    left_extract_stems = _alias_stems(left.get('extract'))
    right_extract_stems = _alias_stems(right.get('extract'))
    return (
        (len(left_name) >= 10 and left_name in right_extract)
        or (len(right_name) >= 10 and right_name in left_extract)
        or (len(left_stems) >= 2 and left_stems.issubset(right_extract_stems))
        or (len(right_stems) >= 2 and right_stems.issubset(left_extract_stems))
    )


def merge_candidates(candidates, entity_name):
    if len(candidates) < 2:
        return candidates
    parent = list(range(len(candidates)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left
    refs = [{root for url in item.get('reference_image_urls', [])
             if (root := original_reference(str(url)))} for item in candidates]
    keys = [set(item.get('entity_keys') or []) for item in candidates]
    source_urls = [set(str(url) for url in item.get('source_urls', []) if url) for item in candidates]
    urls = [str(item.get('url') or '') for item in candidates]
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            source_membership = (
                (urls[left] and urls[left] in source_urls[right])
                or (urls[right] and urls[right] in source_urls[left])
            )
            explicit_alias = (
                candidates[left].get('discovery') == 'wikipedia_text_search'
                and candidates[right].get('discovery') == 'wikipedia_text_search'
                and _explicit_page_alias(candidates[left], candidates[right])
            )
            if refs[left] & refs[right] or keys[left] & keys[right] or source_membership or explicit_alias:
                union(left, right)
    grouped = {}
    for index, item in enumerate(candidates):
        grouped.setdefault(find(index), []).append(item)
    merged = []
    for members in grouped.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        best = max(members, key=lambda item: _name_score(item, entity_name))
        references = list(dict.fromkeys(
            url for item in members for url in item.get('reference_image_urls', [])))[:6]
        sources = list(dict.fromkeys(
            str(item.get('url') or '') for item in members if item.get('url')))[:6]
        aliases = list(dict.fromkeys(
            str(item.get('name') or '') for item in members if item.get('name')))[:8]
        merged_name = best.get('name')
        merged.append({
            **best, 'name': merged_name, 'reference_image_urls': references,
            'source_urls': sources, 'entity_aliases': aliases,
            'alias_candidate_ids': [item['candidate_id'] for item in members
                                    if item['candidate_id'] != best['candidate_id']],
            'multi_view': len({original_reference(url) for url in references
                               if original_reference(url)}) > 1,
            'discovery': 'wikimedia_entity_cluster',
        })
    return merged


async def category_candidates(service, client, searches, excluded, entity_name):
    category_pages = {}
    for query in list(dict.fromkeys(value for value in searches if value))[:4]:
        try:
            pages = await api(service, client, COMMONS, {
                'generator': 'search', 'gsrsearch': query, 'gsrnamespace': 14,
                'gsrlimit': 3, 'prop': 'categoryinfo'})
        except (httpx.HTTPError, ValueError):
            continue
        for page in pages:
            title = str(page.get('title') or '')
            if title.startswith('Category:'):
                category_pages.setdefault(title, page)
    result = []
    for title in list(category_pages)[:5]:
        cid = 'commonscat:' + hashlib.sha256(title.encode()).hexdigest()[:16]
        if cid in excluded:
            continue
        try:
            files = await api(service, client, COMMONS, {
                'generator': 'categorymembers', 'gcmtitle': title, 'gcmtype': 'file',
                'gcmlimit': 6, 'prop': 'imageinfo|categories', 'cllimit': 30,
                'iiprop': 'url|extmetadata', 'iiurlwidth': 1280})
        except (httpx.HTTPError, ValueError):
            continue
        refs = list(dict.fromkeys(url for page in files for url in image_urls(page)))[:6]
        if not refs:
            continue
        category_name = re.sub(r'^Category:', '', title)
        source_urls = [f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}"]
        source_urls.extend(
            f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title') or '').replace(' ', '_'))}"
            for page in files if page.get('title'))
        entity_keys = ['category:' + ' '.join(sorted(_tokens(category_name)))]
        entity_keys.extend(key for page in files
                           for key in _entity_keys(page, entity_name or ' '.join(searches)))
        result.append({
            'candidate_id': cid, 'name': category_name,
            'url': source_urls[0], 'source_urls': list(dict.fromkeys(source_urls))[:6],
            'extract': category_name, 'reference_image_urls': refs,
            'entity_keys': list(dict.fromkeys(entity_keys))[:8],
            'entity_aliases': [category_name],
            'multi_view': len({original_reference(url) for url in refs
                               if original_reference(url)}) > 1,
            'discovery': 'commons_category',
        })
    return result


async def retrieve(service, wiki_queries, commons_query, excluded, *, entity_name=''):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
            headers={'User-Agent': WIKIPEDIA_USER_AGENT}) as client:
        jobs = [api(service, client, WIKI, {
            'generator': 'search', 'gsrsearch': query, 'gsrlimit': 3, 'gsrnamespace': 0,
            'prop': 'extracts|info|pageimages|images', 'exintro': 1, 'explaintext': 1,
            'exchars': 1200, 'inprop': 'url', 'piprop': 'name|original|thumbnail',
            'pithumbsize': 1280, 'imlimit': 10}) for query in wiki_queries]
        responses = await asyncio.gather(*jobs, return_exceptions=True)
        pages = {}
        for response in responses:
            if isinstance(response, list):
                for page in sorted(response, key=lambda item: item.get('index', 100)):
                    if page.get('pageid') and not page.get('missing'):
                        pages.setdefault(page['pageid'], page)
        selected = list(pages.values())[:6]
        titles = list(dict.fromkeys(
            [file_title(page.get('pageimage', '')) for page in selected if page.get('pageimage')]
            + [file_title(image.get('title', '')) for page in selected
               for image in page.get('images', [])
               if IMAGE_SUFFIX.search(str(image.get('title', '')))]))[:12]
        image_pages = await api(service, client, COMMONS, {
            'titles': '|'.join(titles), 'prop': 'imageinfo|categories', 'cllimit': 30,
            'iiprop': 'url|extmetadata', 'iiurlwidth': 1280}) if titles else []
        by_title = {page.get('title'): page for page in image_pages if page.get('imageinfo')}
        candidates = await category_candidates(
            service, client, [commons_query, *wiki_queries, entity_name], excluded, entity_name)
        for page in selected:
            cid = f"wiki:{page['pageid']}"
            if cid in excluded:
                continue
            pageimage = by_title.get(file_title(page.get('pageimage')), {}) if page.get('pageimage') else {}
            references = image_urls(page) + image_urls(pageimage)
            related_pages = [pageimage] if pageimage else []
            for image in page.get('images', []):
                related = by_title.get(file_title(image.get('title')), {})
                if related:
                    related_pages.append(related)
                    references.extend(image_urls(related))
            references = list(dict.fromkeys(references))[:6]
            if references:
                hint = commons_query or ' '.join(wiki_queries) or entity_name
                aliases = list(dict.fromkeys(
                    alias for related in related_pages
                    for alias in _specific_aliases(related, hint)))[:6]
                entity_keys = list(dict.fromkeys(
                    key for related in related_pages
                    for key in _entity_keys(related, hint)))[:8]
                candidates.append({
                    'candidate_id': cid,
                    'name': aliases[0] if aliases else plain(page.get('title'), 180),
                    'url': f"https://ru.wikipedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': plain(page.get('extract')), 'reference_image_urls': references,
                    'identity_eligible': wikipedia_identity_eligible(
                        str(page.get('title') or ''), str(page.get('extract') or '')),
                    'entity_keys': entity_keys, 'entity_aliases': aliases,
                    'discovery': 'wikipedia_text_search'})
        if commons_query:
            try:
                commons_pages = await api(service, client, COMMONS, {
                    'generator': 'search', 'gsrsearch': commons_query, 'gsrnamespace': 6,
                    'gsrlimit': 5, 'prop': 'imageinfo|categories', 'cllimit': 30,
                    'iiprop': 'url|extmetadata', 'iiurlwidth': 1280})
            except (httpx.HTTPError, ValueError):
                commons_pages = []
            for page in sorted(commons_pages, key=lambda item: item.get('index', 100))[:5]:
                cid = f"commons:{page.get('pageid')}"
                refs = image_urls(page)
                if not refs or cid in excluded:
                    continue
                info = (page.get('imageinfo') or [{}])[0]
                description = plain(
                    (info.get('extmetadata', {}).get('ImageDescription') or {}).get('value'))
                name = re.sub(r'^(?:File|Файл):', '', plain(page.get('title'), 180))
                candidates.append({
                    'candidate_id': cid, 'name': name,
                    'url': f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': description, 'reference_image_urls': refs,
                    'entity_keys': _entity_keys(page, commons_query),
                    'discovery': 'commons_text_search'})
        return merge_candidates(candidates, entity_name)[:10]


async def web_search_hints(service, visual_query):
    search = getattr(service.providers.gemini, 'search_web', None)
    if not visual_query or not callable(search):
        return []
    try:
        result = await asyncio.wait_for(search(
            f"{visual_query} {REGION_HINT}",
            {'purpose': 'identity_candidate_discovery', 'region': REGION_HINT},
        ), timeout=12)
    except Exception:
        return []
    sources = getattr(result, 'grounding_sources', None) or []
    return list(dict.fromkeys(
        title for source in sources[:8]
        if isinstance(source, dict)
        and (title := plain(source.get('title'), 180))
        and not title.startswith('http')
    ))[:3]


async def recover(service, story, transcript, candidates, excluded):
    gemini = service.providers.gemini
    if not hasattr(gemini, '_generate') or not hasattr(gemini, 'executor'):
        return None
    record_identity_event(service, story['id'], 'identity_discovery_started', {'candidate_count': len(candidates)})
    async def work():
        entity_name, wiki_queries, visual_query, commons_query = await suggest(
            service, story, transcript, candidates)
        web_hints = await web_search_hints(service, visual_query)
        search_queries = list(dict.fromkeys([
            *wiki_queries,
            *([visual_query] if visual_query else []),
            *web_hints,
        ]))[:6]
        if web_hints:
            record_identity_event(service, story['id'], 'identity_web_search_hints', {
                'hint_count': len(web_hints)})
        discovered = await retrieve(
            service, search_queries, commons_query, excluded, entity_name=entity_name)
        record_identity_event(service, story['id'], 'identity_discovery_candidates', {
            'candidate_ids': [x['candidate_id'] for x in discovered],
            'query_count': len(search_queries) + bool(commons_query),
            'web_hint_count': len(web_hints),
            'entity_name_present': bool(entity_name)})
        if not discovered:
            return None
        # Six images maximum; source records, not hypotheses, define the candidates.
        result = await service._identify_photo_batch(story, transcript, discovered, reference_limit=6)
        return result, discovered
    try:
        return await asyncio.wait_for(work(), timeout=75)
    except Exception as exc:
        record_identity_event(service, story['id'], 'identity_discovery_unavailable', {'error_type': type(exc).__name__})
        return None
