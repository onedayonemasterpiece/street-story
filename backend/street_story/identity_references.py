"""Select public reference URLs and their article metadata for vision transport."""
from __future__ import annotations
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MAX_BYTES = 8 * 1024 * 1024


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
    """Return public REF addresses; the vision provider reads the original image.

    No image download, image decoding, normalization, digest or disk storage.
    This call records supplied descriptors, never a visual verdict.
    """
    from .article_media import public_url
    result, seen_urls = [], set()
    for candidate in candidates:
        cid = candidate.get('candidate_id')
        if not cid:
            continue
        for raw in candidate.get('reference_image_urls') or []:
            url = public_url(raw)
            if not url or url in seen_urls:
                continue
            if len(result) >= max(0, min(6, limit)):
                return result
            seen_urls.add(url)
            descriptor = next((m for m in candidate.get('article_media') or []
                if m.get('image_url') == raw and m.get('article_url') == candidate.get('url')), {})
            extension = urlsplit(url).path.lower()
            mime = 'image/png' if extension.endswith('.png') else 'image/webp' if extension.endswith('.webp') else 'image/jpeg'
            result.append((cid, mime, url))
            if evidence is not None:
                allowed = ('article_url', 'image_url', 'kind', 'alt', 'figcaption', 'section_heading', 'context_text', 'article_title')
                evidence.append({**{key: descriptor[key] for key in allowed if key in descriptor},
                    'candidate_id': cid, 'source_url': url, 'image_url': url,
                    'reference_id': candidate.get('reference_id'), 'delivery': 'direct_public_url'})
    return result
