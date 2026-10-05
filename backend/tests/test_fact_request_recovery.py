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
    (RuntimeError('controlled worker exhaustion'), True),
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


def test_restart_worker_failure_exhaustion_schedules_followup_after_terminalizing_original(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=?,lease_until=0,last_error='worker_failure:RuntimeError' WHERE id=?", (MAX_JOB_ATTEMPTS, jid))
    svc.store.checkpoint_put(jid, 'worker_non_wait_failures', {'count': MAX_JOB_ATTEMPTS})
    assert svc.recover_jobs() == 1
    assert_followup(svc, sid, jid)


@pytest.mark.asyncio
async def test_provider_waiting_beyond_old_retry_limit_survives_restart_without_losing_joined_work(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET attempts=? WHERE id=?', (MAX_JOB_ATTEMPTS + 5, jid))
    async def wait(job):
        raise RetryableProviderError('provider_quota_waiting', retry_at=svc.store.now() + 900)
    svc._run_research = wait
    assert await svc.run_once()
    assert svc.recover_jobs() == 0
    with svc.store.connection() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        assert job['state'] == 'retry' and job['attempts'] > MAX_JOB_ATTEMPTS
        assert 'pending_fact_request' in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1


def test_restart_unexpired_and_recovered_attempts_keep_pending_request(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1,lease_until=? WHERE id=?", (svc.store.now() + 1000, jid))
    assert svc.recover_jobs() == 0
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET lease_until=0 WHERE id=?', (jid,))
    assert svc.recover_jobs() == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'retry'
        assert 'pending_fact_request' in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['stop', 'generation', 'photo'])
async def test_terminal_failure_discards_joined_request_after_stop_or_binding_change(tmp_path, guard):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        if guard == 'stop':
            research['fact_research_cancelled'] = True
        elif guard == 'generation':
            research['identity_generation'] = 1
        else:
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b' * 64, sid))
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))

    async def fail(job):
        raise PermanentProviderError('controlled permanent')

    svc._run_research = fail
    assert await svc.run_once()
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'failed'
        assert 'pending_fact_request' not in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])


@pytest.mark.asyncio
async def test_joined_request_waits_for_all_active_research_jobs_to_terminalize(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    with svc.store.tx() as db:
        svc._enqueue_job(db, sid, 'refinement', 'controlled-other-active', {})

    async def fail(job):
        raise PermanentProviderError('controlled permanent')

    svc._run_research = fail
    assert await svc.run_once()
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 2
        assert 'pending_fact_request' in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
    assert await svc.run_once()
    assert_followup(svc, sid, jid)
