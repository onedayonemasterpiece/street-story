"""Compile literal named HTTPS links into the existing VibePublish rich-text input."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_LINK = re.compile(r'\[([^\]\n]+)\]\((https://[^\s)]+)\)')


def visible_text(text: str) -> str:
    """Text as the reader sees it: named links show only their labels."""
    return _LINK.sub(lambda match: match[1], text)


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

def publication_plain_content(text: str) -> dict:
    """Explicit VK rendering: preserve link labels AND URLs, without Telegram entities."""
    rich = publication_content(text)
    if "paragraphs" not in rich:
        return {"text": text, "format": "plain"}
    runs = rich["paragraphs"][0]
    return {"text": "".join(
        run["text"] if run["kind"] == "text"
        else f'{run["label"]} ({run["url"]})'
        for run in runs
    ), "format": "plain"}
