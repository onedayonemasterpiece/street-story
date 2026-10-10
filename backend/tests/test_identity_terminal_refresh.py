import json

from street_story.identity_telemetry import record_identity_event
from street_story.research_budget import expire_queued
from test_identity_lifecycle import create, make_service


def test_deadline_ended_photo_is_not_resurrected_by_retained_outage_or_foreground_refresh(tmp_path):
    svc, _ = make_service(tmp_path)
    now = [1000]
    svc.store.now = lambda: now[0]
    story = create(svc)
    svc.ensure_identity(story['id'])
    record_identity_event(svc, story['id'], 'identity_started', {'generation': 0})
    record_identity_event(svc, story['id'], 'identity_osm_unavailable', {'generation': 0})
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['visual_identity'] = {'status': 'uncertain', 'generation': 0, 'candidates': []}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    now[0] += svc.settings.identity_timeout_seconds + 1
    with svc.store.tx() as db:
        assert expire_queued(svc, db) == 1
        assert svc._recover_transient_identity(db) == 0
    assert svc.ensure_identity(story['id'])['state'] == 'needs_review'
    svc.recover_jobs()
    assert svc.ensure_identity(story['id'])['state'] == 'needs_review'
    with svc.store.connection() as db:
        jobs = list(db.execute('SELECT state FROM jobs WHERE story_id=?', (story['id'],)))
    assert len(jobs) == 1 and jobs[0]['state'] == 'done'
    # A new owner-authorized generation can start a new bounded wave.
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['identity_generation'] = 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    assert svc.ensure_identity(story['id'])['state'] == 'identifying'
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE story_id=? AND state='ready'", (story['id'],)).fetchone()[0] == 1
