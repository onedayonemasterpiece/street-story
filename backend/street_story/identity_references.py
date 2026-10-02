"""Bounded Wikimedia reference retrieval with explicit failures and 429 backoff.

No alternate host, credential or proxy bypass. Negative cache prevents the same
unavailable image being requested again by a targeted visual verification.
"""
from __future__ import annotations
import asyncio
import time
import math
from email.utils import parsedate_to_datetime
from collections import OrderedDict
from urllib.parse import urlsplit, urljoin, urlunsplit, parse_qsl, urlencode
import httpx
import hashlib
from .identity_telemetry import record_identity_event
from .reference_image_codec import MAX_DOWNLOAD_BYTES, normalize_reference

MAX_BYTES = MAX_DOWNLOAD_BYTES


def canonical_reference(raw: str) -> str | None:
    try:
        url = urlsplit(raw)
        if url.scheme != 'https' or url.hostname != 'upload.wikimedia.org' or url.port not in (None, 443) or url.username or url.password or url.fragment:
            return None
        return urlunsplit(('https', 'upload.wikimedia.org', url.path,
            urlencode([(k, v) for k, v in parse_qsl(url.query) if not k.startswith('utm_')]), ''))
    except (TypeError, ValueError):
        return None


async def reference_images(service, candidates, limit=6, *, story_id=None, http=None, evidence=None):
    if not hasattr(service, '_identity_reference_cache'):
        service._identity_reference_cache = OrderedDict()
    cache = service._identity_reference_cache
    result, seen = [], set()
    own = http is None
    client = http or httpx.AsyncClient(timeout=8, follow_redirects=False,
        headers={'User-Agent': 'StreetStory/0.1 (https://github.com/onedayonemasterpiece/street-story) visual-reference'})
    def event(name, payload):
        if story_id:
            record_identity_event(service, story_id, name, payload)
    def receipt(cid, url, image, cache_hit):
        if evidence is not None:
            evidence.append({'candidate_id': cid, 'source_url': url,
                'model_image_sha256': hashlib.sha256(image[1]).hexdigest(),
                'model_image_bytes': len(image[1]), 'cache_hit': cache_hit})
    try:
        for candidate in [x for x in candidates if x.get('reference_image_urls')][:max(0, min(6, limit))]:
            cid = candidate.get('candidate_id')
            for raw in candidate.get('reference_image_urls', [])[:2]:
                url = canonical_reference(str(raw))
                if not cid or not url or url in seen:
                    continue
                seen.add(url)
                now = time.monotonic()
                saved = cache.get(url)
                if saved and saved[0] > now:
                    if saved[1]:
                        result.append((cid, saved[1][0], saved[1][1]))
                        receipt(cid, url, saved[1], True)
                        event('identity_reference_loaded', {'candidate_id': cid, 'bytes': len(saved[1][1]), 'cache_hit': True})
                        break
                    event('identity_reference_unavailable', {'candidate_id': cid, 'reason': saved[2], 'cache_hit': True})
                    continue
                if getattr(service, '_wikimedia_reference_wait_until', 0) > now:
                    event('identity_reference_unavailable', {'candidate_id': cid, 'reason': 'host_rate_limited', 'cache_hit': True})
                    break
                reason, status, image = 'unavailable', None, None
                started = time.monotonic()
                try:
                    target = url
                    for hop in range(2):
                        async with client.stream('GET', target) as response:
                            status = response.status_code
                            if status in (301, 302, 307, 308) and hop == 0:
                                next_url = canonical_reference(urljoin(target, response.headers.get('location', '')))
                                if not next_url:
                                    reason = 'unsafe_redirect'
                                    break
                                target = next_url
                                continue
                            if status == 429:
                                try:
                                    raw_wait = response.headers.get('retry-after', '60')
                                    try:
                                        wait = float(raw_wait)
                                    except ValueError:
                                        wait = parsedate_to_datetime(raw_wait).timestamp() - time.time()
                                    wait = max(60.0, wait) if math.isfinite(wait) else 60.0
                                except (ValueError, TypeError, OverflowError):
                                    wait = 60.0
                                service._wikimedia_reference_wait_until = time.monotonic() + wait
                                reason = 'rate_limited'
                                break
                            if status != 200:
                                reason = 'http_error'
                                break
                            mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                            if mime not in {'image/jpeg', 'image/png', 'image/webp'}:
                                reason = 'unsupported_mime'
                                break
                            if int(response.headers.get('content-length') or 0) > MAX_BYTES:
                                reason = 'reference_too_large'
                                break
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(data) + len(chunk) > MAX_BYTES:
                                    data.clear()
                                    reason = 'reference_too_large'
                                    break
                                data.extend(chunk)
                            if data:
                                image = await asyncio.to_thread(normalize_reference, bytes(data))
                                reason = 'ready'
                            break
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    reason = type(exc).__name__
                cache[url] = (time.monotonic() + (300 if image else 60), image, reason)
                cache.move_to_end(url)
                while len(cache) > 8:
                    cache.popitem(last=False)
                event('identity_reference_loaded' if image else 'identity_reference_unavailable', {
                    'candidate_id': cid, 'reason': reason, 'http_status': status,
                    'duration_ms': round((time.monotonic() - started) * 1000),
                    'bytes': len(image[1]) if image else 0, 'cache_hit': False})
                if image:
                    result.append((cid, image[0], image[1]))
                    receipt(cid, url, image, False)
                    break
    finally:
        if own:
            await client.aclose()
    return result
