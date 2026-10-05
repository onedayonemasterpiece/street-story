"""New-job budgets must not hide topics behind skipped or already queued rows."""
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story.native_vision import MODEL, TRANSPORT, VERIFICATION_KEY, NativeVisionProvider
from street_story.research_adapter import ProductResearchAdapter
from test_identity_lifecycle import create, make_service


def add_topic(service, name, purpose, blocked=None):
    story = create(service, client=name)
    identity = {'status': 'uncertain' if purpose == 'identity' else 'match',
        'candidate_id': 'wiki:1', 'candidates': [{'candidate_id': 'wiki:1', 'name': 'Test object'}]}
    research = {'identity_generation': 0, 'visual_identity': identity, 'publication_concept': 'Owner concept'}
    if blocked == 'empty':
        identity['candidates'] = []
    elif blocked == 'match':
        identity['status'] = 'match'
    elif blocked == 'unconfirmed':
        identity['status'] = 'uncertain'
    elif blocked == 'owner_input':
        research['input_revision'] = 'owner-input'
    elif blocked == 'automatic':
        research['automatic_fact_request'] = {'photo_sha256': story['photo_sha256'], 'identity_generation': 0}
    elif blocked == 'stopped':
        research['research_controls'] = {purpose: {'stopped': True,
            'photo_sha256': story['photo_sha256'], 'identity_generation': 0}}
    with service.store.tx() as db:
        db.execute('UPDATE stories SET state=?,research_json=?,draft_text=? WHERE id=?',
            ('needs_review' if purpose == 'identity' else 'identity_ready', json.dumps(research), 'Owner draft', story['id']))
        if blocked == 'active':
            service._enqueue_job(db, story['id'], 'research', 'owner-research:' + name, {})
    return story['id']


def schedule(service, purpose):
    if purpose == 'identity':
        service._schedule_identity_visual()
    else:
        service._schedule_confirmed_facts()


def scheduled_jobs(service, purpose):
    prefix = 'identity-visual:%' if purpose == 'identity' else 'automatic-facts:%'
    with service.store.connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM jobs WHERE semantic_key LIKE ?', (prefix,))]


@pytest.mark.parametrize('purpose', ['identity', 'facts'])
def test_later_eligible_topic_survives_twenty_filtered_topics(tmp_path, purpose, caplog):
    service, _ = make_service(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True, facts_available=True)
    reasons = ['empty', 'match', 'stopped'] if purpose == 'identity' else [
        'unconfirmed', 'owner_input', 'automatic', 'stopped', 'active']
    skipped = [add_topic(service, f'skipped-{index}', purpose, reasons[index % len(reasons)])
        for index in range(20)]
    target = add_topic(service, 'late-target', purpose)
    caplog.set_level(logging.INFO, logger='street_story.service')
    schedule(service, purpose)
    schedule(service, purpose)
    assert [job['story_id'] for job in scheduled_jobs(service, purpose)] == [target]
    _, research = service._identity_snapshot(target)
    assert research['publication_concept'] == 'Owner concept'
    assert service.story(target)['draft_text'] == 'Owner draft'
    assert all(service.story(sid)['state'] == ('needs_review' if purpose == 'identity' else 'identity_ready')
        for sid in skipped)
    logs = [json.loads(record.getMessage().split('research_scheduler ', 1)[1])
        for record in caplog.records if record.getMessage().startswith('research_scheduler ')]
    assert logs == [{'component': 'research_scheduler',
        'stage': 'identity_visual' if purpose == 'identity' else 'confirmed_facts', 'scheduled': 1}]


@pytest.mark.parametrize('purpose', ['identity', 'facts'])
def test_twenty_new_job_budget_continues_over_later_passes(tmp_path, purpose):
    service, _ = make_service(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True, facts_available=True)
    topics = {add_topic(service, f'eligible-{index}', purpose) for index in range(45)}
    schedule(service, purpose)
    first = scheduled_jobs(service, purpose)
    assert len(first) == 20
    with service.store.tx() as db:
        # Completed jobs must remain idempotent and must not consume the next budget.
        db.execute("UPDATE jobs SET state='done',attempts=3 WHERE id=?", (first[0]['id'],))
    schedule(service, purpose)
    assert len(scheduled_jobs(service, purpose)) == 40
    schedule(service, purpose)
    assert {job['story_id'] for job in scheduled_jobs(service, purpose)} == topics
    schedule(service, purpose)
    final = scheduled_jobs(service, purpose)
    assert len(final) == 45
    original = next(job for job in final if job['id'] == first[0]['id'])
    assert original['state'] == 'done' and original['attempts'] == 3


