"""Publication destinations named the way the owner says them.

Owner test 19.10.2026: Mira asked for an "exact system alias" when the owner
said «тестовая группа» and «Полюбить Калининград», and the publication never
happened. The owner names a channel; the product finds its alias.
"""
from __future__ import annotations

import re
from typing import Any

from .service import ConflictError

# Display names of real channels, as the owner calls them.
CHANNEL_LABELS = {'lovekenig_tg': 'Полюбить Калининград'}
_TEST_WORDS = ('тест', 'test')


def _norm(value: Any) -> str:
    text = str(value or '').lower().replace('ё', 'е')
    return ' '.join(re.findall(r'[0-9a-zа-я]+', text))


def _choices(destinations: list[dict[str, Any]]) -> str:
    return ', '.join('«%s»' % (item.get('label') or item.get('alias')) for item in destinations)


def resolve_destination(requested: Any, destinations: list[dict[str, Any]]) -> str:
    """Alias for an exact alias, a label, or an unambiguous spoken name."""
    raw = str(requested or '').strip()
    by_alias = {str(item.get('alias') or ''): item for item in destinations if item.get('alias')}
    if raw in by_alias:
        return raw
    spoken = _norm(raw)
    if spoken:
        exact = [alias for alias, item in by_alias.items()
                 if spoken in {_norm(alias), _norm(item.get('label'))}]
        if len(exact) == 1:
            return exact[0]
        if any(word in spoken for word in _TEST_WORDS):
            tests = [alias for alias, item in by_alias.items() if item.get('test')]
            if len(tests) == 1:
                return tests[0]
        partial = [alias for alias, item in by_alias.items() if not item.get('test') and any(
            len(name) >= 5 and (name in spoken or spoken in name)
            for name in (_norm(item.get('label')), _norm(alias)) if name)]
        if len(partial) == 1:
            return partial[0]
    raise ConflictError('publish_destination_invalid',
                        f'Unknown destination «{raw[:80]}». Ask the author which one: {_choices(destinations)}; '
                        'pass the name exactly as listed.')
