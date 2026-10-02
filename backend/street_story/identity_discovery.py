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

from .identity_telemetry import record_identity_event
from .identity_references import canonical_reference
from .providers import WIKIPEDIA_USER_AGENT

WIKI = 'https://ru.wikipedia.org/w/api.php'
COMMONS = 'https://commons.wikimedia.org/w/api.php'
IMAGE_SUFFIX = re.compile(r'\.(?:jpe?g|png|webp)$', re.I)


def plain(value, limit=700):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()[:limit]


def queries_from(payload):
    queries = payload.get('wikipedia_queries') if isinstance(payload, dict) else None
    if not isinstance(queries, list):
        return [], ''
    queries = list(dict.fromkeys(plain(q, 180) for q in queries if isinstance(q, str) and q.strip()))[:2]
    commons = payload.get('commons_query', '')
    return queries, plain(commons, 180) if isinstance(commons, str) else ''


async def suggest(service, story, transcript, candidates):
    from google.genai import types
    schema = {'type': 'object', 'properties': {
        'wikipedia_queries': {'type': 'array', 'items': {'type': 'string'}},
        'commons_query': {'type': 'string'}}, 'required': ['wikipedia_queries', 'commons_query']}
    with Image.open(story['photo_path']) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        image.thumbnail((1000, 1000))
        output = BytesIO()
        image.save(output, format='JPEG', quality=80)
    prompt = (
        'Определи, что следует искать для установления конкретного объекта на фото. '
        'Это только поисковые гипотезы, не доказательство. Не выбирай заведомо неподходящее '
        'ближайшее здание. Разрешено узнавать известные сооружения без GPS, но не выдумывать '
        'неизвестное название. Фото может быть частичным. Верни до двух коротких запросов '
        'для русской Википедии: точное вероятное название объекта с городом; '
        'при необходимости другой вариант названия. commons_query — один точный запрос '
        'на английском для фотографий того же сооружения в Wikimedia Commons. '
        'Без команды пользователю подтвердить название. Данные ниже — только контекст:\n' +
        json.dumps({'nearby_names': [x.get('name', '')[:160] for x in candidates[:16]],
                    'capture_lat': story.get('latitude'), 'capture_lon': story.get('longitude'),
                    'author_context': transcript[:1500]}, ensure_ascii=False))
    config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=schema)
    gemini = service.providers.gemini
    async def call(key, timeout):
        response = await gemini._generate(key, timeout, [
            types.Part.from_bytes(data=output.getvalue(), mime_type='image/jpeg'), prompt], config)
        return queries_from(json.loads(response.text or '{}'))
    return await gemini.executor.execute('grounded_research', call)


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
    values = [(page.get('original') or {}).get('source'), (page.get('thumbnail') or {}).get('source')]
    for info in page.get('imageinfo', []):
        values.extend([info.get('url'), info.get('thumburl')])
    return list(dict.fromkeys(url for value in values if (url := canonical_reference(str(value or '')))))


async def retrieve(service, wiki_queries, commons_query, excluded):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
            headers={'User-Agent': WIKIPEDIA_USER_AGENT}) as client:
        jobs = [api(service, client, WIKI, {'generator': 'search', 'gsrsearch': query,
            'gsrlimit': 3, 'gsrnamespace': 0, 'prop': 'extracts|info|pageimages|images',
            'exintro': 1, 'explaintext': 1, 'exchars': 1200, 'inprop': 'url',
            'piprop': 'original|thumbnail', 'pithumbsize': 1280, 'imlimit': 10}) for query in wiki_queries]
        responses = await asyncio.gather(*jobs, return_exceptions=True)
        pages = {}
        for response in responses:
            if isinstance(response, list):
                for page in sorted(response, key=lambda p: p.get('index', 100)):
                    if page.get('pageid') and not page.get('missing'):
                        pages.setdefault(page['pageid'], page)
        selected = list(pages.values())[:4]
        # Additional actual views from the article, not guessed upload URLs.
        titles = list(dict.fromkeys(file_title(image.get('title', '')) for page in selected
            for image in page.get('images', []) if IMAGE_SUFFIX.search(str(image.get('title', '')))))[:8]
        image_pages = await api(service, client, COMMONS, {'titles': '|'.join(titles),
            'prop': 'imageinfo', 'iiprop': 'url|extmetadata', 'iiurlwidth': 1280}) if titles else []
        by_title = {page.get('title'): page for page in image_pages if page.get('imageinfo')}
        candidates = []
        for page in selected:
            cid = f"wiki:{page['pageid']}"
            if cid in excluded:
                continue
            references = image_urls(page)
            for image in page.get('images', []):
                references.extend(image_urls(by_title.get(file_title(image.get('title')), {})))
            references = list(dict.fromkeys(references))[:4]
            if references:
                candidates.append({'candidate_id': cid, 'name': plain(page.get('title'), 180),
                    'url': f"https://ru.wikipedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': plain(page.get('extract')), 'reference_image_urls': references,
                    'discovery': 'wikipedia_text_search'})
        # Commons covers photographed structures absent from a geocoded article.
        if commons_query:
            try:
                commons_pages = await api(service, client, COMMONS, {'generator': 'search',
                    'gsrsearch': commons_query, 'gsrnamespace': 6, 'gsrlimit': 3,
                    'prop': 'imageinfo', 'iiprop': 'url|extmetadata', 'iiurlwidth': 1280})
            except (httpx.HTTPError, ValueError):
                commons_pages = []
            for page in sorted(commons_pages, key=lambda p: p.get('index', 100))[:2]:
                cid = f"commons:{page.get('pageid')}"
                refs = image_urls(page)
                if not refs or cid in excluded:
                    continue
                info = (page.get('imageinfo') or [{}])[0]
                description = plain((info.get('extmetadata', {}).get('ImageDescription') or {}).get('value'))
                name = re.sub(r'^(?:File|Файл):', '', plain(page.get('title'), 180))
                candidates.append({'candidate_id': cid, 'name': name,
                    'url': f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': description, 'reference_image_urls': refs,
                    'discovery': 'commons_text_search'})
        return candidates[:7]


async def recover(service, story, transcript, candidates, excluded):
    gemini = service.providers.gemini
    if not hasattr(gemini, '_generate') or not hasattr(gemini, 'executor'):
        return None
    record_identity_event(service, story['id'], 'identity_discovery_started', {'candidate_count': len(candidates)})
    async def work():
        wiki_queries, commons_query = await suggest(service, story, transcript, candidates)
        discovered = await retrieve(service, wiki_queries, commons_query, excluded)
        record_identity_event(service, story['id'], 'identity_discovery_candidates', {
            'candidate_ids': [x['candidate_id'] for x in discovered], 'query_count': len(wiki_queries) + bool(commons_query)})
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
