"""Bounded publisher transport; observed cards and text never establish identity.

The HTML contract was documented by the immutable 68a34bab research probe.
GET acquisitions share the ordinary public article cache. No images, automatic
pagination, ranking, replacement location, or model calls are performed here.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import re
import weakref
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .article_media import MAX_PAGE_BYTES, cached_public_page, resolve_public

BASE = 'https://www.prussia39.ru'
DATABASE = BASE + '/sight/database.php'
COORDINATES = BASE + '/sight/map_coord.php'
COOLDOWN = 'prussia39:transport-cooldown-v1'
READ_TIMEOUT_SECONDS = 12.0
_LOCKS = weakref.WeakKeyDictionary()
DEFAULTS = {
    'text_n': 'Название достопримечательности', 'text_np': 'Населенный пункт',
    'text_adr': 'Адрес', 'text_link': 'Официальный сайт', 'text_cph': 'Код',
    'text_nph': 'Номер телефона', 'w_id_wout': '0', 'w_id_photo': '0', 'w_st_okn': '0',
}


class _PublisherGET:
    """Preserve the common acquisition implementation with this site's UA."""
    def __init__(self, client, receipt=None):
        self.client, self.receipt = client, receipt

    def stream(self, method, url, **kwargs):
        from .research_budget import guard_research_send
        guard_research_send()
        if self.receipt is not None:
            self.receipt['http_dispatch_started'] = True
        kwargs['headers'] = dict(kwargs.get('headers', {}), **{'User-Agent': 'StreetStoryResearch/1.0'})
        return self.client.stream(method, url, **kwargs)


def _literal(value):
    # Orthographic transport normalization retains numbers, suffixes and ranges.
    return re.sub(r'\s+', ' ', str(value)).strip().replace('–', '-').replace('—', '-')


def _cached_body(store, key):
    entry = store.cache_get(key) or {}
    try:
        raw = base64.b64decode(entry.get('body', ''), validate=True)
        return bool(raw and entry.get('sha256') == hashlib.sha256(raw).hexdigest())
    except (ValueError, TypeError):
        return False


def cached_get_available(store, url):
    return _cached_body(store, 'public-article-acquisition-v1:' + hashlib.sha256(url.encode()).hexdigest())


def _address_query(value):
    """Publisher orthography only; original OSM literals stay in the receipt.

    OSM can spell a street type after its name while the catalog prefixes it.
    Removing that transport label does not merge addresses or prove identity.
    """
    text = _literal(value)
    text = re.sub(r'^(?:улица|ул\.|проспект|пр-т|переулок|пер\.)\s+', '', text, flags=re.I)
    return re.sub(r'\s+(?:улица|проспект|переулок)(?=\s*,|$)', '', text, flags=re.I)


def canonical_article(value):
    if isinstance(value, int) and not isinstance(value, bool):
        sid = value
    else:
        parsed = urlsplit(urljoin(BASE + '/sight/', str(value)))
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in
                {'www.prussia39.ru', 'prussia39.ru'} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or parsed.path != '/sight/index.php'):
            raise ValueError('invalid_article_url')
        ids = parse_qs(parsed.query).get('sid', [])
        if len(ids) != 1 or not re.fullmatch(r'[0-9]{1,12}', ids[0]):
            raise ValueError('invalid_article_id')
        sid = int(ids[0])
    if not 0 < sid < 10**12:
        raise ValueError('invalid_article_id')
    return sid, BASE + f'/sight/index.php?sid={sid}'


def _page_url(value):
    parsed = urlsplit(urljoin(DATABASE, value))
    if (parsed.scheme != 'https' or parsed.hostname != 'www.prussia39.ru'
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.path != '/sight/database.php' or len(value) > 4096):
        raise ValueError('invalid_pagination_url')
    query = parse_qs(parsed.query, encoding='cp1251', errors='strict')
    if not query.get('text_adr') or not re.fullmatch(r'[0-9]{1,4}', ''.join(query.get('p', []))):
        raise ValueError('invalid_pagination_url')
    return urlunsplit(('https', 'www.prussia39.ru', parsed.path, parsed.query, ''))


def _decode(raw):
    if not raw:
        raise ValueError('empty_body')
    declaration = re.search(r'charset\s*=\s*["\x27]?([\w-]+)', raw[:4096].decode('ascii', 'ignore'), re.I)
    encoding = declaration.group(1).lower() if declaration else 'cp1251'
    if encoding not in {'cp1251', 'windows-1251', 'utf-8', 'utf8'}:
        raise ValueError('unsupported_encoding')
    text = raw.decode(encoding, errors='strict')
    soup = BeautifulSoup(text, 'html.parser')
    if soup.find('html') is None:
        raise ValueError('unexpected_body')
    return soup, encoding


