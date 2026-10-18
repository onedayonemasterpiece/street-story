"""The selected house's own OpenStreetMap record, read by the host.

The research session is given map_record_url so it can see what the map says occupies the
house now. The public reader accepts only documents (HTML, text, PDF), so a JSON API answer
was refused as unsupported_content_type and the session never saw the record (story 131).
The host reads this one URL itself and returns the current tags as text with the date of the
last edit: the record describes the present, not history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx

LOG = logging.getLogger(__name__)
USER_AGENT = 'StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story) map-record'
_URL = re.compile(r'https://api\.openstreetmap\.org/api/0\.6/(node|way|relation)/(\d+)\.json')


def record_text(payload: dict[str, Any], url: str) -> str:
    element = next(iter(payload.get('elements') or []), None)
    if not isinstance(element, dict):
        raise ValueError('map_record_empty')
    tags = element.get('tags') or {}
    edited = str(element.get('timestamp') or '')[:10]
    lines = [f'Источник: запись OpenStreetMap этого дома ({element.get("type")} {element.get("id")}), '
             f'версия {element.get("version")}, последняя правка {edited or "без даты"}. '
             'Теги описывают то, что карта отмечает в доме сейчас, а не историю.', '']
    lines += [f'{key} = {value}' for key, value in sorted(tags.items())] or ['(тегов нет)']
    return '\n'.join(lines)


async def capture(url: str) -> dict[str, Any]:
    if not _URL.fullmatch(str(url or '')):
        raise ValueError('map_record_url_unknown')
    async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                 headers={'User-Agent': USER_AGENT}) as client:
        response = await client.get(url)
    if response.status_code != 200:
        LOG.warning('street_story_map_record outcome=http_error status=%s', response.status_code)
        raise ValueError(f'map_record_http_{response.status_code}')
    body = record_text(json.loads(response.content), url)
    LOG.info('street_story_map_record outcome=read chars=%s', len(body))
    return {'source_url': url, 'final_url': url, 'title': 'Запись OpenStreetMap этого дома',
            'normalized_text': body, 'http_status': 200,
            'response_sha256': hashlib.sha256(body.encode()).hexdigest()}
