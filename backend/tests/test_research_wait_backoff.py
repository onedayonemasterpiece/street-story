"""Shared-budget waits preserve progress and bound actual admission retries."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError, research_retry_at
from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical
from test_fact_request_recovery import pending_request
from test_research_control import fixture


class ResourceRefusal(RuntimeError):
    """Shared control boundary contract; these tests do not require its private SDK."""
    resource_failure = True

    def __init__(self, code, retry_after_ms=0):
        self.code = code
        self.retry_after_ms = retry_after_ms
        super().__init__(code)


def setup_adapter(tmp_path):
    service, sid, photo = fixture(tmp_path)
    now = (int(service.store.now()) // 86400 + 1) * 86400 + 12 * 3600
    clock = [now]
    service.store.now = lambda: clock[0]
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.client = SimpleNamespace(endpoint='http://existing-opencode:4097', model_id='configured',
                                     provider_id='opencode', profile_fingerprint='profile-a')
    return adapter, service, {'id': sid, 'photo_sha256': photo}, clock


@pytest.mark.asyncio
async def test_local_daily_wait_rechecks_policy_without_sixty_second_hot_loop(tmp_path):
    service, sid, jid = pending_request(tmp_path)
    now = (int(service.store.now()) // 86400 + 1) * 86400 + 12 * 3600
    clock = [now]
    service.store.now = lambda: clock[0]
    from street_story.research_budget import ensure_budget
    original_budget = ensure_budget(service, sid, explicit=True)
    frozen = {'passage_cursor': 7, 'source_version_id': 'frozen', 'accepted_fact_ids': ['saved']}
    service.store.checkpoint_put(jid, 'grounded_research_v3', frozen)
    calls = []
    async def deny(job):
        calls.append(job['id'])
        raise RetryableProviderError('RESOURCE_DAILY_BUDGET', retry_at=clock[0]+60)
    service._run_research = deny
    assert await service.run_once()
    recheck = now+300
    with service.store.connection() as db:
        assert db.execute('SELECT available_at FROM jobs WHERE id=?', (jid,)).fetchone()[0] == recheck
    for current in (now+60, now+120, recheck-1):
        clock[0] = current
        assert not await service.run_once()
    assert calls == [jid]
    assert service.store.checkpoint_get(jid, 'grounded_research_v3') == frozen
    assert service.store.checkpoint_get(jid, 'worker_non_wait_failures') is None
    clock[0] = recheck
    assert await service.run_once()
    assert calls == [jid, jid]
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert research['research_budget'] == original_budget
        assert research['automatic_research_outcome']['outcome'] == 'resource_blocked'
        assert db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'done'
        assert 'pending_fact_request' in research
    assert service.recover_jobs() == 0


@pytest.mark.asyncio
async def test_local_daily_cooldown_has_bounded_admission_and_resume_or_profile_change(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    probes, sends = [], []
    async def admission_denied(binding):
        probes.append(binding['control_revision'])
        raise ResourceRefusal('RESOURCE_DAILY_BUDGET', 60000)
        sends.append('unreachable')
    recheck = clock[0]+300
    for _ in range(3):
        with pytest.raises(RetryableProviderError) as error:
            await adapter.run(story, 'search', 'same-unit', admission_denied)
        assert error.value.retry_at == recheck
        clock[0] += 60
    assert probes == [0] and not sends
    resumed = {**story, '_identity_research_control_revision': 2}
    for _ in range(2):
        with pytest.raises(RetryableProviderError):
            await adapter.run(resumed, 'search', 'same-unit', admission_denied)
    assert probes == [0, 2] and not sends
    adapter.client.profile_fingerprint = 'profile-b'
    for _ in range(2):
        with pytest.raises(RetryableProviderError):
            await adapter.run(resumed, 'search', 'same-unit', admission_denied)
    assert probes == [0, 2, 2] and not sends
    with service.store.connection() as db:
        rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts'))
    assert len(rows) == 1 and json.loads(rows[0][0])['phase'] == 'created'


@pytest.mark.asyncio
async def test_binding_changed_cannot_repeat_invoke_for_unchanged_binding_after_cooldown(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    calls = []
    async def binding_changed(binding):
        calls.append(binding)
        raise ResearchUnavailable('research_attempt_binding_changed')
    for _ in range(2):
        with pytest.raises(RetryableProviderError):
            await adapter.run(story, 'search', 'same-unit', binding_changed)
        clock[0] += 3601
    assert len(calls) == 1
    adapter.client.profile_fingerprint = 'changed-profile'
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'search', 'same-unit', binding_changed)
    assert len(calls) == 2
    with service.store.connection() as db:
        rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts'))
    assert len(rows) == 1 and json.loads(rows[0][0])['route_failure']['requires_binding_change'] is True


@pytest.mark.asyncio
async def test_addressed_unknown_readback_bypasses_daily_and_binding_wait_without_new_attempt(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    binding, _ = adapter.attempt(story, 'search', 'same-unit')
    route_key = 'research-route-health:' + hashlib.sha256(canonical([
        adapter.client.endpoint, adapter.client.model_id, None, adapter.client.profile_fingerprint]).encode()).hexdigest()
    wait_scope = hashlib.sha256(canonical([route_key, story['id'], story['photo_sha256'], 0, 0]).encode()).hexdigest()
    receipt = {'phase': 'abort_outcome_unknown', 'binding': binding, 'session_id': 'saved-session', 'message_id': 'saved-message',
               'route_failure': {'code': 'research_attempt_binding_changed', 'requires_binding_change': True, 'wait_scope': wait_scope}}
    with service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?', (canonical(receipt), binding['attempt_id']))
    service.store.cache_put('research-quota-health:opencode:configured', {'category': 'RESOURCE_DAILY_BUDGET', 'retry_at': clock[0]+86400}, 86400)
    calls = []
    async def readback(current):
        calls.append(current)
        assert current['session_id'] == 'saved-session' and current['message_id'] == 'saved-message'
        return {'result': 'existing-response'}
    assert await adapter.run(story, 'search', 'same-unit', readback) == {'result': 'existing-response'}
    assert len(calls) == 1
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 1
        assert json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0]) == receipt


@pytest.mark.asyncio
async def test_unavailable_cooldown_prevents_each_story_from_repeating_closed_route(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    calls = []
    async def unavailable(binding):
        calls.append(binding)
        raise ResearchUnavailable('research_runtime_unavailable')
    for _ in range(2):
        with pytest.raises(RetryableProviderError) as error:
            await adapter.run(story, 'search', 'same-unit', unavailable)
        assert error.value.retry_at == clock[0]+300
    assert len(calls) == 1
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'search', 'another-unit', unavailable)
    assert len(calls) == 1


def test_long_provider_hint_and_alternative_route_readiness_are_preserved():
    now = 1791288000
    assert research_retry_at('RESOURCE_DAILY_BUDGET', now, now+86400) == now+300
    assert research_retry_at('research_provider_quota', now, now+86400) == now+86400
    assert research_retry_at('RESOURCE_POLICY_UNAVAILABLE', now, now+900) == now+900
    # Another independently admitted route may recover before the daily-blocked route.
    aggregate = 'all_fact_search_routes_unavailable:RESOURCE_DAILY_BUDGET:gemini:article_url_discovery_unavailable'
    assert research_retry_at(aggregate, now, now+120) == now+120


@pytest.mark.asyncio
async def test_identity_visual_retry_without_hint_preserves_default_deadline(tmp_path, monkeypatch):
    from street_story.headless_identity import HeadlessIdentity
    service, sid, _photo = fixture(tmp_path)
    now = service.store.now()
    service.store.now = lambda: now
    with service.store.tx() as db:
        # The photo fixture initially enqueues legacy identity. Isolate the
        # visual worker so this assertion exercises its no-hint retry branch.
        db.execute("UPDATE jobs SET state='done',lease_until=0")
        jid = service._enqueue_job(db, sid, 'identity_visual', 'visual-no-hint', {})
    async def wait(self, job):
        raise RetryableProviderError('research_chunk_busy')
    monkeypatch.setattr(HeadlessIdentity, 'run', wait)
    assert await service.run_once()
    with service.store.connection() as db:
        row = db.execute('SELECT state,available_at FROM jobs WHERE id=?', (jid,)).fetchone()
    assert tuple(row) == ('retry', now+60)
    assert service.store.checkpoint_get(jid, 'worker_non_wait_failures') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['RESOURCE_NO_CAPACITY', 'RESOURCE_TOKEN_BUDGET', 'RESOURCE_BINDING_BUSY'])
async def test_capacity_or_minute_refusal_does_not_poison_other_binding_route(tmp_path, code):
    adapter, service, story, _clock = setup_adapter(tmp_path)
    async def refused(binding):
        raise ResourceRefusal(code, 60000)
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'search', 'large-or-busy-binding', refused)
    called = []
    async def healthy(binding):
        called.append(binding)
        return {'result': 'healthy-smaller-workload'}
    assert await adapter.run(story, 'search', 'other-smaller-binding', healthy) == {'result': 'healthy-smaller-workload'}
    assert len(called) == 1
    assert service.store.cache_get('research-quota-health:opencode:configured') is None


@pytest.mark.asyncio
async def test_resource_unavailable_profile_change_can_recheck_healthy_route(tmp_path):
    adapter, service, story, _clock = setup_adapter(tmp_path)
    async def refused(binding):
        raise ResourceRefusal('RESOURCE_POLICY_UNAVAILABLE', 30000)
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'search', 'same-unit', refused)
    adapter.client.profile_fingerprint = 'corrected-profile'
    called = []
    async def healthy(binding):
        called.append(binding)
        return {'result': 'healthy-route'}
    assert await adapter.run(story, 'search', 'same-unit', healthy) == {'result': 'healthy-route'}
    assert len(called) == 1


@pytest.mark.asyncio
async def test_input_specific_research_refusal_does_not_poison_other_input(tmp_path):
    adapter, service, story, _clock = setup_adapter(tmp_path)
    async def refused(binding):
        raise ResearchUnavailable('research_image_invalid')
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'vision', 'invalid-input', refused)
    called = []
    async def healthy(binding):
        called.append(binding)
        return {'result': 'healthy-input'}
    assert await adapter.run(story, 'search', 'healthy-input', healthy) == {'result': 'healthy-input'}
    assert len(called) == 1


@pytest.mark.asyncio
async def test_large_fact_daily_refusal_does_not_block_smaller_search_reservation(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    remaining = 2000
    probes = []
    sends = []

    async def large_fact(binding):
        probes.append(('facts', 3000))
        if 3000 > remaining:
            raise ResourceRefusal('RESOURCE_DAILY_BUDGET', 60000)
        sends.append('unreachable')

    async def small_search(binding):
        probes.append(('search', 1000))
        assert 1000 <= remaining
        sends.append('source-discovery')
        return {'sources': [{'url': 'https://example.org/source', 'title': 'Public article'}]}

    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'facts', 'frozen-large-fact-chunk', large_fact)
    # The refused exact unit remains frozen rather than hot-looping authority.
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'facts', 'frozen-large-fact-chunk', large_fact)
    assert probes == [('facts', 3000)] and not sends
    found = await adapter.run(story, 'search', 'independent-small-query', small_search)
    assert found['sources'][0]['url'] == 'https://example.org/source'
    assert probes == [('facts', 3000), ('search', 1000)]
    assert sends == ['source-discovery']
    assert service.store.cache_get('research-quota-health:opencode:configured') is None


@pytest.mark.asyncio
async def test_legacy_blanket_daily_wait_does_not_hide_normal_admission_for_new_unit(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    key = 'research-quota-health:opencode:configured'
    legacy = {'category': 'RESOURCE_DAILY_BUDGET', 'retry_at': clock[0]+86400}
    service.store.cache_put(key, legacy, 86400)
    probes = []

    async def denied(binding):
        probes.append(binding['request_id'])
        raise ResourceRefusal('RESOURCE_DAILY_BUDGET', 60000)

    for _ in range(2):
        with pytest.raises(RetryableProviderError):
            await adapter.run(story, 'search', 'new-frozen-query', denied)
    assert len(probes) == 1
    assert service.store.cache_get(key) == legacy  # no quota/cooldown reset


@pytest.mark.asyncio
async def test_provider429_wait_still_blocks_new_independent_units(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    probes = []

    async def quota(binding):
        probes.append(binding['request_id'])
        raise ResearchUnavailable('provider_429', {'provider_status': 429})

    for unit in ('first-search', 'second-independent-search'):
        with pytest.raises(RetryableProviderError):
            await adapter.run(story, 'search', unit, quota)
    assert len(probes) == 1
    assert service.store.cache_get('research-quota-health:opencode:configured')['category'] == 'research_provider_quota'


@pytest.mark.asyncio
async def test_legacy_exact_workload_wait_rechecks_changed_policy_then_bounds_refusal(tmp_path):
    adapter, service, story, clock = setup_adapter(tmp_path)
    binding, _ = adapter.attempt(story, 'search', 'frozen-query')
    route_key = 'research-route-health:' + hashlib.sha256(canonical([
        adapter.client.endpoint, adapter.client.model_id, None, adapter.client.profile_fingerprint]).encode()).hexdigest()
    key = 'research-workload-health:' + route_key + ':' + binding['request_id']
    service.store.cache_put(key, {'category': 'RESOURCE_DAILY_BUDGET', 'retry_at': clock[0]+86400}, 86400)
    probes = []
    async def invoke(current):
        probes.append(current['attempt_id'])
        if len(probes) == 1:
            raise ResourceRefusal('RESOURCE_DAILY_BUDGET', 86400000)
        return {'result': 'policy-now-admits'}
    for _ in range(2):
        with pytest.raises(RetryableProviderError) as error:
            await adapter.run(story, 'search', 'frozen-query', invoke)
        assert error.value.retry_at == clock[0]+300
    assert len(probes) == 1
    clock[0] += 300
    assert await adapter.run(story, 'search', 'frozen-query', invoke) == {'result': 'policy-now-admits'}
    assert probes == [binding['attempt_id']]*2


def test_recovery_only_revisits_legacy_local_daily_jobs(tmp_path):
    service, sid, jid = pending_request(tmp_path)
    now = service.store.now()
    service.store.now = lambda: now
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='retry',last_error='RESOURCE_DAILY_BUDGET',available_at=? WHERE id=?", (now+86400, jid))
        quota = service._enqueue_job(db, sid, 'identity_visual', 'provider-quota', {})
        db.execute("UPDATE jobs SET state='retry',last_error='research_provider_quota',available_at=? WHERE id=?", (now+86400, quota))
        unknown = service._enqueue_job(db, sid, 'identity_visual', 'original-unknown', {})
        db.execute("UPDATE jobs SET state='retry',last_error='research_attempt_unknown',available_at=? WHERE id=?", (now+86400, unknown))
    assert service.recover_jobs() == 1
    with service.store.connection() as db:
        jobs = {row['id']: row['available_at'] for row in db.execute('SELECT id,available_at FROM jobs')}
    assert jobs[jid] == now
    assert jobs[quota] == jobs[unknown] == now+86400
    assert service.recover_jobs() == 0