def _text(node):
    return '\n'.join(re.sub(r'\s+', ' ', line).strip()
                     for line in node.get_text('\n', strip=True).splitlines() if line.strip())


def _card(link):
    sid, url = canonical_article(link.get('href', ''))
    return {'article_id': f'prussia39:sid:{sid}', 'sid': sid, 'canonical_url': url,
            'title': link.get_text(' ', strip=True), 'address_text': '', 'annotation': ''}


def parse_address(soup, page_url):
    marker = soup.find(string=lambda text: text and 'Результаты поиска' in text)
    if marker is None:
        raise ValueError('address_results_section_missing')
    # A publisher results container, not links elsewhere in the form/navigation.
    container = marker.find_parent(['td', 'section', 'article', 'div']) or soup.body
    if container is None:
        raise ValueError('address_results_section_missing')
    total_match = re.search(r'всего совпадений\s*:\s*(\d+)', container.get_text(' ', strip=True), re.I)
    if total_match is None:
        raise ValueError('address_total_missing')
    total = int(total_match.group(1))
    results, seen, pages = [], set(), []
    after_marker = False
    for node in container.descendants:
        if node is marker:
            after_marker = True
        if not after_marker or getattr(node, 'name', None) != 'a' or not node.get('href'):
            continue
        href = urljoin(page_url, node['href'])
        try:
            card = _card(node)
        except ValueError:
            try:
                page = _page_url(href)
                if page not in pages and page != page_url:
                    pages.append(page)
            except ValueError:
                pass
            continue
        row = node.find_parent('tr')
        # Thumbnail/text links in one publisher row are one card. Distinct
        # address rows may name the same canonical complex article; retain
        # those address aliases while the article cache deduplicates bodies.
        row_key = (card['sid'], id(row) if row is not None else id(node))
        if row_key in seen:
            continue
        seen.add(row_key)
        if row is not None:
            card['annotation'] = _text(row)[:2000]
            # Publisher card layout qualified 2026-10-08: a justified text
            # cell contains bold title and a direct small-print address p.
            # This reads a declared DOM field, without deriving an address
            # from words in a title or a nearest mapped object.
            cell = node.find_parent('td')
            if cell is not None and re.search(r'text-align\s*:\s*justify', cell.get('style', ''), re.I):
                title = cell.find('b', recursive=False)
                address = cell.find('p', recursive=False)
                if title is not None:
                    card['title'] = title.get_text(' ', strip=True)
                if address is not None and re.search(r'font-size\s*:\s*0?8pt', address.get('style', ''), re.I):
                    card['address_text'] = address.get_text(' ', strip=True)
            explicit = row.find(attrs={'data-address': True})
            if explicit is not None:
                card['address_text'] = explicit['data-address']
            else:
                label = row.find(string=lambda t: t and re.match(r'^\s*Адрес\s*:', t))
                if label is not None:
                    card['address_text'] = re.sub(r'^\s*Адрес\s*:\s*', '', str(label)).strip()
        results.append(card)
    if (total == 0 and results) or (total > 0 and not results) or len(results) > total:
        raise ValueError('address_result_count_mismatch')
    return {'results': results, 'total_count': total, 'received_row_count':len(results),
            'unique_article_count':len({card['article_id'] for card in results}), 'pagination_urls': pages,
            'inventory_complete': total == len(results) and not pages}


def parse_coordinates(soup):
    arrays = {}
    for script in soup.find_all('script'):
        source = script.string or script.get_text()
        for name in ('name_fgr', 'coords'):
            match = re.search(r'\bvar\s+' + name + r'\s*=\s*', source)
            if match:
                if name in arrays:
                    raise ValueError('coordinate_arrays_ambiguous')
                try:
                    arrays[name], _ = json.JSONDecoder().raw_decode(source[match.end():].lstrip())
                except ValueError as exc:
                    raise ValueError('coordinate_array_invalid') from exc
    names, points = arrays.get('name_fgr'), arrays.get('coords')
    if not isinstance(names, list) or not isinstance(points, list) or len(names) != len(points):
        raise ValueError('coordinate_arrays_missing_or_mismatched')
    results, seen = [], set()
    for label, point in zip(names, points):
        if (not isinstance(label, str) or not isinstance(point, list) or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in point)
                or not all(math.isfinite(v) for v in point)
                or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180):
            raise ValueError('coordinate_card_invalid')
        links = BeautifulSoup(label, 'html.parser').find_all('a', href=True)
        if len(links) != 1:
            raise ValueError('coordinate_card_link_invalid')
        card = _card(links[0])
        if card['sid'] not in seen:
            card['coordinates'] = {'latitude': point[0], 'longitude': point[1],
                                   'provenance': 'publisher_coordinate_array'}
            card['annotation'] = BeautifulSoup(label, 'html.parser').get_text(' ', strip=True)
            results.append(card)
            seen.add(card['sid'])
    return {'results': results, 'total_count': len(results), 'pagination_urls': [],
            'inventory_complete': True, 'inventory_scope': 'publisher_coordinate_window',
            'window_bounds': None}