def test_qualified_native_reserve_admits_scheduler_without_google_or_opencode(tmp_path):
    service, _ = make_service(tmp_path)
    service.settings = replace(service.settings, native_vision_reserve=True)
    # Construct only the local availability views; no provider client or inference.
    native = NativeVisionProvider.__new__(NativeVisionProvider)
    native.service = service
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    adapter.primary_vision = SimpleNamespace(available=False)
    adapter.native_vision = native
    service.providers.research = adapter
    target = add_topic(service, 'native-reserve-topic', 'identity')
    assert adapter.vision_available is False
    service._schedule_identity_visual()
    assert scheduled_jobs(service, 'identity') == []
    service.store.cache_put(VERIFICATION_KEY, {'model': MODEL, 'transport': TRANSPORT,
        'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}}, 600)
    assert adapter.vision_available is True
    service._schedule_identity_visual()
    assert [job['story_id'] for job in scheduled_jobs(service, 'identity')] == [target]


@pytest.mark.parametrize(('kind', 'semantic', 'payload'), [
    ('identity', 'identity:active', {}),
    ('identity_visual', 'identity-visual:active', {'queue_priority': 'interactive'}),
    ('research', 'research-explicit:active', {}),
    ('research', 'research:owner-voice', {}),
    ('refinement', 'refinement:active', {}),
    ('visual', 'visual:active', {}),
    ('publish', 'publish:active', {}),
])
def test_owner_work_precedes_older_background_without_reordering_background_fifo(
        tmp_path, kind, semantic, payload):
    service, _ = make_service(tmp_path)
    target = add_topic(service, 'priority-target', 'identity')
    with service.store.tx() as db:
        # Unmarked rows represent the already deployed scheduler's legacy jobs.
        first = service._enqueue_job(db, target, 'identity_visual', 'identity-visual:old', {})
        second = service._enqueue_job(db, target, 'research', 'automatic-facts:old', {})
        active = service._enqueue_job(db, target, kind, semantic, payload)
        now = service.store.now()
        for offset, ident in enumerate([first, second, active]):
            db.execute('UPDATE jobs SET created_at=? WHERE id=?', (now - 30 + offset, ident))
    assert service._claim()['id'] == active
    assert service._claim()['id'] == first
    assert service._claim()['id'] == second


def test_priority_preserves_due_retry_and_active_lease_filters_and_owner_fifo(tmp_path):
    service, _ = make_service(tmp_path)
    target = add_topic(service, 'due-target', 'identity')
    with service.store.tx() as db:
        bulk = service._enqueue_job(db, target, 'identity_visual', 'identity-visual:bulk', {})
        future = service._enqueue_job(db, target, 'identity', 'identity:future', {})
        live = service._enqueue_job(db, target, 'publish', 'publish:live', {})
        retry = service._enqueue_job(db, target, 'research', 'research-explicit:retry', {})
        newer = service._enqueue_job(db, target, 'refinement', 'refinement:newer', {})
        now = service.store.now()
        db.execute('UPDATE jobs SET available_at=? WHERE id=?', (now + 60, future))
        db.execute("UPDATE jobs SET state='running',lease_until=? WHERE id=?", (now + 60, live))
        db.execute("UPDATE jobs SET state='retry',available_at=?,created_at=? WHERE id=?", (now + 60, now - 20, retry))
        db.execute('UPDATE jobs SET available_at=? WHERE id=?', (now + 60, newer))
    assert service._claim()['id'] == bulk
    assert service._claim() is None
    with service.store.tx() as db:
        db.execute('UPDATE jobs SET available_at=? WHERE id IN (?,?)', (service.store.now() - 1, retry, newer))
    assert service._claim()['id'] == retry
    assert service._claim()['id'] == newer


def test_active_initial_identity_passes_backfill_budget_and_keeps_priority_in_visual_queue(tmp_path):
    service, _ = make_service(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True)
    backlog = [add_topic(service, f'backfill-{index}', 'identity') for index in range(45)]
    target = add_topic(service, 'foreground-photo', 'identity')
    service.ensure_identity(target)
    with service.store.tx() as db:
        initial = db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity'", (target,)).fetchone()
        assert json.loads(initial['payload_json'])['queue_priority'] == 'interactive'
        # Simulate discovery completion without starting any provider work.
        db.execute("UPDATE jobs SET state='done' WHERE id=?", (initial['id'],))
    service._schedule_identity_visual()
    jobs = scheduled_jobs(service, 'identity')
    assert len(jobs) == 20
    continuation = next(job for job in jobs if job['story_id'] == target)
    assert json.loads(continuation['payload_json'])['queue_priority'] == 'interactive'
    assert all(json.loads(job['payload_json'])['queue_priority'] == 'background'
        for job in jobs if job['story_id'] in backlog)
    assert service._claim()['id'] == continuation['id']
    service._schedule_identity_visual()
    assert len(scheduled_jobs(service, 'identity')) == 40
    service._schedule_identity_visual()
    assert len(scheduled_jobs(service, 'identity')) == 46
