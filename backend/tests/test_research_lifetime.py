"""Elapsed time cannot terminate durable research; SOURCE and UNKNOWN stay fenced."""
import asyncio
import json

import pytest

from street_story.research_budget import ensure_budget, require_remaining, reserve_work, ResearchTerminated, finish_attempt
from test_mvp_research import service


def job(svc, sid, kind='identity', key='bounded-wave'):
    with svc.store.tx() as db:
        return svc._enqueue_job(db, sid, kind, key, {'identity_generation': 0})


@pytest.mark.parametrize('kind', ['identity', 'identity_visual', 'research', 'refinement'])
@pytest.mark.parametrize('elapsed', [600, 1200])
def test_queue_wait_and_restart_continue_after_closed_client(tmp_path, monkeypatch, kind, elapsed):
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    jid = job(svc, sid, kind)
    monkeypatch.setattr(svc.store, 'now', lambda: original['started_at'] + elapsed)
    svc.recover_jobs()
    with svc.store.connection() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        state = json.loads(svc._story_row(db, sid)['research_json'])
    assert row['state'] == 'ready'
    assert state['research_budget'] == original
    assert 'automatic_research_outcome' not in state
    assert require_remaining(svc, sid, 'identity') == float('inf')
    assert require_remaining(svc, sid, 'facts') == float('inf')
    assert svc._claim(claim_story_id=sid)['id'] == jid


def test_active_legacy_deadline_migrates_without_new_operation(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    initial = ensure_budget(svc, sid)
    old = {**initial, 'policy_version': 'photo-research-budget-v1',
           'identity_deadline_at': initial['started_at'] + 180, 'deadline_at': initial['started_at'] + 480}
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        research['research_budget'] = old
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
    jid = job(svc, sid)
    monkeypatch.setattr(svc.store, 'now', lambda: initial['started_at'] + 1200)
    svc.recover_jobs()
    migrated = ensure_budget(svc, sid)
    assert migrated['started_at'] == initial['started_at']
    assert migrated['previous_deadlines']['identity_deadline_at'] == old['identity_deadline_at']
    assert migrated['identity_deadline_at'] is migrated['deadline_at'] is None
    assert svc._claim(claim_story_id=sid)['id'] == jid


@pytest.mark.asyncio
async def test_active_worker_keeps_unknown_receipt_after_twenty_minutes(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    jid = job(svc, sid)
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = {'phase': 'unknown', 'thread_id': 'same-thread', 'turn_id': 'same-turn'}
    calls = []
    async def pending_worker(_job):
        calls.append(_job['id'])
        with svc.store.tx() as db:
            db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
                ('unknown-original', 'original-unit', sid, 'vision_native', json.dumps(receipt), svc.store.now(), svc.store.now()))
        entered.set()
        await release.wait()
        raise RetryableProviderError('original_outcome_unknown', retry_at=svc.store.now()+3600)
    svc._run_identity = pending_worker
    task = asyncio.create_task(svc.run_once(claim_story_id=sid))
    try:
        await entered.wait()
        monkeypatch.setattr(svc.store, 'now', lambda: original['started_at']+1200)
        await svc.maintenance_tick()
        await asyncio.sleep(0)
        assert not task.done()
        assert svc.story(sid)['research_pending']['identity']
        release.set()
        assert await task
        with svc.store.connection() as db:
            row = db.execute('SELECT state,available_at FROM jobs WHERE id=?', (jid,)).fetchone()
            assert row['state'] == 'retry' and row['available_at'] == svc.store.now()+3600
            assert json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0]) == receipt
            state = json.loads(svc._story_row(db, sid)['research_json'])
        assert 'automatic_research_outcome' not in state and calls == [jid]
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['identity', 'identity_visual', 'research', 'refinement'])
async def test_individual_request_timeout_retries_whole_task_without_terminal_state(tmp_path, kind, monkeypatch):
    from street_story.headless_identity import HeadlessIdentity
    svc, _, story = service(tmp_path)
    jid = job(svc, story['id'], kind)
    async def timed_out(*args):
        raise TimeoutError('one transport request')
    svc._run_identity = svc._run_research = timed_out
    monkeypatch.setattr(HeadlessIdentity, 'run', timed_out)
    assert await svc.run_once(claim_kind=kind)
    with svc.store.connection() as db:
        row = db.execute('SELECT state,last_error,available_at FROM jobs WHERE id=?', (jid,)).fetchone()
        research = json.loads(svc._story_row(db, story['id'])['research_json'])
    assert row['state'] == 'retry' and row['last_error'] == 'research_request_timeout'
    assert row['available_at'] > svc.store.now()
    assert 'automatic_research_outcome' not in research


@pytest.mark.parametrize('kind', ['exact_pairs', 'query_hypotheses', 'pages', 'planner_calls'])
def test_distinct_work_has_no_artificial_wave_ceiling_and_same_unit_is_idempotent(tmp_path, kind):
    svc, _, story = service(tmp_path)
    units = [str(n) for n in range(100)]
    assert reserve_work(svc, story['id'], kind, units) == units
    assert reserve_work(svc, story['id'], kind, units) == units
    assert ensure_budget(svc, story['id'])['work_units'][kind] == units


