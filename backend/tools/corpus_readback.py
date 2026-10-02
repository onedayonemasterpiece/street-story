"""Read private identity-corpus results without model calls or publication."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sqlite3
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--messages', default='')
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--events', action='store_true')
    args = parser.parse_args()
    root = args.corpus.resolve()
    if not root.is_relative_to('/home/dev/artifacts') or not (root / '.artifact.json').is_file():
        parser.error('Expected a managed private corpus')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.run_name):
        parser.error('Invalid run name')
    if args.messages and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', args.messages):
        parser.error('Invalid message selection')
    results = json.loads((root / args.run_name / 'results.json').read_text())
    selected = {int(x) for x in args.messages.split(',')} if args.messages else None
    output = []
    for case in results:
        if selected and case['message_id'] not in selected:
            continue
        identity = case.get('identity') or {}
        item = {k: case.get(k) for k in ('message_id', 'duration_ms', 'gps_present', 'error_type', 'error')}
        item.update({k: identity.get(k) for k in ('status', 'candidate_id', 'candidate_name', 'candidate_url', 'confidence')})
        item['references'] = len(identity.get('reference_evidence', []))
        if args.details:
            item.update({k: identity.get(k) for k in ('observations', 'alternative_candidate_ids', 'reference_evidence')})
            item['candidates'] = [{k: c.get(k) for k in ('candidate_id', 'name', 'url', 'reference_image_urls')} for c in identity.get('candidates', [])]
        if args.events and case.get('story_id'):
            database = root / args.run_name / 'data/street-story.sqlite3'
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                rows = db.execute('SELECT event_type,payload_json FROM live_diagnostics WHERE story_id=? AND source=? ORDER BY id LIMIT 160', (case['story_id'], 'identity')).fetchall()
            keys = {'status', 'candidate_id', 'candidate_ids', 'reason', 'error_type', 'reference_ids_sent', 'confidence', 'source', 'bytes', 'cache_hit', 'wiki_queries', 'commons_query'}
            item['events'] = [{'event': name, 'fields': {k: v for k, v in json.loads(payload).items() if k in keys}} for name, payload in rows]
        output.append(item)
    print(json.dumps({'run': args.run_name, 'cases_recorded': len(results),
        'accepted': sum((case.get('identity') or {}).get('status') == 'match' for case in results),
        'median_ms': statistics.median(case['duration_ms'] for case in results) if results else None,
        'results': output}, ensure_ascii=False))


if __name__ == '__main__':
    main()
