"""The house's own map card pages, read by the host in a real mobile browser.

2ГИС renders organizations in the building and visitor reviews only in a browser, so the
research session cannot read them over plain HTTP. When it asks for one of these URLs
through webfetch, the host opens the page in mobile Chrome (Playwright), scrolls every
review in, and returns the text as an owned source. Reviews are people's opinions: the
source says so in its first line, and facts resting on it carry from_visitor_reviews.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .map_links import building_links

LOG = logging.getLogger(__name__)
CHROME = '/usr/bin/google-chrome'
REVIEWS = 'reviews'
ORGANIZATIONS = 'organizations'
HEADERS = {
    ORGANIZATIONS: 'Источник: карточка дома в 2ГИС, вкладка «В здании» — организации, которые сейчас отмечены '
                   'в этом здании (снято {date}).',
    REVIEWS: 'Источник: отзывы посетителей в 2ГИС — мнения и впечатления людей, а не проверенная история; '
             'факты из них — то, что пишут посетители (снято {date}).',
}
TABS = {ORGANIZATIONS: 'inside', REVIEWS: 'reviews'}
_DATE = re.compile(r'\d{1,2} (?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря) \d{4}')
_SCROLL = """() => {
  const els = Array.from(document.querySelectorAll('*')).filter(e => {
    const s = getComputedStyle(e); return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 50; });
  els.sort((a, b) => b.scrollHeight - a.scrollHeight);
  for (const e of els.slice(0, 3)) e.scrollTop = e.scrollHeight;
  window.scrollTo(0, document.body.scrollHeight); }"""
_LOCK = asyncio.Lock()


def page_urls(research: dict[str, Any]) -> dict[str, str]:
    """Stable URLs the research session reads through webfetch; the fragment names the tab."""
    base = (building_links(research) or {}).get('2ГИС')
    return {kind: f'{base}#{kind}' for kind in TABS} if base else {}


def kind_of(url: str) -> str | None:
    fragment = urlsplit(str(url or '')).fragment
    return fragment if fragment in TABS else None


def from_visitor_reviews(source_url: str) -> bool:
    return kind_of(source_url) == REVIEWS


def _tab_url(resolved: str, tab: str) -> str | None:
    match = re.match(r'https://2gis\.ru/([^/]+)/(firm|geo)/(\d+)', resolved)
    return f'https://2gis.ru/{match.group(1)}/{match.group(2)}/{match.group(3)}/tab/{tab}' if match else None


async def capture(url: str, *, budget_seconds: float = 150) -> dict[str, Any]:
    """Read one map page in mobile Chrome; reviews are scrolled until no more load."""
    kind = kind_of(url)
    if kind is None:
        raise ValueError('map_page_kind_unknown')
    from playwright.async_api import async_playwright
    started = time.monotonic()
    async with _LOCK, async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
            args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
        try:
            context = await browser.new_context(**playwright.devices['Pixel 7'], locale='ru-RU')
            page = await context.new_page()
            await page.goto(url.split('#', 1)[0], wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(8000)
            tab_url = _tab_url(page.url, TABS[kind])
            if tab_url is None:
                raise ValueError('map_page_card_not_found')
            await page.goto(tab_url, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(8000)
            # Scroll until nothing new loads: every review, every organization in the building.
            loaded, size, stable = -1, -1, 0
            while stable < 4 and time.monotonic() - started < budget_seconds:
                await page.evaluate(_SCROLL)
                await page.wait_for_timeout(1500)
                current = await page.evaluate('document.body.innerText')
                count = len(_DATE.findall(current))
                stable = stable + 1 if (count, len(current)) == (loaded, size) else 0
                loaded, size = count, len(current)
            text = re.sub(r'(…\s*)+|(\.{5,}\s*)+', '', await page.evaluate('document.body.innerText'))
        finally:
            await browser.close()
    header = HEADERS[kind].format(date=datetime.now(timezone.utc).date().isoformat())
    body = header + '\n\n' + text.strip()
    LOG.info('street_story_map_page kind=%s chars=%s reviews=%s seconds=%s', kind, len(body),
             max(loaded, 0) if kind == REVIEWS else None, round(time.monotonic() - started))
    return {'source_url': url, 'final_url': tab_url, 'title': header.split(' — ')[0].removeprefix('Источник: '),
            'normalized_text': body, 'http_status': 200,
            'response_sha256': hashlib.sha256(body.encode()).hexdigest()}
