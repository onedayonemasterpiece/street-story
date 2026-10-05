"""Resource waits do not spend the content-worker failure budget."""
import json

import pytest

from test_fact_request_recovery import pending_request, assert_followup
from street_story.errors import MalformedProviderResponse, RetryableProviderError
from street_story.service import MAX_JOB_ATTEMPTS


def job_state(svc, jid):
    with svc.store.connection() as db:
        return dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())


def ready_again(svc, jid):
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET available_at=0 WHERE id=?', (jid,))


@pytest.mark.asyncio
async def test_many_quota_and_chunk_waits_then_first_closed_failure_is_retryable(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    frozen = {'source_version_id': 'srcv_frozen', 'passage_cursor': 7, 'accepted_fact_ids': ['fact_saved']}
    svc.store.checkpoint_put(jid, 'grounded_research_v3', frozen)
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET attempts=94 WHERE id=?', (jid,))
    for code in ('RESOURCE_DAILY_BUDGET', 'research_chunk_busy'):
        async def wait(job):
            raise RetryableProviderError(code, retry_at=svc.store.now()+60)
        svc._run_research = wait
        assert await svc.run_once()
        assert svc.store.checkpoint_get(jid, 'worker_non_wait_failures') is None
        ready_again(svc, jid)
    async def closed(job):
        raise MalformedProviderResponse('gigachat:semantic_json_invalid')
    svc._run_research = closed
    assert await svc.run_once()
    assert job_state(svc, jid)['attempts'] == 97
    assert job_state(svc, jid)['state'] == 'retry'
    assert svc.store.checkpoint_get(jid, 'worker_non_wait_failures') == {'count': 1}
    assert svc.store.checkpoint_get(jid, 'grounded_research_v3') == frozen
    assert svc.recover_jobs() == 0
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert 'pending_fact_request' in json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])


@pytest.mark.asyncio
async def test_only_actual_worker_failures_exhaust_the_budget_and_schedule_joined_request(tmp_path):
    svc, sid, jid = pending_request(tmp_path)
    async def closed(job):
        raise MalformedProviderResponse('gigachat:semantic_json_invalid')
    for index in range(MAX_JOB_ATTEMPTS):
        svc._run_research = closed
        assert await svc.run_once()
        assert svc.store.checkpoint_get(jid, 'worker_non_wait_failures') == {'count': index+1}
        if index < MAX_JOB_ATTEMPTS-1:
            assert job_state(svc, jid)['state'] == 'retry'
            async def busy(job):
                raise RetryableProviderError('research_chunk_busy', retry_at=svc.store.now()+60)
            ready_again(svc, jid)
            svc._run_research = busy
            assert await svc.run_once()
            assert svc.store.checkpoint_get(jid, 'worker_non_wait_failures') == {'count': index+1}
            ready_again(svc, jid)
    assert_followup(svc, sid, jid)


@pytest.mark.asyncio
async def test_unknown_attempt_readback_never_resends_or_consumes_failure_budget(tmp_path):
    svc, _, jid = pending_request(tmp_path)
    # Product adapter's durable unknown outcome fence is evaluated before send.
    frozen = {'phase': 'submitted', 'input_sha256': 'same_frozen_input', 'provider_send_state': 'unknown'}
    svc.store.checkpoint_put(jid, 'unknown_model_attempt', frozen)
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET attempts=97 WHERE id=?', (jid,))
    sends = []
    async def fenced(job):
        if svc.store.checkpoint_get(job['id'], 'unknown_model_attempt')['phase'] == 'submitted':
            raise RetryableProviderError('gigachat_attempt_outcome_unknown', retry_at=svc.store.now()+300)
        sends.append(job['id'])
    svc._run_research = fenced
    for _ in range(2):
        assert await svc.run_once()
        assert job_state(svc, jid)['state'] == 'retry'
        assert svc.recover_jobs() == 0
        ready_again(svc, jid)
    assert sends == []
    assert svc.store.checkpoint_get(jid, 'worker_non_wait_failures') is None
    assert svc.store.checkpoint_get(jid, 'unknown_model_attempt') == frozen
