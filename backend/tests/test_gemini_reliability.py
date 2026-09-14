from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story.config import Settings
from street_story.db import Store
from street_story.gemini import GeminiExecutor, GeminiKeyPool, GeminiPolicy, GeminiUnavailable, classify_error
from street_story.providers import PermanentProviderError

KEYS = ('test-private-key-A', 'test-private-key-B', 'test-private-key-C')


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class ProviderError(Exception):
    def __init__(self, code, retry=None, status=None, message='provider-private-text'):
        super().__init__(message)
        self.code = code
        self.status = status
        self.response = SimpleNamespace(headers={'Retry-After': str(retry)} if retry is not None else {})
        self.details = {'error': {'code': code, 'message': message}}


def pool(tmp_path, keys=KEYS, policy=None):
    clock = Clock()
    p = GeminiKeyPool(Store(tmp_path/'test.sqlite'), tuple(SecretStr(k) for k in keys), 'gemini-3.1-flash-lite', policy=policy, clock=clock)
    return p, GeminiExecutor(p), clock


@pytest.mark.asyncio
@pytest.mark.parametrize('failures', [[429], [429, 429], [503], [TimeoutError()], [httpx.ConnectError('private prompt')], [401]])
async def test_immediate_failover(tmp_path, failures):
    p, executor, clock = pool(tmp_path)
    seen = []

    async def call(key, timeout):
        seen.append(key)
        if len(seen) <= len(failures):
            failure = failures[len(seen)-1]
            raise ProviderError(failure) if isinstance(failure, int) else failure
        return 'success'

    assert await executor.execute('grounded_research', call) == 'success'
    assert seen == list(KEYS[:len(failures)+1])
    assert clock() == 1000  # No scheduler sleep/cooldown wait between healthy keys.
    assert p.snapshot()['in_flight'] == 0


@pytest.mark.asyncio
async def test_retry_after_cooldown_and_expiry_persist_across_restart(tmp_path):
    p, executor, clock = pool(tmp_path, keys=KEYS[:1])

    async def limited(key, timeout):
        raise ProviderError(429, retry=120)

    with pytest.raises(GeminiUnavailable) as error:
        await executor.execute('grounded_research', limited)
    assert error.value.retry_at == 1120
    called = []

    async def ok(key, timeout):
        called.append(key)
        return 'ok'

    restarted = GeminiKeyPool(p.store, p.keys, p.model, clock=clock)
    with pytest.raises(GeminiUnavailable):
        await GeminiExecutor(restarted).execute('grounded_research', ok)
    assert called == []
    # A grounded-search quota does not ban transcription.
    assert await GeminiExecutor(restarted).execute('transcription', ok) == 'ok'
    clock.value = 1120
    assert await GeminiExecutor(restarted).execute('grounded_research', ok) == 'ok'
    health = restarted.snapshot('grounded_research')
    assert health['healthy_keys'] == 1 and health['cooling_down_keys'] == 0


@pytest.mark.asyncio
async def test_auth_disable_applies_across_workloads_and_restart(tmp_path):
    p, executor, clock = pool(tmp_path, keys=KEYS[:2])

    async def call(key, timeout):
        if key == KEYS[0]:
            raise ProviderError(401)
        return 'ok'

    await executor.execute('grounded_research', call)
    restarted = GeminiKeyPool(p.store, p.keys, p.model, clock=clock)
    called = []

    async def ok(key, timeout):
        called.append(key)
        return 'ok'

    await GeminiExecutor(restarted).execute('transcription', ok)
    assert called == [KEYS[1]]
    assert restarted.snapshot()['disabled_keys'] == 1


@pytest.mark.asyncio
async def test_concurrent_operations_reserve_different_slots(tmp_path):
    p, executor, _ = pool(tmp_path, keys=KEYS[:2])
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def call(key, timeout):
        seen.append(key)
        if len(seen) == 2:
            started.set()
        await release.wait()
        return 'ok'

    tasks = [asyncio.create_task(executor.execute('grounded_research', call)) for _ in range(2)]
    await asyncio.wait_for(started.wait(), 2)
    assert len(set(seen)) == 2
    assert p.snapshot()['in_flight'] == 2
    release.set()
    assert await asyncio.gather(*tasks) == ['ok', 'ok']
    assert p.snapshot()['in_flight'] == 0


@pytest.mark.asyncio
async def test_local_accounting_deprioritizes_and_reserves_before_provider(tmp_path):
    policy = GeminiPolicy(grounded_research_rpm=1)
    p, executor, clock = pool(tmp_path, keys=KEYS[:2], policy=policy)
    seen = []

    async def call(key, timeout):
        seen.append(key)
        return 'ok'

    await executor.execute('grounded_research', call)
    await executor.execute('grounded_research', call)
    with pytest.raises(GeminiUnavailable) as error:
        await executor.execute('grounded_research', call)
    assert error.value.retry_at == 1020
    assert seen == list(KEYS[:2])
    clock.value = 1020
    await executor.execute('grounded_research', call)


