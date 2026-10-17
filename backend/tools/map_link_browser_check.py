"""E2E acceptance: open a post's ЯндексКарты/2ГИС links in a real mobile browser.

A link passes only when the opened card is the building: its text names the
expected street and house number (not just the city or a bare point). Every
check saves a screenshot as evidence. Live network, so it belongs to product
acceptance runs, not to unit CI.

Usage: python map_link_browser_check.py OUT_DIR '[{"name": "...", "url": "...",
        "street": "Комсомольская", "house": "88"}, ...]'
Prints a JSON report; exit code 1 if any link fails.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

CHROME = '/usr/bin/google-chrome'
WAIT_MS = 15000


def _norm(text: str) -> str:
    return re.sub(r'[^0-9a-zа-я]+', ' ', str(text or '').casefold().replace('ё', 'е')).strip()


def card_names_building(text: str, street: str, house: str) -> bool:
    """The card text mentions the street (any of its words) together with the house number."""
    body = ' ' + _norm(text) + ' '
    words = [w for w in _norm(street).split() if len(w) > 2 and w not in {'улица', 'проспект', 'переулок'}]
    number = _norm(house).split(' ')[0] if house else ''
    return bool(words) and all(' ' + w + ' ' in body for w in words) and bool(number) and (' ' + number in body)


async def check(out: Path, items: list[dict]) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    report = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=True,
            args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'])
        context = await browser.new_context(**p.devices['Pixel 7'], locale='ru-RU')
        for item in items:
            page = await context.new_page()
            row = {'name': item['name'], 'url': item['url']}
            try:
                await page.goto(item['url'], wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(WAIT_MS)
                shot = out / f"{item['name']}.png"
                await page.screenshot(path=str(shot), timeout=90000, animations='disabled', caret='hide')
                row['screenshot'] = str(shot)
                # Some cards (2ГИС) fill their text only after the first full render.
                text = await page.evaluate("document.body ? document.body.innerText : ''")
                row.update(final_url=page.url, card_head=re.sub(r'\.{5,}', '…', text)[:600])
                row['building_card'] = card_names_building(text, item.get('street', ''), item.get('house', ''))
            except Exception as exc:  # report, never hide
                row.update(error=repr(exc)[:300], building_card=False)
            report.append(row)
            await page.close()
        await browser.close()
    return report


def main() -> int:
    report = asyncio.run(check(Path(sys.argv[1]), json.loads(sys.argv[2])))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all(r.get('building_card') for r in report) else 1


if __name__ == '__main__':
    sys.exit(main())