def test_explicit_owner_wave_preserves_history_without_elapsed_cutoff(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    initial = ensure_budget(svc, story['id'])
    monkeypatch.setattr(svc.store, 'now', lambda: initial['started_at']+1200)
    renewed = ensure_budget(svc, story['id'], explicit=True)
    assert renewed['started_at'] == initial['started_at']+1200
    assert renewed['deadline_at'] is renewed['identity_deadline_at'] is None


def test_terminal_attempt_forbids_new_send_even_with_positive_remaining_time(tmp_path):
    svc, _, story = service(tmp_path)
    with svc.store.tx() as db:
        finish_attempt(svc, db, story['id'], outcome='resource_blocked', reason='quota_reset_after_deadline')
    with pytest.raises(ResearchTerminated, match='already_finished'):
        require_remaining(svc, story['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('superseded', [False, True])
async def test_google_admission_cannot_send_after_its_research_scope_expires(tmp_path, monkeypatch, superseded):
    pytest.importorskip('ai_resource_control.client', reason='Private pinned admission SDK: verified by the full devserver suite')
    from types import SimpleNamespace
    from pydantic import SecretStr
    from street_story.gemini import GeminiExecutor, GeminiKeyPool
    from street_story.providers import GeminiClient
    svc, _, story = service(tmp_path)
    budget = ensure_budget(svc, story['id'])
    now = [budget['started_at']]
    monkeypatch.setattr(svc.store, 'now', lambda: now[0])
    calls = []
    admissions = []
    class Admission:
        async def run(self, key, timeout, size, invoke):
            admissions.append('admission')
            if superseded:
                with svc.store.tx() as db:
                    research = json.loads(svc._story_row(db, story['id'])['research_json'])
                    research['identity_generation'] = 1
                    db.execute('UPDATE stories SET research_json=? WHERE id=?',
                        (json.dumps(research), story['id']))
            else:
                now[0] = budget['started_at'] + 1200
            return await invoke()
    client = GeminiClient.__new__(GeminiClient)
    client.settings = svc.settings
    async def sdk(*args, **kwargs):
        calls.append('provider_send')
        return SimpleNamespace(text='unrelated response')
    client._provider_request = sdk
    pool = GeminiKeyPool(svc.store, (SecretStr('fixture-one'), SecretStr('fixture-two')),
        svc.settings.gemini_model, clock=lambda: now[0])
    executor = GeminiExecutor(pool)
    async def handler(_job):
        async def invoke(key, timeout):
            return await client._generate(key, timeout, ['fixture'], quota=Admission())
        await executor.execute('grounded_research', invoke)
    svc._run_identity = handler
    if superseded:
        with pytest.raises(ResearchTerminated, match='scope_superseded'):
            await svc._execute_job_with_deadline({'kind': 'identity', 'story_id': story['id']})
        assert calls == []
    else:
        await svc._execute_job_with_deadline({'kind': 'identity', 'story_id': story['id']})
        assert calls == ['provider_send']
    assert admissions == ['admission'] and pool.snapshot()['in_flight'] == 0
    # The worker scope is reset; unrelated requests inherit no expired story.
    await client._generate('fixture', 20, ['fixture'], quota=Admission())
    assert calls == (['provider_send'] if superseded else ['provider_send', 'provider_send'])


def test_late_identity_sibling_cannot_end_confirmed_fact_wave(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        research['visual_identity'] = {'status': 'match', 'candidate_id': 'physical-building'}
        db.execute("UPDATE stories SET research_json=?,state='researching' WHERE id=?", (json.dumps(research), sid))
        fact_job = svc._enqueue_job(db, sid, 'research', 'in-progress-facts', {})
        finish_attempt(svc, db, sid, outcome='deadline_exceeded', reason='identity_deadline_exceeded', purpose='identity')
        saved = svc._story_row(db, sid)
        research = json.loads(saved['research_json'])
        fact = db.execute('SELECT state FROM jobs WHERE id=?', (fact_job,)).fetchone()
    assert saved['state'] == 'researching'
    assert 'automatic_research_outcome' not in research
    assert research['identity_attempt_outcome']['purpose'] == 'identity'
    assert research['research_budget'] == original
    assert fact['state'] == 'ready'


@pytest.mark.parametrize('owner_request', [False, True])
def test_terminal_queued_request_does_not_reopen_product(tmp_path, owner_request):
    svc, _, story = service(tmp_path)
    sid = story['id']
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        research['pending_fact_request'] = {'input_revision': 'model-alternate', 'photo_sha256': story['photo_sha256'],
                                           'identity_generation': 0, 'owner_research_wave': owner_request}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
        finish_attempt(svc, db, sid, outcome='no_supported_facts', reason='search_exhausted')
        assert svc._schedule_joined_fact_request(db, sid) is False
        assert not db.execute("SELECT 1 FROM jobs WHERE story_id=? AND state IN ('ready','retry','running')", (sid,)).fetchone()


def test_owner_new_fact_request_renews_terminal_wave_and_idempotent_replay_does_not(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        research['visual_identity'] = {'status': 'match', 'candidate_id': 'physical-building'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
        finish_attempt(svc, db, sid, outcome='no_supported_facts', reason='search_exhausted')
    now = original['started_at'] + 600
    monkeypatch.setattr(svc.store, 'now', lambda: now)
    request = {'action': 'research', 'goal': 'Сведения о ремонте'}
    svc.mutate_facts(sid, 'new-owner-goal', request)
    renewed = ensure_budget(svc, sid)
    assert renewed['started_at'] == now
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
    assert 'automatic_research_outcome' not in research
    monkeypatch.setattr(svc.store, 'now', lambda: now + 20)
    svc.mutate_facts(sid, 'new-owner-goal', request)
    assert ensure_budget(svc, sid) == renewed


def test_separately_accepted_waiting_owner_wave_runs_after_exhaustion_once(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        research['visual_identity'] = {'status': 'match', 'candidate_id': 'physical-building'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
    svc.mutate_facts(sid, 'first-accepted', {'action': 'research', 'goal': 'Construction history'})
    initial = ensure_budget(svc, sid)
    svc.mutate_facts(sid, 'second-accepted', {'action': 'research', 'goal': 'Later restoration'})
    with svc.store.connection() as db:
        waiting = json.loads(svc._story_row(db, sid)['research_json'])['pending_fact_request']
        assert db.execute("SELECT resource_id FROM idempotency WHERE key='second-accepted' AND action='research'").fetchone()[0] == sid
    now = initial['started_at'] + 600
    monkeypatch.setattr(svc.store, 'now', lambda: now)
    with svc.store.tx() as db:
        finish_attempt(svc, db, sid, outcome='deadline_exceeded', reason='research_deadline_exceeded')
        db.execute("UPDATE jobs SET state='done' WHERE story_id=? AND kind='research'", (sid,))
        assert svc._schedule_joined_fact_request(db, sid)
        assert not svc._schedule_joined_fact_request(db, sid)
    renewed = ensure_budget(svc, sid)
    assert renewed['started_at'] == now
    with svc.store.connection() as db:
        current = json.loads(svc._story_row(db, sid)['research_json'])
        jobs = db.execute("SELECT payload_json FROM jobs WHERE story_id=? AND kind='research' AND state='ready'", (sid,)).fetchall()
    assert len(jobs) == 1 and json.loads(jobs[0][0])['input_revision'] == waiting['input_revision']
    assert json.loads(jobs[0][0])['accepted_owner_request']['key'] == 'second-accepted'
    assert current['research_attempt_history'][-1]['automatic_research_outcome']['outcome'] == 'deadline_exceeded'
    assert current['research_attempt_history'][-1]['research_budget']['started_at'] == initial['started_at']
    svc.mutate_facts(sid, 'second-accepted', {'action': 'research', 'goal': 'Later restoration'})
    assert ensure_budget(svc, sid) == renewed


@pytest.mark.parametrize('kind', ['identity', 'identity_visual', 'research', 'refinement'])
def test_restart_does_not_close_research_after_repeated_worker_failures(tmp_path, kind):
    from street_story.service import canonical
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    jid = job(svc, sid, kind)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='retry',attempts=20,last_error='worker_failure:RuntimeError' WHERE id=?", (jid,))
        db.execute("INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?)",
                   (jid, 'worker_non_wait_failures', canonical({'count': 20}), svc.store.now()))
    svc.recover_jobs()
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'retry'
        research = json.loads(svc._story_row(db, sid)['research_json'])
    assert research['research_budget'] == original
    assert 'automatic_research_outcome' not in research


def test_legacy_environment_cannot_restore_background_cutoffs(monkeypatch):
    from street_story.config import Settings
    for name, value in {'IDENTITY_TIMEOUT_SECONDS': '180', 'RESEARCH_TIMEOUT_SECONDS': '480',
        'IDENTITY_MAX_EXACT_PAIRS': '6', 'IDENTITY_MAX_QUERY_HYPOTHESES': '8',
        'IDENTITY_MAX_PAGES': '6', 'IDENTITY_MAX_PLANNER_CALLS': '2'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('GEMINI_CALL_TIMEOUT_SECONDS', '17')
    monkeypatch.setenv('GEMINI_ATTEMPT_TIMEOUT_SECONDS', '53')
    settings = Settings.from_env()
    assert settings.identity_timeout_seconds == settings.research_timeout_seconds == 0
    assert settings.identity_max_exact_pairs == settings.identity_max_query_hypotheses == 0
    assert settings.identity_max_pages == settings.identity_max_planner_calls == 0
    assert settings.gemini_call_timeout_seconds == 17
    assert settings.gemini_attempt_timeout_seconds == 53
