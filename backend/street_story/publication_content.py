"""Compile literal named HTTPS links into the existing VibePublish rich-text input."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_LINK = re.compile(r'\[([^\]\n]+)\]\((https://[^\s)]+)\)')


def publication_content(text: str) -> dict:
    runs = []
    cursor = 0
    for match in _LINK.finditer(text):
        try:
            url = urlsplit(match[2])
        except ValueError:
            continue
        if not url.hostname or url.username or url.password:
            continue
        if match.start() > cursor:
            runs.append({'kind': 'text', 'text': text[cursor:match.start()]})
        runs.append({'kind': 'link', 'label': match[1], 'url': match[2]})
        cursor = match.end()
    if not runs:
        return {'text': text, 'format': 'plain'}
    if cursor < len(text):
        runs.append({'kind': 'text', 'text': text[cursor:]})
    return {'paragraphs': [runs]}
