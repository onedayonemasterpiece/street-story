"""Bounded public article illustrations, with a quiet browser as a last resort.

Search hits are hypotheses. Only decoded article media can become visual evidence.
Public HTTP requests pin the validated DNS address and validate every redirect.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .identity_telemetry import record_identity_event
from .reference_image_codec import MAX_DOWNLOAD_BYTES, normalize_reference

MAX_PAGES = 20
MAX_PAGE_BYTES = 2 * 1024 * 1024
CHROME = re.compile(r'(?:^|[\s_/-])(?:ad|ads|advert\w*|banner|logo\w*|icon|avatar|footer|header|sidebar|related|recommend\w*|cookie|social|share|tracking|poster-item|interest-slider|content-right|contact|similar|widget)(?:$|[\s_/-])', re.I)
CONTENT = re.compile(r'(?:article|entry-content|post-content|news-detail|detail|articleBody|description|gallery|photo|content_container|mw-parser-output)', re.I)


def public_url(raw: str) -> str | None:
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or len(raw) > 4096
                or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal'))):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if '.' not in host:
                return None
        return urlunsplit(('https', parsed.netloc, parsed.path or '/', parsed.query, ''))
    except (ValueError, TypeError):
        return None


async def resolve_public(host: str) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('article_media_private_address')
    return next((ip for ip in ips if ':' not in ip), ips[0])


async def fetch_public(client, raw: str, maximum: int, *, resolver=resolve_public):
    target = public_url(raw)
    for _hop in range(5):
        if not target:
            raise ValueError('article_media_unsafe_url')
        parsed = urlsplit(target)
        ip = await resolver(parsed.hostname)
        if not ipaddress.ip_address(ip).is_global:
            raise ValueError('article_media_private_address')
        authority = f'[{ip}]' if ':' in ip else ip
        pinned = urlunsplit(('https', authority, parsed.path, parsed.query, ''))
        async with client.stream('GET', pinned, headers={'Host': parsed.hostname},
                extensions={'sni_hostname': parsed.hostname}) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = public_url(urljoin(target, response.headers.get('location', '')))
                continue
            response.raise_for_status()
            if int(response.headers.get('content-length') or 0) > maximum:
                raise ValueError('article_media_size')
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > maximum:
                    raise ValueError('article_media_size')
                body.extend(chunk)
            return target, response.headers.get('content-type', '').split(';')[0].lower(), bytes(body)
    raise ValueError('article_media_redirect_limit')


def extract_media(document: str, page_url: str) -> tuple[str, list[dict]]:
    """Use article/main/gallery media, never the site's whole image inventory."""
    soup = BeautifulSoup(document, 'html.parser')
    title = (soup.find('h1') or soup.find('title'))
    title = title.get_text(' ', strip=True)[:180] if title else ''
    roots = soup.select('article, [itemprop="articleBody"], main, [role="main"]')
    content_roots = soup.find_all(['div', 'section'], class_=CONTENT)
    roots.extend(node for node in content_roots if not any(parent in roots for parent in node.parents))
    media, seen = [], set()

    def add(raw, kind, alt=''):
        if not raw or not str(raw).strip():
            return
        url = public_url(urljoin(page_url, str(raw or '').strip()))
        if url and url not in seen and not urlsplit(url).path.lower().endswith('.svg') and not CHROME.search(urlsplit(url).path):
            seen.add(url)
            media.append({'image_url': url, 'article_url': page_url, 'kind': kind, 'alt': alt[:220]})

    for root in roots:
        for image in root.find_all('img'):
            ancestors = [image, *list(image.parents)]
            if any(node.name in {'nav', 'aside', 'header', 'footer'} or CHROME.search(
                    ' '.join([str(node.get('id', '')), *node.get('class', [])])) for node in ancestors):
                continue
            try:
                if any(0 < int(image.get(key, 0)) < 160 for key in ('width', 'height')):
                    continue
            except ValueError:
                pass
            alt = str(image.get('alt') or '')
            if CHROME.search(alt):
                continue
            parent = image.find_parent('a')
            if (parent and re.search(r'\.(?:jpe?g|png|webp)(?:\?|$)', str(parent.get('href')), re.I)
                    and not re.match(r'^/wiki/(?:File|Файл|Image|Изображение):', unquote(str(parent.get('href'))), re.I)):
                add(parent.get('href'), 'article_image_link', alt)
                continue
            if parent and parent.get('href') and not str(parent.get('href')).startswith('#'):
                linked = public_url(urljoin(page_url, parent['href']))
                file_link = linked and re.match(r'^/wiki/(?:File|Файл|Image|Изображение):', unquote(urlsplit(linked).path), re.I)
                if linked and not file_link and urlsplit(linked).path.rstrip('/') != urlsplit(page_url).path.rstrip('/'):
                    continue
            srcset = image.get('data-srcset') or image.get('srcset') or ''
            variants = []
            for variant in srcset.split(','):
                parts = variant.strip().split()
                if parts:
                    try:
                        score = float(parts[-1].rstrip('wx')) if len(parts) > 1 else 0
                    except ValueError:
                        score = 0
                    variants.append((score, parts[0]))
            if variants:
                add(max(variants)[1], 'article_srcset', alt)
                continue
            add(image.get('data-original') or image.get('data-src') or image.get('data-lazy-src')
                or image.get('src'), 'article_img', alt)
        for node in ([root] if root.has_attr('style') else []) + root.select('[style]'):
            if not CONTENT.search(' '.join(node.get('class', []))) or any(CHROME.search(
                    ' '.join([str(p.get('id', '')), *p.get('class', [])])) for p in [node, *node.parents]):
                continue
            for raw in re.findall(r'url\([\'"]?([^\)\'"]+)', node.get('style', '')):
                add(raw, 'article_gallery_background')
    # Structured Article/Place image is explicit publisher-selected main media.
    def structured(value):
        if isinstance(value, list):
            for item in value:
                structured(item)
        elif isinstance(value, dict):
            types = value.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if any(t in {'Article', 'NewsArticle', 'BlogPosting', 'TouristAttraction', 'Place', 'Museum'} for t in types):
                images = value.get('image', [])
                for item in images if isinstance(images, list) else [images]:
                    add(item.get('url') or item.get('contentUrl') if isinstance(item, dict) else item,
                        'structured_main_image')
            structured(value.get('@graph', []))
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            structured(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    # OpenGraph is accepted only if it also occurs in the article media above.
    og = soup.select_one('meta[property="og:image"]')
    if og:
        url = public_url(urljoin(page_url, og.get('content', '')))
        media.sort(key=lambda item: item['image_url'] != url)
    return title, media


@asynccontextmanager
async def article_browser(page_url: str):
    """Quiet browser with a managed disposable profile and public-only routing."""
    from playwright.async_api import async_playwright
    if not public_url(page_url):
        raise ValueError('article_media_unsafe_url')
    await resolve_public(urlsplit(page_url).hostname)
    # Chromium's disposable profile is a managed devserver artifact, rather
    # than an untracked profile in /tmp or inside the application checkout.
    artifact_cli = shutil.which('dev-artifacts') or str(Path.home() / '.local/bin/dev-artifacts')
    process = await asyncio.create_subprocess_exec(artifact_cli, 'new', 'street-story',
        'article-browser-' + uuid.uuid4().hex[:12], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _stderr = await process.communicate()
    workspace = Path(stdout.decode().strip())
    if process.returncode or not workspace.is_relative_to('/home/dev/artifacts') or not (workspace / '.artifact.json').is_file():
        raise ValueError('article_browser_artifact_unavailable')
    workspace.chmod(0o700)
    async with async_playwright() as playwright:
        executable = browser_executable(playwright.chromium.executable_path)
        context = await playwright.chromium.launch_persistent_context(str(workspace / 'profile'),
            headless=True, executable_path=executable, service_workers='block', accept_downloads=False)
        try:
            async def guard(route):
                raw = public_url(route.request.url)
                if not raw or route.request.resource_type in {'media', 'font', 'websocket'}:
                    return await route.abort()
                try:
                    await resolve_public(urlsplit(raw).hostname)
                except (ValueError, OSError):
                    return await route.abort()
                await route.continue_()
            await context.route('**/*', guard)
            page = await context.new_page()
            yield page
        finally:
            await context.close()


async def rendered_media(page, page_url):
    await page.goto(page_url, wait_until='domcontentloaded', timeout=12000)
    for _ in range(3):
        await page.evaluate('window.scrollBy(0, window.innerHeight)')
        await page.wait_for_timeout(200)
    await page.evaluate("document.querySelectorAll('img').forEach(i => { if(i.currentSrc) i.setAttribute('data-src', i.currentSrc) })")
    document = await page.content()
    if len(document.encode()) > MAX_PAGE_BYTES:
        raise ValueError('article_media_size')
    return extract_media(document, page.url)


async def browser_media(page_url: str) -> tuple[str, list[dict]]:
    async with article_browser(page_url) as page:
        return await rendered_media(page, page_url)


async def wikipedia_article_references(candidate):
    """Expand all inline/gallery images, rather than the API's lead thumbnail."""
    page_url = public_url(str(candidate.get('url') or ''))
    if not page_url or not (urlsplit(page_url).hostname or '').endswith('.wikipedia.org'):
        return []
    async with httpx.AsyncClient(timeout=8, follow_redirects=False,
            headers={'User-Agent': 'StreetStory/0.1 article-media'}) as client:
        try:
            _resolved, mime, body = await fetch_public(client, page_url, MAX_PAGE_BYTES)
            if mime not in {'text/html', 'application/xhtml+xml'}:
                return []
            _title, media = extract_media(body, page_url)
        except (ValueError, OSError, httpx.HTTPError):
            return []
    from .identity_references import canonical_reference
    return list(dict.fromkeys(url for item in media if (url := canonical_reference(item['image_url']))))


async def browser_reference(descriptor):
    """Extract only the authorized article's rendered illustration, never the page."""
    raw = descriptor['image_url']
    async with article_browser(descriptor['article_url']) as page:
        _title, media = await rendered_media(page, descriptor['article_url'])
        if raw not in {item['image_url'] for item in media}:
            raise ValueError('article_media_not_extracted')
        images = page.locator('img')
        for index in range(await images.count()):
            image = images.nth(index)
            exact = await image.evaluate("(i, url) => [i.currentSrc, i.src, i.dataset.src, i.dataset.original, i.closest('a')?.href].includes(url)", raw)
            if not exact:
                continue
            await image.scroll_into_view_if_needed(timeout=3000)
            await image.evaluate("i => i.decode()")
            if not await image.evaluate('i => i.naturalWidth >= 160 && i.naturalHeight >= 160'):
                continue
            data = await image.screenshot(type='jpeg', quality=85, timeout=5000)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise ValueError('article_media_size')
            return data
    raise ValueError('article_media_render_unavailable')


def browser_executable(default: str) -> str:
    """Reuse an installed headless Chromium; never download in a story request."""
    configured = os.environ.get('STREET_STORY_ARTICLE_BROWSER_EXECUTABLE')
    if configured:
        if not Path(configured).is_file():
            raise ValueError('article_browser_unavailable')
        return configured
    if Path(default).is_file():
        return default
    caches = [Path.home() / '.cache/ms-playwright']
    if os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
        caches.insert(0, Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']))
    installed = sorted((p for cache in caches for p in cache.glob('chromium-*/chrome-linux*/chrome')),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if installed:
        return str(installed[0])
    raise ValueError('article_browser_unavailable')


async def article_candidates(service, story, sources, excluded, *, http=None, resolver=resolve_public, browser=browser_media):
    own = http is None
    client = http or httpx.AsyncClient(timeout=8, follow_redirects=False,
        headers={'User-Agent': 'StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story) article-media'})
    candidates = []
    semaphore = asyncio.Semaphore(4)
    browser_slots = 2
    def event(name, fields):
        record_identity_event(service, story['id'], name, {**fields, 'generation': story.get('_identity_generation', 0)})
    async def read(source):
        nonlocal browser_slots
        raw = public_url(str(source.get('url') or ''))
        if not raw or any((urlsplit(raw).hostname or '').endswith(host) for host in ('wikipedia.org', 'wikimedia.org')):
            return None
        cid = 'web:' + hashlib.sha256(raw.encode()).hexdigest()[:16]
        if cid in excluded:
            return None
        async with semaphore:
            page_url, title, media = raw, '', []
            try:
                page_url, mime, body = await fetch_public(client, raw, MAX_PAGE_BYTES, resolver=resolver)
                if mime not in {'text/html', 'application/xhtml+xml'}:
                    raise ValueError('article_media_not_html')
                # BeautifulSoup honors declared HTML encoding (including CP1251).
                title, media = extract_media(body, page_url)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                event('identity_article_unavailable', {'reason': type(exc).__name__})
            if not media and browser_slots > 0:
                browser_slots -= 1
                try:
                    title, media = await asyncio.wait_for(browser(page_url), timeout=20)
                    event('identity_article_browser', {'image_count': len(media), 'headless': True})
                except Exception as exc:
                    event('identity_article_browser_unavailable', {'reason': type(exc).__name__})
            if not media:
                return None
            event('identity_article_media', {'candidate_id': cid, 'image_count': len(media)})
            return {'candidate_id': cid, 'name': title or str(source.get('title') or '')[:180],
                'url': page_url, 'source_urls': [page_url], 'reference_image_urls': [item['image_url'] for item in media],
                'article_media': media, 'multi_view': True, 'discovery': 'web_article_media'}
    try:
        unique = {str(s.get('url')): s for s in sources if isinstance(s, dict)}
        values = await asyncio.gather(*(read(source) for source in list(unique.values())[:MAX_PAGES]))
        candidates = [value for value in values if value]
    finally:
        if own:
            await client.aclose()
    event('identity_web_media_candidates', {'page_count': min(len(unique), MAX_PAGES), 'candidate_count': len(candidates)})
    return candidates


async def load_article_reference(client, candidate, raw, *, resolver=resolve_public):
    descriptor = next((item for item in candidate.get('article_media', []) if item.get('image_url') == raw), None)
    if not descriptor:
        raise ValueError('article_media_not_extracted')
    method = 'http'
    try:
        target, mime, data = await fetch_public(client, raw, MAX_DOWNLOAD_BYTES, resolver=resolver)
        if mime not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise ValueError('article_media_not_image')
        image = await asyncio.to_thread(normalize_reference, data)
    except (httpx.HTTPError, ValueError, OSError):
        budget = candidate.get('_browser_budget') or {}
        if budget.get('remaining', 0) <= 0:
            raise
        budget['remaining'] -= 1
        data = await asyncio.wait_for(browser_reference(descriptor), timeout=20)
        image = await asyncio.to_thread(normalize_reference, data)
        target, method = raw, 'article_browser_element'

    # Exclude tiny tracking pixels even if mislabelled as article illustrations.
    from PIL import Image
    from io import BytesIO
    with Image.open(BytesIO(image[1])) as decoded:
        if min(decoded.size) < 160:
            raise ValueError('article_media_too_small')
    return image, {**descriptor, 'resolved_image_url': target, 'retrieval_method': method}