def test_single_key_and_ref_configuration_no_secrets(monkeypatch, tmp_path):
    for name in ('GEMINI_API_KEYS', 'GEMINI_API_KEY_REFS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GEMINI_API_KEY', KEYS[0])
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    cfg = Settings.from_env()
    assert [k.get_secret_value() for k in cfg.gemini_keys] == [KEYS[0]]
    monkeypatch.setenv('GOOGLE_API_KEY', KEYS[0])
    monkeypatch.setenv('GOOGLE_API_KEY2', KEYS[1])
    monkeypatch.setenv('GEMINI_API_KEY_REFS', '["GOOGLE_API_KEY", "GOOGLE_API_KEY2"]')
    cfg = Settings.from_env()
    assert [k.get_secret_value() for k in cfg.gemini_keys] == list(KEYS[:2])
    assert all(k not in repr(cfg)+json.dumps(asdict(cfg), default=str) for k in KEYS)


def test_invalid_secret_json_never_echoes_values(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEYS', KEYS[0])
    with pytest.raises(ValueError) as e:
        Settings.from_env()
    assert KEYS[0] not in str(e.value)


def test_google_retry_info_and_permanent_schema_classification():
    e = ProviderError(429)
    e.details = {'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '123.5s'}]}}
    assert classify_error(e, now=1000).retry_after == 123.5
    assert classify_error(ProviderError(400, status='INVALID_ARGUMENT'), now=1000).category == 'invalid_request'
    assert classify_error(ProviderError(404), now=1000).category == 'unsupported_model'


@pytest.mark.asyncio
async def test_permanent_request_does_not_rotate_or_disable_keys(tmp_path):
    p, executor, _ = pool(tmp_path)
    calls = []

    async def call(key, timeout):
        calls.append(key)
        raise ProviderError(400, status='INVALID_ARGUMENT')

    with pytest.raises(PermanentProviderError):
        await executor.execute('grounded_research', call)
    assert calls == [KEYS[0]]
    assert p.snapshot()['disabled_keys'] == 0


@pytest.mark.asyncio
async def test_safe_observability(tmp_path, caplog):
    p, executor, _ = pool(tmp_path, keys=KEYS[:1])
    caplog.set_level('INFO')

    async def call(key, timeout):
        raise ProviderError(429, message='Bearer '+key+' private prompt')

    with pytest.raises(GeminiUnavailable) as e:
        await executor.execute('grounded_research', call)
    serialized = caplog.text + str(e.value) + json.dumps(p.snapshot())
    assert 'selected' in serialized and '429' in serialized and 'all_keys_unavailable' in serialized
    assert KEYS[0] not in serialized and 'private prompt' not in serialized and 'Bearer' not in serialized
    with p.store.connection() as db:
        rows = str([tuple(x) for x in db.execute('select * from gemini_key_health')])
    assert KEYS[0] not in rows and 'private prompt' not in rows


@pytest.mark.asyncio
async def test_real_timeout_budget_cancels_and_rotates_without_orphan_calls(tmp_path):
    p, executor, _ = pool(tmp_path, keys=KEYS[:2], policy=GeminiPolicy(call_timeout=.01, attempt_timeout=.2))
    cancelled = []

    async def call(key, timeout):
        if key == KEYS[0]:
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.append(key)
        return 'ok'

    assert await executor.execute('grounded_research', call) == 'ok'
    assert cancelled == [KEYS[0]]
    assert p.snapshot()['in_flight'] == 0


@pytest.mark.asyncio
async def test_internal_attempt_count_bound(tmp_path):
    p, executor, _ = pool(tmp_path, policy=GeminiPolicy(max_failover_keys=2))
    calls = []

    async def call(key, timeout):
        calls.append(key)
        raise ProviderError(503)

    with pytest.raises(GeminiUnavailable):
        await executor.execute('transcription', call)
    assert calls == list(KEYS[:2])


@pytest.mark.asyncio
async def test_cancel_releases_reservation_without_recording_success(tmp_path):
    p, executor, _ = pool(tmp_path)
    entered = asyncio.Event()

    async def call(key, timeout):
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(executor.execute('grounded_research', call))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.snapshot()['in_flight'] == 0
    assert p.snapshot()['cooling_down_keys'] == 1


@pytest.mark.asyncio
async def test_restart_preserves_unknown_inflight_reservation(tmp_path):
    p, _, clock = pool(tmp_path, keys=KEYS[:1])
    assert p.reserve('grounded_research', set(), 20)
    fresh = GeminiKeyPool(p.store, p.keys, p.model, clock=clock)
    assert fresh.reserve('transcription', set(), 20) is None
    clock.value += 26
    assert fresh.reserve('grounded_research', set(), 20) is not None


@pytest.mark.asyncio
async def test_empty_pool_and_all_invalid_keys_are_durable_runtime_problems(tmp_path):
    p, executor, _ = pool(tmp_path, keys=())

    async def invalid(key, timeout):
        raise ProviderError(401)

    with pytest.raises(GeminiUnavailable, match='no_configured_keys'):
        await executor.execute('grounded_research', invalid)
    p, executor, _ = pool(tmp_path, keys=KEYS)
    with pytest.raises(GeminiUnavailable, match='all_keys_disabled'):
        await executor.execute('grounded_research', invalid)
    assert p.snapshot()['disabled_keys'] == 3


@pytest.mark.asyncio
async def test_malformed_provider_response_rotates(tmp_path):
    from street_story.errors import MalformedProviderResponse
    p, executor, _ = pool(tmp_path)
    calls = []

    async def call(key, timeout):
        calls.append(key)
        if len(calls) == 1:
            raise MalformedProviderResponse('not persisted')
        return 'ok'

    assert await executor.execute('grounded_research', call) == 'ok'
    assert calls == list(KEYS[:2])


class PipelineGemini:
    """Actual GeminiClient/executor; only the SDK HTTP boundary is deterministic."""
    @staticmethod
    def build(settings, store, failures):
        from street_story.providers import GeminiClient
        client = GeminiClient(settings, store)
        client.calls = []
        client.failures = dict(failures)

        async def generate(key, timeout, contents, config=None):
            operation = 'transcription' if config is None else 'grounded_research'
            client.calls.append((operation, key))
            if operation == 'transcription':
                return SimpleNamespace(text='test transcript')
            code = client.failures.get(key)
            if code:
                raise ProviderError(code, retry=120 if code == 429 else None, message='429 RESOURCE_EXHAUSTED '+key+' private voice')
            return SimpleNamespace(text=json.dumps({'place_name':'Test place','summary':'Summary','draft_text':'Draft','facts':[{'text':'Fact','confidence':.9,'source_urls':['https://ru.wikipedia.org/wiki/Test']}]}), candidates=[])

        client._generate = generate
        return client


def pipeline(tmp_path, failures):
    from test_backend import FakeOSM, FakeVibePublish, FakeWikipedia, config
    from street_story.service import ProviderBundle, StreetStoryService
    cfg = replace(config(tmp_path), gemini_api_key=None, gemini_api_keys=tuple(SecretStr(k) for k in KEYS))
    store = Store(tmp_path/'street-story.sqlite3')
    gemini = PipelineGemini.build(cfg, store, failures)
    osm, wiki = FakeOSM(), FakeWikipedia()
    osm.calls, wiki.calls = 0, 0
    lookup, nearby = osm.lookup, wiki.nearby

    async def counted_lookup(*args):
        osm.calls += 1
        return await lookup(*args)

    async def counted_nearby(*args):
        wiki.calls += 1
        return await nearby(*args)

    osm.lookup, wiki.nearby = counted_lookup, counted_nearby
    svc = StreetStoryService(cfg, ProviderBundle(osm, wiki, gemini, FakeVibePublish()))
    clock = Clock()
    svc.store.now = clock
    gemini.pool.clock = clock
    return svc, gemini, osm, wiki, clock


def admit(svc, name='pipeline'):
    from test_backend import add_chunk, create, finish, open_voice
    story = create(svc, key=name, client=name)
    open_voice(svc, story['id'], session=name)
    sha = add_chunk(svc, story['id'], name, 0, 'test')[1]
    finish(svc, story['id'], name, [sha])
    return story['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('failures', [{KEYS[0]:429}, {KEYS[0]:429, KEYS[1]:429}])
async def test_failover_finishes_same_durable_job_attempt_without_repeating_stages(tmp_path, failures):
    svc, gemini, osm, wiki, _ = pipeline(tmp_path, failures)
    sid = admit(svc)
    assert await svc.run_once()
    story = svc.story(sid)
    assert story['state'] == 'review' and story['facts'][0]['selected']
    assert story['error'] is None
    assert [op for op, _ in gemini.calls].count('transcription') == 1
    assert osm.calls == wiki.calls == 1
    with svc.store.connection() as db:
        job = db.execute('select * from jobs').fetchone()
    assert job['attempts'] == 1 and job['state'] == 'done'
    assert len(gemini.calls) == len(failures)+2


@pytest.mark.asyncio
async def test_all_keys_exhausted_durable_retry_neutral_android_and_recovery(tmp_path):
    svc, gemini, osm, wiki, clock = pipeline(tmp_path, dict.fromkeys(KEYS,429))
    sid = admit(svc)
    assert await svc.run_once()
    story = svc.story(sid)
    assert story['state'] == 'researching' and story['error'] is None
    assert '429' not in json.dumps(story) and 'private voice' not in json.dumps(story)
    with svc.store.connection() as db:
        job = db.execute('select * from jobs').fetchone()
    assert job['state'] == 'retry' and job['available_at'] == 1120 and job['attempts'] == 1
    assert not await svc.run_once()  # No tight loop; SQLite schedule respected.
    clock.value = 1120
    gemini.failures.clear()
    assert await svc.run_once()
    assert svc.story(sid)['state'] == 'review'
    assert osm.calls == wiki.calls == 1
    assert [op for op, _ in gemini.calls].count('transcription') == 1


@pytest.mark.asyncio
async def test_pipeline_checkpoints_survive_process_restart_and_expired_provider_cache(tmp_path):
    from street_story.service import ProviderBundle, StreetStoryService
    svc, gemini, osm, wiki, clock = pipeline(tmp_path, dict.fromkeys(KEYS,429))
    sid = admit(svc)
    await svc.run_once()
    fresh_gemini = PipelineGemini.build(svc.settings, svc.store, {})
    fresh_gemini.pool.clock = clock
    fresh = StreetStoryService(svc.settings, ProviderBundle(osm, wiki, fresh_gemini, svc.providers.vibepublish))
    fresh.store.now = clock
    clock.value += 8*86400
    assert await fresh.run_once()
    assert fresh.story(sid)['state'] == 'review'
    assert osm.calls == wiki.calls == 1
    assert all(op == 'grounded_research' for op, _ in fresh_gemini.calls)


@pytest.mark.asyncio
async def test_long_exhaustion_adds_neutral_hint_without_changing_android_state(tmp_path):
    svc, _, _, _, clock = pipeline(tmp_path, dict.fromkeys(KEYS,429))
    sid = admit(svc)
    await svc.run_once()
    clock.value += 1801
    story = svc.story(sid)
    assert story['state'] == 'researching' and story['error'] is None
    assert story['processing'] == {'status':'processing_delayed','message':'Обработка займёт немного больше времени','automatic_retry':True}


@pytest.mark.asyncio
async def test_two_concurrent_research_jobs_use_distinct_slots(tmp_path):
    svc, gemini, _, _, _ = pipeline(tmp_path, {})
    admit(svc, 'one')
    admit(svc, 'two')
    entered, release = asyncio.Event(), asyncio.Event()
    original = gemini._generate
    slots = []

    async def generate(key, timeout, contents, config=None):
        if config is not None:
            slots.append(key)
            if len(slots) == 2:
                entered.set()
            await release.wait()
        return await original(key, timeout, contents, config)

    gemini._generate = generate
    tasks = [asyncio.create_task(svc.run_once()) for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 2)
    assert len(set(slots)) == 2
    release.set()
    await asyncio.gather(*tasks)
    assert all(s['state'] == 'review' for s in svc.stories())


def test_json_secret_list_deduplicates_and_is_redacted(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY_REFS', raising=False)
    monkeypatch.setenv('GEMINI_API_KEYS', json.dumps([KEYS[0],KEYS[0],KEYS[1]]))
    cfg = Settings.from_env()
    assert len(cfg.gemini_keys) == 2
    assert all(key not in json.dumps(asdict(cfg), default=str) for key in KEYS)


def test_retry_after_http_date_and_corrupt_values():
    from email.utils import formatdate
    e = ProviderError(429, retry=formatdate(1200, usegmt=True))
    assert classify_error(e, now=1000).retry_after == 200
    for invalid in ('nan','inf','-5','garbage'):
        assert classify_error(ProviderError(429,retry=invalid), now=1000).retry_after is None


@pytest.mark.asyncio
async def test_sdk_uses_single_attempt_bounded_timeout_and_closes_both_clients(tmp_path, monkeypatch):
    from google import genai
    from test_backend import config
    from street_story.providers import GeminiClient
    observations = []

    class Client:
        def __init__(self, **kwargs):
            observations.append(kwargs)
            self.aio = self
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            observations.append('sync_closed')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            observations.append('async_closed')

        async def generate_content(self, **kwargs):
            return SimpleNamespace(text='result')

    monkeypatch.setattr(genai,'Client',Client)
    client = GeminiClient(config(tmp_path))
    assert (await client._provider_request(KEYS[0],.5,['fixture'])).text == 'result'
    assert observations[0]['http_options'].timeout == 500
    assert observations[0]['http_options'].retry_options.attempts == 1
    assert observations[1:] == ['async_closed','sync_closed']