def parse_article(soup):
    blocks = []
    for node in soup.find_all('td', style=True):
        styles = dict(part.strip().lower().split(':', 1) for part in node['style'].split(';') if ':' in part)
        if styles.get('text-align', '').strip() != 'justify':
            continue
        for unwanted in node.find_all(['script', 'style', 'nav']):
            unwanted.decompose()
        value = _text(node)
        if value and not value.startswith('Все фотографии'):
            blocks.append(value.split('Интерактивный путеводитель', 1)[0].strip())
    if not blocks or not any(blocks):
        raise ValueError('article_body_unavailable')
    body = max(blocks, key=len)
    return {'title': soup.title.get_text(' ', strip=True) if soup.title else '',
            'text': body, 'address_text': '', 'coordinates': None,
            'extraction_method': 'publisher_justify_td_v1'}


class Prussia39Adapter:
    def __init__(self, store, http, *, resolver=resolve_public):
        self.store, self.http, self.resolver = store, http, resolver

    @staticmethod
    def address_url(city, address, *, name=''):
        params = dict(DEFAULTS, text_np=_literal(city), text_adr=_address_query(address))
        if not params['text_np'] or not params['text_adr']:
            raise ValueError('address_context_missing')
        if name:
            params['text_n'] = _literal(name)
        return DATABASE + '?' + urlencode(params, encoding='cp1251', errors='strict')

    async def address_search(self, city, address, *, name=''):
        try:
            url = self.address_url(city, address, name=name)
        except (ValueError, UnicodeError) as exc:
            return {'status': 'not_sent', 'error_code': str(exc), 'results': []}
        return await self._read(url, 'address', original_query={'city': str(city), 'address': str(address), 'name': str(name)})

    async def search_page(self, observed_pagination_url, *, previous_receipt):
        try:
            url = _page_url(observed_pagination_url)
            if url not in previous_receipt.get('pagination_urls', []):
                raise ValueError('pagination_not_observed')
        except (ValueError, TypeError) as exc:
            return {'status': 'not_sent', 'error_code': str(exc), 'results': []}
        return await self._read(url, 'address')

    async def coordinate_search(self, latitude, longitude):
        if (isinstance(latitude, bool) or isinstance(longitude, bool)
                or not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float))
                or not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            return {'status': 'not_sent', 'error_code': 'coordinate_context_invalid', 'results': []}
        body = urlencode({'new_coords': f'{latitude},{longitude}'}).encode('ascii')
        return await self._read(COORDINATES, 'coordinate', body=body,
                                original_query={'latitude': latitude, 'longitude': longitude})

    async def article(self, sid_or_url):
        try:
            sid, url = canonical_article(sid_or_url)
        except (ValueError, TypeError) as exc:
            return {'status': 'not_sent', 'error_code': str(exc)}
        result = await self._read(url, 'article')
        if result['status'] == 'completed':
            result.update(article_id=f'prussia39:sid:{sid}', sid=sid, url=url, canonical_url=url,
                          text_sha256=hashlib.sha256(result['text'].encode()).hexdigest(),
                          raw_body_sha256_verified=True, input_kind='acquired_article_text')
        return result

    async def _post(self, url, body):
        # One fixed, public publisher endpoint. POST redirects are not silently
        # replayed as another operation; DNS/SNI pinning matches public GETs.
        ip = await self.resolver('www.prussia39.ru')
        if not ipaddress.ip_address(ip).is_global:
            raise ValueError('article_media_private_address')
        authority = f'[{ip}]' if ':' in ip else ip
        pinned = urlunsplit(('https', authority, urlsplit(url).path, '', ''))
        from .research_budget import guard_research_send
        guard_research_send()
        async with self.http.stream('POST', pinned, content=body,
                headers={'Host': 'www.prussia39.ru', 'User-Agent': 'StreetStoryResearch/1.0',
                         'Content-Type': 'application/x-www-form-urlencoded'},
                extensions={'sni_hostname': 'www.prussia39.ru'}) as response:
            if 300 <= response.status_code < 400:
                raise ValueError('coordinate_redirect_unavailable')
            response.raise_for_status()
            raw = bytearray()
            async for part in response.aiter_bytes():
                if len(raw) + len(part) > MAX_PAGE_BYTES:
                    raise ValueError('article_media_size')
                raw.extend(part)
            return url, response.headers.get('content-type', '').split(';')[0], bytes(raw)

    async def _read(self, url, kind, *, body=None, original_query=None):
        digest = hashlib.sha256(url.encode() + b'\0' + (body or b'')).hexdigest()
        receipt = {'status': 'transport_failed', 'requested_url': url, 'url': url,
                   'method': 'POST' if body is not None else 'GET', 'operation': kind,
                   'query_sha256': digest, 'results': [], 'cache_hit': False}
        if original_query is not None:
            receipt['original_query'] = original_query
        key = ('prussia39:coordinate-acquisition-v1:' + digest if body is not None else
               'public-article-acquisition-v1:' + hashlib.sha256(url.encode()).hexdigest())
        cooldown = self.store.cache_get(COOLDOWN)
        if cooldown and not _cached_body(self.store, key):
            return dict(receipt, status='not_sent', error_code='provider_cooldown', retry_at=cooldown['retry_at'])
        locks = _LOCKS.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
        lock_key = (str(self.store.path.resolve()), key)
        lock = locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            locks[lock_key] = lock
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS), lock:
                cached = self.store.cache_get(key)
                receipt['cache_hit'] = bool(cached)
                if body is None:
                    final, mime, raw = await cached_public_page(self.store, _PublisherGET(self.http, receipt), url, resolver=self.resolver)
                    cached = self.store.cache_get(key)
                elif cached and hashlib.sha256(base64.b64decode(cached['body'])).hexdigest() == cached['sha256']:
                    final, mime, raw = cached['final_url'], cached['mime'], base64.b64decode(cached['body'])
                else:
                    final, mime, raw = await self._post(url, body)
                    if raw and mime in {'text/html', 'application/xhtml+xml', 'text/plain'}:
                        cached = {'final_url': final, 'mime': mime, 'body': base64.b64encode(raw).decode(),
                                  'sha256': hashlib.sha256(raw).hexdigest(), 'acquired_at': self.store.now()}
                        self.store.cache_put(key, cached, 86400)
                receipt.update(url=final, http_status=200, source_sha256=hashlib.sha256(raw).hexdigest(),
                               raw_content_sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                               fetched_at=(cached or {}).get('acquired_at', self.store.now()))
                origin = urlsplit(final)
                if origin.hostname not in {'www.prussia39.ru', 'prussia39.ru'}:
                    raise ValueError('publisher_redirect_unavailable')
                if kind == 'article' and canonical_article(final)[0] != canonical_article(url)[0]:
                    raise ValueError('publisher_article_redirect_mismatch')
                if not raw:
                    raise ValueError('empty_body')
                receipt['status'] = 'parse_failed'
                if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
                    raise ValueError('unexpected_content_type')
                soup, encoding = _decode(raw)
                receipt['encoding'] = encoding
                parsed = (parse_article(soup) if kind == 'article' else parse_coordinates(soup)
                          if kind == 'coordinate' else parse_address(soup, final))
                receipt.update(parsed, status='completed' if kind == 'article' or parsed['results'] else 'completed_empty')
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            receipt.update(status='transport_failed', http_status=status, error_code=f'http_{status}')
            if status == 429:
                value = exc.response.headers.get('Retry-After', '')
                try:
                    seconds = float(value) if value else 60
                except ValueError:
                    try:
                        seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        seconds = 60
                seconds = max(1, min(86400, math.ceil(seconds)))
                receipt['retry_at'] = self.store.now() + seconds
                self.store.cache_put(COOLDOWN, {'retry_at': receipt['retry_at']}, seconds)
        except (httpx.RequestError, TimeoutError) as exc:
            receipt.update(status='transport_failed', error_code=type(exc).__name__)
        except (ValueError, UnicodeError, TypeError, KeyError) as exc:
            receipt['error_code'] = str(exc)
            if str(exc) == 'empty_body':
                receipt['status'] = 'transport_failed'
        finally:
            # A cancelled optional GET keeps its actual dispatch boundary. The
            # owned caller drains it; this is not an inference UNKNOWN receipt.
            self.last_receipt = dict(receipt)
        return receipt
