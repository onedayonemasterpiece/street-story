"""Photo deadlines bound queue waits, retries and active work without resends."""
import asyncio
import json
from dataclasses import replace

import pytest

from street_story.research_budget import ensure_budget, require_remaining, reserve_work, ResearchTerminated, ResearchWorkExhausted, finish_attempt
from test_mvp_research import service


def job(svc, sid, kind='identity', key='bounded-wave'):
    with svc.store.tx() as db:
        return svc._enqueue_job(db, sid, kind, key, {'identity_generation': 0})


def test_queue_wait_and_restart_inherit_upload_deadline(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    jid = job(svc, sid)
    monkeypatch.setattr(svc.store, 'now', lambda: original['started_at'] + 181)
    svc.recover_jobs()
    with svc.store.connection() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        state = json.loads(svc._story_row(db, sid)['research_json'])
    assert row['state'] == 'done'
    assert state['research_budget'] == original
    assert state['automatic_research_outcome']['outcome'] == 'deadline_exceeded'
    assert state['identity_progress']['finished'] is True
    assert not any(step['status'] == 'working' for step in state['identity_progress']['steps'])
    assert svc.story(sid)['research_outcome']['outcome'] == 'deadline_exceeded'
    assert svc._claim(claim_story_id=sid) is None


def test_automatic_new_goal_does_not_renew_budget(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    original = ensure_budget(svc, sid)
    monkeypatch.setattr(svc.store, 'now', lambda: original['started_at'] + 400)
    job(svc, sid, 'research', 'model-continuation')
    assert ensure_budget(svc, sid) == original
    monkeypatch.setattr(svc.store, 'now', lambda: original['deadline_at'] + 1)
    with pytest.raises(ResearchTerminated):
        require_remaining(svc, sid)
    assert ensure_budget(svc, sid)['deadline_at'] == original['deadline_at']


@pytest.mark.asyncio
async def test_active_worker_is_bounded_and_preserves_unknown_receipt(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    svc.settings = replace(svc.settings, identity_timeout_seconds=.5)
    ensure_budget(svc, sid, explicit=True)
    jid = job(svc, sid)
    calls = []
    async def pending_worker(_job):
        calls.append(_job['id'])
        with svc.store.tx() as db:
            db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                ('unknown-original', 'original-unit', sid, 'vision_native',
                 json.dumps({'phase': 'unknown', 'thread_id': 'same-thread', 'turn_id': 'same-turn'}), svc.store.now(), svc.store.now()))
        await asyncio.sleep(2)
    svc._run_identity = pending_worker
    assert await svc.run_once(claim_story_id=sid) is True
    assert await svc.run_once(claim_story_id=sid) is False
    assert calls == [jid]
    with svc.store.connection() as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='unknown-original'").fetchone()[0])
        state = json.loads(svc._story_row(db, sid)['research_json'])
    assert receipt == {'phase': 'unknown', 'thread_id': 'same-thread', 'turn_id': 'same-turn'}
    assert state['automatic_research_outcome']['reason'] == 'identity_deadline_exceeded'


@pytest.mark.asyncio
async def test_one_unknown_visual_unit_cannot_end_independent_discovery(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    from street_story.headless_identity import HeadlessIdentity
    svc, _, story = service(tmp_path)
    sid = story['id']
    envelope = ensure_budget(svc, sid)
    visual = job(svc, sid, 'identity_visual', 'unknown-visual')
    discovery = job(svc, sid, 'identity', 'independent-discovery')
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',lease_until=? WHERE id=?",
                   (envelope['deadline_at'], discovery))
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('frozen-unknown', 'same-comparison', sid, 'vision_google_pair',
             json.dumps({'phase': 'unknown', 'provider_send_state': 'possibly_sent', 'comparison_id': 'same-comparison'}),
             svc.store.now(), svc.store.now()))
    async def unknown(_self, _job):
        raise RetryableProviderError('research_visual_pair_outcome_unknown', retry_at=svc.store.now()+300)
    monkeypatch.setattr(HeadlessIdentity, 'run', unknown)
    assert await svc.run_once(claim_kind='identity_visual')
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, sid)['research_json'])
        assert 'automatic_research_outcome' not in research
        assert tuple(db.execute('SELECT state,available_at FROM jobs WHERE id=?', (visual,)).fetchone()) == (
            'retry', pytest.approx(envelope['identity_deadline_at'], abs=.02))
        assert db.execute('SELECT state FROM jobs WHERE id=?', (discovery,)).fetchone()[0] == 'running'
        assert json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0])['comparison_id'] == 'same-comparison'
    monkeypatch.setattr(svc.store, 'now', lambda: envelope['identity_deadline_at']+1)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='done',lease_until=0 WHERE id=?", (discovery,))
    svc.recover_jobs()
    assert svc.story(sid)['research_outcome']['outcome'] == 'deadline_exceeded'


