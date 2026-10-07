"""Exact Wikipedia articles explicitly linked by nearby physical OSM objects."""
from __future__ import annotations

import math
import logging
import time
from urllib.parse import quote, unquote, urlsplit

import httpx

from .errors import RetryableProviderError
from .identity_candidate_policy import osm_identity_eligible, wikipedia_coordinate_context
from .providers import WIKIPEDIA_USER_AGENT, _stable_cache_key

LOG = logging.getLogger("uvicorn.error")


def _title(tags):
    value = str(tags.get('wikipedia:ru') or tags.get('wikipedia') or '').strip()
    if value.startswith('https://ru.wikipedia.org/wiki/'):
        value = unquote(urlsplit(value).path.removeprefix('/wiki/'))
    elif value.startswith('ru:'):
        value = value[3:]
    elif ':' in value and not tags.get('wikipedia:ru'):
        return ''
    value = value.replace('_', ' ').strip()
    return value if value and '|' not in value and len(value) <= 240 else ''


async def linked_pages(wikipedia_client, osm):
    """None means no usable mapped links; [] means links resolved to no page.

    An explicit lookup failure is retryable, never permission to substitute
    unrelated geographic articles. Only actual Wiki primary coordinates survive.
    """
    try:
        radius = min(160, float(osm.get('close_radius_m') or 160))
    except (TypeError, ValueError):
        radius = 160
    linked = {}
    present = False
    items = [osm.get('reverse') or {}, *(osm.get('nearby') or [])]
    def proximity(item):
        try:
            value = float(item.get('distance_m'))
            return value if math.isfinite(value) else math.inf
        except (TypeError, ValueError):
            return math.inf
    for item in sorted(items, key=proximity):
        tags = item.get('tags') or {}
        kind = item.get('osm_type') or item.get('type')
        osm_id = item.get('osm_id') or item.get('id')
        try:
            distance = float(item['distance_m'])
        except (KeyError, TypeError, ValueError):
            continue
        if (kind not in {'node', 'way', 'relation'} or osm_id is None
                or not math.isfinite(distance) or not 0 <= distance <= radius
                or not osm_identity_eligible(tags) or item.get('identity_eligible') is False
                or tags.get('boundary') or tags.get('route')
                or tags.get('highway') and not any(tags.get(k) for k in ('building', 'historic', 'bridge'))):
            continue
        if not (tags.get('wikipedia') or tags.get('wikipedia:ru')):
            continue
        present = True
        title = _title(tags)
        if title:
            linked.setdefault(title, []).append({'candidate_id': f'osm:{kind}:{osm_id}',
                'source_url': f'https://www.openstreetmap.org/{kind}/{osm_id}',
                'distance_m': distance, 'link': str(tags.get('wikipedia:ru') or tags.get('wikipedia')),
                **({'wikidata': tags['wikidata']} if tags.get('wikidata') else {})})
    if not present:
        return None
    if not linked:
        return []
    titles = list(linked)
    key = _stable_cache_key('wikipedia-mapped-titles-v1', titles)
    pages = wikipedia_client.store.cache_get(key)
    if pages is None:
        own = wikipedia_client.http is None
        client = wikipedia_client.http or httpx.AsyncClient(timeout=20, headers={'User-Agent': WIKIPEDIA_USER_AGENT})
        started = time.monotonic()
        try:
            LOG.info('street_story_wikipedia stage=exact_mapped_send title_count=%s', len(titles))
            response = await client.get(wikipedia_client.endpoint, params={
                'action': 'query', 'titles': '|'.join(titles), 'redirects': 1,
                'prop': 'extracts|info|pageimages|pageprops|coordinates', 'exintro': 1,
                'explaintext': 1, 'inprop': 'url', 'piprop': 'original|thumbnail',
                'pithumbsize': 1200, 'coprimary': 'primary', 'colimit': 1,
                'format': 'json', 'formatversion': 2}, headers={'User-Agent': WIKIPEDIA_USER_AGENT})
            response.raise_for_status()
            payload = response.json()
            if payload.get('error') or not isinstance(payload.get('query', {}).get('pages'), list):
                raise ValueError('mapped_wikipedia_invalid_response')
            pages = payload['query']['pages']
            if any(not isinstance(page, dict) for page in pages):
                raise ValueError('mapped_wikipedia_invalid_pages')
            aliases = {v['to']: v['from'] for field in ('normalized', 'redirects')
                       for v in payload.get('query', {}).get(field, []) if v.get('from') and v.get('to')}
            for page in pages:
                title = page.get('title')
                seen = set()
                while title in aliases and title not in seen:
                    seen.add(title)
                    title = aliases[title]
                page['_mapped_title'] = title
            wikipedia_client.store.cache_put(key, pages, 7*24*3600)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            LOG.warning('street_story_wikipedia stage=exact_mapped_failed title_count=%s error_type=%s duration_ms=%s',
                        len(titles), type(exc).__name__, round((time.monotonic()-started)*1000))
            raise RetryableProviderError('mapped_wikipedia_lookup_failed') from exc
        finally:
            if own:
                await client.aclose()
    results = []
    for page in pages:
        mapped = linked.get(page.get('_mapped_title', page.get('title')))
        if not mapped or page.get('missing') or not page.get('pageid'):
            continue
        url = page.get('fullurl') or 'https://ru.wikipedia.org/wiki/' + quote(page['title'].replace(' ', '_'))
        if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'ru.wikipedia.org':
            continue
        def image(key):
            value = (page.get(key) or {}).get('source')
            return value if value and urlsplit(value).scheme == 'https' and urlsplit(value).hostname == 'upload.wikimedia.org' else None
        coordinates = wikipedia_coordinate_context(page, None, None)
        results.append({'pageid': page['pageid'], 'title': page['title'], 'url': url,
            'extract': str(page.get('extract') or '')[:6000], 'image_url': image('original'),
            'thumbnail_url': image('thumbnail'), 'pageprops': page.get('pageprops') or {},
            **coordinates, 'distance_m': min(v['distance_m'] for v in mapped),
            'distance_provenance': 'capture_to_explicitly_linked_osm_object',
            'mapped_wikipedia_sources': mapped, 'discovery': 'osm_explicit_wikipedia_link'})
    LOG.info('street_story_wikipedia stage=exact_mapped_ready title_count=%s page_count=%s', len(titles), len(results))
    return results
