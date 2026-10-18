"""Read one owner's retained diagnostic trace without reviving a Live session.

Uses SQLite read-only mode, no model/provider calls, no audio/transcripts or GPS.
The active-session events endpoint cannot retrieve a stopped session (409).
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import re
import sqlite3

from devcoveer_story_diag import load_installer

SAFE_TEXT = frozenset({
    'code', 'reason', 'status', 'state', 'stage', 'type', 'activity', 'policy',
    'app_version', 'source_sha', 'protocol', 'transport', 'mode', 'name',
    'error_code', 'close_reason', 'error_type', 'component', 'format',
})
PRIVATE_KEY = re.compile(r'token|ticket|secret|credential|authorization|cookie|audio_data|pcm|latitude|longitude|transcript|text', re.I)


def safe_metrics(value, key='', depth=0):
    if depth > 4 or PRIVATE_KEY.search(key):
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:240] if key in SAFE_TEXT else None
    if isinstance(value, dict):
        result = {}
        for child_key, child in list(value.items())[:80]:
            cleaned = safe_metrics(child, str(child_key), depth + 1)
            if cleaned is not None:
                result[str(child_key)] = cleaned
        return result
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--story-id', required=True)
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--limit', type=int, default=150)
    parser.add_argument('--identity', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'story_[A-Za-z0-9]{8,80}', args.story_id):
        parser.error('Invalid story identifier')
    if args.after < 0 or not 1 <= args.limit <= 500:
        parser.error('Invalid pagination')
    database = load_installer().DATA_ROOT / 'street-story.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        story = db.execute('SELECT state,error_code,research_json FROM stories WHERE id=?', (args.story_id,)).fetchone()
        if not story:
            parser.error('Story not found')
        rows = db.execute('SELECT id,session_id,source,event_type,payload_json,created_at FROM live_diagnostics WHERE story_id=? AND id>? ORDER BY id LIMIT ?',
                          (args.story_id, args.after, args.limit + 1)).fetchall()
    events = [{'id': row['id'], 'session_id': row['session_id'], 'source': row['source'],
               'event': row['event_type'], 'at': row['created_at'],
               'fields': safe_metrics(json.loads(row['payload_json']))} for row in rows[:args.limit]]
    result = {'read_only': True, 'story_id': args.story_id, 'state': story['state'],
              'error_code': story['error_code'], 'has_more': len(rows) > args.limit,
              'next_after': events[-1]['id'] if events else args.after,
              'event_counts': dict(Counter(x['event'] for x in events)), 'events': events}
    if args.identity:
        research = json.loads(story['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        result['identity'] = {k: identity.get(k) for k in ('status', 'candidate_id', 'candidate_name', 'candidate_url', 'visual_reference_verified', 'confidence')}
        result['candidates'] = [{k: item.get(k) for k in ('candidate_id', 'name', 'url', 'reference_image_urls')} for item in identity.get('candidates', [])]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
