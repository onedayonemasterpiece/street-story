"""Terminal/restart scheduling retains joined requests without concurrent jobs."""
import json

import pytest

from test_mvp_research import service
from street_story.providers import PermanentProviderError, RetryableProviderError
from street_story.service import MAX_JOB_ATTEMPTS, ConflictError, InvalidStateError


def pending_request(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=?,draft_text=? WHERE id=?', (json.dumps({
            'visual_identity': {'status': 'match', 'candidate_id': 'wiki:1', 'candidate_name': 'Дом Советов'},
            'publication_concept': 'Owner concept',
        }), 'Owner draft', sid))
    svc.mutate_facts(sid, 'first', {'action': 'research', 'coverage_goal': 'Architecture'})
    svc.mutate_facts(sid, 'more', {'action': 'research', 'coverage_goal': 'History'})
    with svc.store.connection() as db:
        jid = db.execute('SELECT id FROM jobs').fetchone()[0]
    return svc, sid, jid


def assert_followup(svc, sid, jid, *, preceding_state='failed'):
    with svc.store.connection() as db:
        preceding = db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()
        assert preceding['state'] == preceding_state
        active = list(db.execute("SELECT * FROM jobs WHERE state IN ('ready','retry','running')"))
        assert len(active) == 1 and active[0]['id'] != jid and active[0]['state'] == 'ready'
        assert json.loads(active[0]['payload_json'])['coverage_goal'] == 'History'
        story = db.execute('SELECT research_json,draft_text FROM stories WHERE id=?', (sid,)).fetchone()
        research = json.loads(story['research_json'])
        assert 'pending_fact_request' not in research
        assert research['publication_concept'] == 'Owner concept' and story['draft_text'] == 'Owner draft'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,last_attempt', [
    (PermanentProviderError('controlled permanent'), False),
    (ConflictError('controlled_conflict', 'controlled conflict'), False),
    (InvalidStateError('controlled_invalid', 'controlled invalid state'), False),
])
async def test_terminal_failure_schedules_joined_request(failure, last_attempt, tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    if last_attempt:
        with svc.store.tx() as db:
            db.execute('UPDATE jobs SET attempts=? WHERE id=?', (MAX_JOB_ATTEMPTS - 1, jid))
        svc.store.checkpoint_put(jid, 'worker_non_wait_failures', {'count': MAX_JOB_ATTEMPTS - 1})

    async def fail(job):
        raise failure

    svc._run_research = fail
    assert await svc.run_once()
    assert_followup(svc, sid, jid)


@pytest.mark.asyncio
async def test_successful_terminal_job_schedules_joined_request_without_mvp_hook(tmp_path):
    svc, sid, jid = pending_request(tmp_path)

    async def done(job):
        return None

    svc._run_research = done
    assert await svc.run_once()
    assert_followup(svc, sid, jid, preceding_state='done')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RetryableProviderError('controlled temporary'), RuntimeError('controlled temporary worker')])
async def test_nonterminal_retry_keeps_joined_request_without_another_job(tmp_path, failure):
    svc, sid, jid = pending_request(tmp_path)

    async def fail(job):
        raise failure

    svc._run_research = fail
    assert await svc.run_once()
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'retry'
        assert 'pending_fact_request' in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])


@pytest.mark.parametrize('terminal_state', ['done', 'failed'])
def test_restart_recovers_orphan_joined_request_once(tmp_path, terminal_state):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET state=? WHERE id=?', (terminal_state, jid))
    assert svc.recover_jobs() == 1
    assert_followup(svc, sid, jid, preceding_state=terminal_state)
    assert svc.recover_jobs() == 0


def test_restart_after_repeated_worker_failures_keeps_original_and_joined_request(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=?,lease_until=0,last_error='worker_failure:RuntimeError' WHERE id=?", (MAX_JOB_ATTEMPTS, jid))
    svc.store.checkpoint_put(jid, 'worker_non_wait_failures', {'count': MAX_JOB_ATTEMPTS})
    assert svc.recover_jobs() == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'retry'
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert 'pending_fact_request' in json.loads(svc._story_row(db, sid)['research_json'])


@pytest.mark.asyncio
async def test_provider_wait_after_old_attempt_ceiling_keeps_authoritative_due_time(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET attempts=? WHERE id=?', (MAX_JOB_ATTEMPTS + 5, jid))
        original_budget = json.loads(svc._story_row(db, sid)['research_json'])['research_budget']
    due = svc.store.now() + 900
    async def wait(job):
        raise RetryableProviderError('provider_quota_waiting', retry_at=due)
    svc._run_research = wait
    assert await svc.run_once()
    with svc.store.connection() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        assert job['state'] == 'retry' and job['attempts'] > MAX_JOB_ATTEMPTS
        assert job['available_at'] == due
        research = json.loads(svc._story_row(db, sid)['research_json'])
        assert research['research_budget'] == original_budget
        assert 'automatic_research_outcome' not in research
        assert 'pending_fact_request' in research
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    assert svc.recover_jobs() == 0
