"""Story-scoped identity/import telemetry, available before a Live session exists."""
from __future__ import annotations
import json
import logging
import re
from typing import Any
from .identity_progress import advance

LOG = logging.getLogger('uvicorn.error')
CLIENT_FIELDS = frozenset({
    'app_version', 'source_sha', 'event_id', 'status', 'read_mode', 'mime_type',
    'media_location_granted', 'original_requested', 'original_opened', 'fallback_reason',
    'gps_present', 'gps_lat_tag', 'gps_lon_tag', 'exif_orientation', 'photo_sha256',
    'photo_bytes', 'elapsed_ms', 'provider_kind', 'error_type',
    'active', 'connecting', 'generation', 'reason',
})


def client_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value[:160] if isinstance(value, str) else value
            for key, value in fields.items() if key in CLIENT_FIELDS
            and (value is None or isinstance(value, (str, bool, int, float)))}


def record_identity_event(service, story_id: str, event: str, fields: dict[str, Any] | None = None, *, source='identity') -> None:
    if not re.fullmatch(r'story_[A-Za-z0-9]{8,80}', story_id) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', event):
        return
    # Reuse the existing table/retention. Empty session_id means pre-Live work,
    # not a fabricated Live session. A telemetry failure never breaks a photo job.
    try:
        from .live import ensure_live_schema
        if not getattr(service, '_identity_telemetry_schema_ready', False):
            ensure_live_schema(service)
            service._identity_telemetry_schema_ready = True
        payload = json.dumps(fields or {}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) > 24_000:
            payload = json.dumps({'truncated': True})
        with service.store.tx() as db:
            if not db.execute('SELECT 1 FROM stories WHERE id=?', (story_id,)).fetchone():
                return
            event_id = (fields or {}).get('event_id')
            if source == 'android_import' and event_id and db.execute(
                'SELECT 1 FROM live_diagnostics WHERE story_id=? AND source=? AND event_type=? AND payload_json=? LIMIT 1',
                (story_id, source, event, payload),
            ).fetchone():
                return
            if source == 'identity':
                row = db.execute('SELECT research_json FROM stories WHERE id=?', (story_id,)).fetchone()
                research = json.loads(row['research_json'] or '{}')
                field_generation = (fields or {}).get('generation', research.get('identity_generation', 0))
                if field_generation == research.get('identity_generation', 0):
                    research['identity_progress'] = advance(research.get('identity_progress') or {}, event, fields or {}, service.store.now())
                    db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research, ensure_ascii=False), story_id))
            db.execute('INSERT INTO live_diagnostics(story_id,session_id,source,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)',
                       (story_id, '', source, event, payload, service.store.now()))
            db.execute('DELETE FROM live_diagnostics WHERE created_at<?', (service.store.now() - 7 * 86400,))
        LOG.info('street_story_identity %s', json.dumps({'story_id': story_id, 'event': event, **(fields or {})}, ensure_ascii=False))
    except Exception as exc:
        LOG.warning('street_story_identity_telemetry_failed story_id=%s type=%s', story_id, type(exc).__name__)
