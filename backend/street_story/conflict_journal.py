"""Read the existing conflict ledger and each side's own retained sources."""
from __future__ import annotations

import json


def conflict_journal(service, story_id: str, cursor: int | None = None):
    with service.store.connection() as db:
        story = service._story_row(db, story_id)
        research = json.loads(story['research_json'] or '{}')
        rows = list(db.execute(
            'SELECT rowid AS cursor_value,* FROM fact_conflicts WHERE story_id=? '
            'AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT 21',
            (story_id, cursor, cursor)))
        page = rows[:20]
        items = []
        for row in page:
            sides = {}
            for side in ('left', 'right'):
                fact_id = row[side + '_fact_id']
                fact = db.execute(
                    'SELECT f.sources_json,a.eligibility,a.revision_digest '
                    'FROM facts f LEFT JOIN fact_assertions a '
                    'ON a.story_id=f.story_id AND a.assertion_id=f.fact_id '
                    'WHERE f.story_id=? AND f.fact_id=?', (story_id, fact_id)).fetchone()
                sides[side] = {
                    'fact_id': fact_id, 'text': row[side + '_text'],
                    'recorded_revision_digest': row[side + '_revision_digest'],
                    'current_revision_digest': fact['revision_digest'] if fact else None,
                    'eligibility': fact['eligibility'] if fact else 'withheld',
                    'sources': json.loads(fact['sources_json'] or '[]') if fact else [],
                }
            events = [dict(event) for event in db.execute(
                'SELECT event_id,resolution,reason,arbitrated_by,state,stale_reason,created_at,stale_at '
                'FROM fact_arbitration_events WHERE story_id=? AND conflict_id=? ORDER BY created_at,event_id',
                (story_id, row['conflict_id']))]
            items.append({
                'conflict_id': row['conflict_id'], 'poi_key': row['poi_key'], **sides,
                'relation': row['relation'], 'reason': row['detector_rationale'],
                'resolution': row['final_resolution'] or 'unresolved',
                'resolution_reason': row['arbitration_reason'], 'resolved_by': row['arbitrated_by'],
                'first_seen_at': row['first_seen_at'], 'last_seen_at': row['last_seen_at'],
                # Historical decisions retain their own version and stale state;
                # viewing them never makes a withheld assertion selectable.
                'arbitration_events': events,
            })
        return {
            'story_id': story_id, 'photo_sha256': story['photo_sha256'],
            'identity_generation': int(research.get('identity_generation') or 0),
            'items': items, 'has_more': len(rows) > len(page),
            'next_cursor': page[-1]['cursor_value'] if len(rows) > len(page) else None,
            'read_only': True,
        }
