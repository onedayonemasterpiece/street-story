"""Bounded Wikimedia reference retrieval with explicit failures and 429 backoff.

No alternate host, credential or proxy bypass. Negative cache prevents the same
unavailable image being requested again by a targeted visual verification.
"""
from __future__ import annotations
import asyncio
import base64
import binascii
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


def original_reference(raw: str) -> str | None:
    """Canonical Commons original used to deduplicate original/thumbnail variants."""
    url = canonical_reference(raw)
    if not url:
        return None
    parsed = urlsplit(url)
    marker = "/wikipedia/commons/thumb/"
    if marker not in parsed.path:
        return url
    prefix, rest = parsed.path.split(marker, 1)
    parts = rest.rsplit("/", 1)
    if len(parts) != 2 or "/" not in parts[0]:
        return None
    return urlunsplit(("https", "upload.wikimedia.org",
        prefix + "/wikipedia/commons/" + parts[0], "", ""))


def thumbnail_reference(raw: str, width: int = 1280) -> str | None:
    """Derive Wikimedia's bounded raster thumbnail for a Commons original."""
    url = original_reference(raw)
    if not url:
        return None
    parsed = urlsplit(url)
    marker = "/wikipedia/commons/"
    if marker not in parsed.path:
        return None
    prefix, rest = parsed.path.split(marker, 1)
    if "/" not in rest:
        return None
    filename = rest.rsplit("/", 1)[-1]
    if not filename or filename.lower().endswith(".svg"):
        return None
    path = prefix + marker + "thumb/" + rest + f"/{width}px-{filename}"
    return urlunsplit(("https", "upload.wikimedia.org", path, "", ""))


async def reference_images(service, candidates, limit=6, *, story_id=None, http=None, evidence=None):
    if not hasattr(service, '_identity_reference_cache'):
        service._identity_reference_cache = OrderedDict()
    cache = service._identity_reference_cache
    result, seen_urls = [], set()
    own = http is None
    client = http or httpx.AsyncClient(timeout=8, follow_redirects=False,
        headers={'User-Agent': 'StreetStory/0.1 (https://github.com/onedayonemasterpiece/street-story) visual-reference'})

    def event(name, payload):
        if story_id:
            record_identity_event(service, story_id, name, payload)

    def receipt(cid, url, image, cache_hit, descriptor=None, *, article_source_url=None, resolved_url=None):
        if evidence is not None:
            provenance = dict(descriptor or {})
            if descriptor and article_source_url:
                # The extracted article URL identifies the source illustration;
                # Wikimedia may serve a bounded derivative of that same file.
                provenance.update(requested_image_url=url, retrieval_method='wikimedia_reference')
                if resolved_url:
                    provenance['resolved_image_url'] = resolved_url
                store = getattr(service, 'store', None)
                if store is not None:
                    key = 'public-article-acquisition-v1:' + hashlib.sha256(descriptor['article_url'].encode()).hexdigest()
                    saved_article = store.cache_get(key)
                    if saved_article and saved_article.get('final_url') == descriptor['article_url']:
                        try:
                            body = base64.b64decode(saved_article['body'], validate=True)
                            article_hash = hashlib.sha256(body).hexdigest()
                            if article_hash == saved_article.get('sha256'):
                                provenance['article_source_sha256'] = article_hash
                        except (KeyError, ValueError, TypeError, binascii.Error):
                            pass
            # Identity and the exact normalized bytes are host-owned, even if a
            # descriptor contains similarly named article metadata.
            evidence.append({**provenance, 'candidate_id': cid, 'source_url': article_source_url or url,
                'model_image_sha256': hashlib.sha256(image[1]).hexdigest(),
                'model_image_bytes': len(image[1]), 'cache_hit': cache_hit})

    async def fetch_variant(cid, url, *, descriptor=None, article_source_url=None):
        now = time.monotonic()
        saved = cache.get(url)
        if saved and saved[0] > now:
            if saved[1]:
                receipt(cid, url, saved[1], True, descriptor,
                    article_source_url=article_source_url, resolved_url=saved[3] if len(saved) > 3 else None)
                event('identity_reference_loaded', {
                    'candidate_id': cid, 'bytes': len(saved[1][1]), 'cache_hit': True})
                return saved[1]
            event('identity_reference_unavailable', {
                'candidate_id': cid, 'reason': saved[2], 'cache_hit': True})
            return None
        if getattr(service, '_wikimedia_reference_wait_until', 0) > now:
            event('identity_reference_unavailable', {
                'candidate_id': cid, 'reason': 'host_rate_limited', 'cache_hit': True})
            return None
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
        cache[url] = (time.monotonic() + (300 if image else 60), image, reason, target)
        cache.move_to_end(url)
        while len(cache) > 8:
            cache.popitem(last=False)
        event('identity_reference_loaded' if image else 'identity_reference_unavailable', {
            'candidate_id': cid, 'reason': reason, 'http_status': status,
            'duration_ms': round((time.monotonic() - started) * 1000),
            'bytes': len(image[1]) if image else 0, 'cache_hit': False})
        if image:
            receipt(cid, url, image, False, descriptor, article_source_url=article_source_url, resolved_url=target)
        return image

    try:
        remaining = max(0, min(6, limit))
        for candidate in [item for item in candidates if item.get('reference_image_urls')]:
            if remaining <= 0:
                break
            cid = candidate.get('candidate_id')
            if not cid:
                continue
            per_candidate = min(remaining if candidate.get('reference_batch') else (2 if candidate.get('multi_view') else 1), remaining)
            loaded_for_candidate = 0
            if candidate.get('discovery') == 'web_article_media':
                from .article_media import load_article_reference
                for raw in candidate.get('reference_image_urls', [])[:64]:
                    if loaded_for_candidate >= per_candidate or remaining <= 0:
                        break
                    if raw in seen_urls:
                        continue
                    seen_urls.add(raw)
                    try:
                        image, descriptor = await load_article_reference(client, candidate, raw)
                    except (httpx.HTTPError, ValueError, OSError) as exc:
                        event('identity_reference_unavailable', {'candidate_id': cid, 'reason': type(exc).__name__})
                        continue
                    receipt(cid, raw, image, False, descriptor)
                    event('identity_reference_loaded', {'candidate_id': cid, 'bytes': len(image[1]), 'source_kind': 'article_media'})
                    result.append((cid, image[0], image[1]))
                    loaded_for_candidate += 1
                    remaining -= 1
                continue
            groups = []
            seen_roots = set()
            for raw in candidate.get('reference_image_urls', [])[:6]:
                root = original_reference(str(raw))
                if not root or root in seen_roots:
                    continue
                seen_roots.add(root)
                variants = list(dict.fromkeys(
                    value for value in (thumbnail_reference(root), root) if value))
                groups.append((raw, variants))
            for raw, variants in groups:
                if loaded_for_candidate >= per_candidate or remaining <= 0:
                    break
                image = None
                descriptor = next((item for item in candidate.get('article_media', [])
                    if candidate.get('discovery') == 'wikipedia_article_media'
                    and item.get('image_url') == raw and item.get('article_url') == candidate.get('url')), None)
                for url in variants:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    image = await fetch_variant(cid, url, descriptor=descriptor,
                        article_source_url=raw if descriptor else None)
                    if image:
                        break
                if image:
                    result.append((cid, image[0], image[1]))
                    loaded_for_candidate += 1
                    remaining -= 1
    finally:
        if own:
            await client.aclose()
    return result
