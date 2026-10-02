"""Bounded authenticated Live diagnostic readback, no model/publication writes."""
import argparse
from collections import Counter
import json
import re

import httpx
import devcoveer_live_product_smoke as smoke


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--story', required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'story_[a-zA-Z0-9]{8,64}', args.story) or not re.fullmatch(r'live_[a-zA-Z0-9]{8,64}', args.session):
        raise SystemExit('invalid diagnostic identifiers')
    installer = smoke.load_installer(smoke.repo_root())
    installer.require_mode(installer.DEVICE_TOKEN_FILE, 0o600)
    credential = installer.DEVICE_TOKEN_FILE.read_text(encoding='utf-8').strip()
    headers = {'Authorization': 'Bearer ' + credential}
    with httpx.Client(base_url=smoke.BASE_URL, headers=headers, timeout=20, follow_redirects=False) as client:
        story = smoke._json(client.get('/v1/stories/' + args.story), 'story')
        events = []
        cursor = 0
        for _ in range(6):
            page = smoke._json(client.get(f'/v1/stories/{args.story}/live-sessions/{args.session}/events', params={'after': cursor}), 'events')
            events.extend(page.get('events', []))
            cursor = int(page.get('cursor') or cursor)
            if not page.get('has_more'):
                break
    fields = {'seq', 'type', 'stage', 'status', 'code', 'duration_ms', 'name', 'audio_chunks', 'activity_end_sent_at', 'text_sent_at', 'max_stdin_delay_ms', 'max_ws_send_ms'}
    metadata = [{key: value for key, value in event.items() if key in fields} for event in events]
    calls = [{'seq': event.get('seq'), 'tools': [call.get('name') for call in event.get('calls', []) if isinstance(call, dict)]}
             for event in events if event.get('type') == 'tool_call']
    print(json.dumps({'read_only': True, 'story_id': args.story, 'session_id': args.session,
        'state': story.get('state'), 'place_name': story.get('place_name'), 'source_count': story.get('source_count'),
        'error_code': (story.get('error') or {}).get('code'), 'events': metadata[-60:], 'tool_calls': calls,
        'event_counts': dict(Counter(event.get('type') for event in events)), 'cursor': cursor,
        'secrets_disclosed': False, 'transcripts_disclosed': False}, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