@pytest.mark.asyncio
async def test_product_deadline_closes_before_cancelled_sdk_finishes_accounting(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    svc.settings = replace(svc.settings, identity_timeout_seconds=.15)
    ensure_budget(svc, sid, explicit=True)
    job(svc, sid)
    entered, cleanup = asyncio.Event(), asyncio.Event()
    async def delayed_sdk(_job):
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await cleanup.wait()  # Existing remote receipt/accounting cleanup.
            raise
    svc._run_identity = delayed_sdk
    task = asyncio.create_task(svc.run_once(claim_story_id=sid))
    try:
        await entered.wait()
        await asyncio.sleep(.2)
        assert not task.done()
        outcome = svc.story(sid)['research_outcome']
        assert outcome['outcome'] == 'deadline_exceeded'
        assert outcome['reason'] == 'identity_deadline_exceeded'
        assert not svc.story(sid)['research_pending']['identity']
    finally:
        cleanup.set()
        await task


def test_explicit_owner_wave_renews_time_without_erasing_history(tmp_path, monkeypatch):
    svc, _, story = service(tmp_path)
    sid = story['id']
    initial = ensure_budget(svc, sid)
    monkeypatch.setattr(svc.store, 'now', lambda: initial['deadline_at'] + 60)
    renewed = ensure_budget(svc, sid, explicit=True)
    assert renewed['started_at'] == initial['deadline_at'] + 60
    assert renewed['deadline_at'] == renewed['started_at'] + 480


def test_exact_pair_envelope_preserves_original_units_and_resets_only_on_owner_wave(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    assert reserve_work(svc, sid, 'exact_pairs', ['ref1', 'ref2']) == ['ref1', 'ref2']
    assert reserve_work(svc, sid, 'exact_pairs', ['ref1']) == ['ref1', 'ref2']
    reserve_work(svc, sid, 'exact_pairs', ['ref3', 'ref4', 'ref5', 'ref6'])
    with pytest.raises(ResearchWorkExhausted) as exhausted:
        reserve_work(svc, sid, 'exact_pairs', ['ref7'])
    assert exhausted.value.kind == 'exact_pairs'
    assert len(ensure_budget(svc, sid)['work_units']['exact_pairs']) == 6
    ensure_budget(svc, sid, explicit=True)
    assert reserve_work(svc, sid, 'exact_pairs', ['ref7']) == ['ref7']


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
                now[0] = budget['identity_deadline_at'] + 1
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
    with pytest.raises(ResearchTerminated, match='scope_superseded' if superseded else 'identity_deadline_exceeded'):
        await svc._execute_job_with_deadline({'kind': 'identity', 'story_id': story['id']})
    assert calls == []
    assert admissions == ['admission'] and pool.snapshot()['in_flight'] == 0
    # The worker scope is reset; unrelated requests inherit no expired story.
    await client._generate('fixture', 20, ['fixture'], quota=Admission())
    assert calls == ['provider_send']


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
    now = original['deadline_at'] + 60
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
