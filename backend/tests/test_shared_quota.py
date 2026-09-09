import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from test_backend import config
from test_gemini_reliability import KEYS, ProviderError
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient
from street_story.quota import SharedQuotaGate


class Controller:
    def __init__(self):
        self.ids = [str(uuid.uuid4()) for _ in KEYS]
        self.rows = {}
        self.events = []
        self.fail = None
        self.denied = set()

    async def handle(self, request):
        route = request.url.path.rsplit('/',1)[-1]
        payload = json.loads(request.content) if request.content else None
        self.events.append((route,payload))
        if route == self.fail:
            return httpx.Response(503,text='private controller secret')
        if route == 'google_ai_api_keys':
            return httpx.Response(200,json=[{'id':identifier,'is_active':True,'env_var_name':f'GOOGLE_API_KEY{i+1}'} for i,identifier in enumerate(self.ids)])
        if route == 'google_ai_requests':
            uid = request.url.params['request_uid'][3:]
            return httpx.Response(200,json=[self.rows[uid]] if uid in self.rows else [])
        uid = payload['p_request_uid']
        if route == 'google_ai_reserve':
            candidate = payload['p_candidate_key_ids'][0]
            if candidate in self.denied:
                return httpx.Response(200,json={'ok':False,'blocked_reason':'rpm','retry_after_ms':120000})
            self.rows[uid] = {'request_uid':uid,'sent_at':None,'finalized_at':None}
            return httpx.Response(200,json={'ok':True,'api_key_id':candidate})
        if route == 'google_ai_mark_sent':
            self.rows[uid]['sent_at'] = 'sent'
        if route == 'google_ai_finalize':
            self.rows[uid]['finalized_at'] = 'finalized'
        return httpx.Response(204)


@pytest.fixture
def rig(tmp_path,monkeypatch):
    for i,key in enumerate(KEYS):
        monkeypatch.setenv(f'GOOGLE_API_KEY{i+1}',key)
    cfg = replace(config(tmp_path),gemini_api_keys=tuple(SecretStr(k) for k in KEYS),
                  gemini_quota_supabase_url='https://quota.test',gemini_quota_supabase_key='quota-secret')
    c = Controller()
    g = GeminiClient(cfg)
    http = httpx.AsyncClient(transport=httpx.MockTransport(c.handle))
    g.quota = SharedQuotaGate(cfg,g.pool,http=http)
    return g,c


def response():
    return SimpleNamespace(text='transcript',usage_metadata=SimpleNamespace(prompt_token_count=10,candidates_token_count=5,total_token_count=15))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['transcription','grounded_research'])
@pytest.mark.parametrize('failure',['google_ai_api_keys','google_ai_reserve','google_ai_mark_sent','google_ai_requests'])
async def test_controller_outage_never_calls_provider(rig,operation,failure,caplog):
    g,c = rig
    c.fail = failure
    seen = []
    async def provider(*args):
        seen.append(True)
        return response()
    g._provider_request = provider
    with pytest.raises(GeminiUnavailable):
        await g.executor.execute(operation, lambda key, timeout:g._generate(key,timeout,['fixture']))
    assert not seen
    assert 'quota-secret' not in caplog.text and 'private controller secret' not in caplog.text


@pytest.mark.asyncio
async def test_shared_denial_rotates_before_any_provider_call(rig):
    g,c = rig
    c.denied.add(c.ids[0])
    seen = []
    async def provider(key,*args):
        seen.append(key)
        return response()
    g._provider_request = provider
    result = await g.executor.execute('transcription', lambda key,t:g._generate(key,t,['fixture']))
    assert result.text == 'transcript' and seen == [KEYS[1]]
    await g.quota.recover()
    assert len(c.rows) == 1 and all(row['finalized_at'] for row in c.rows.values())
    payload = next(p for name,p in c.events if name == 'google_ai_finalize')
    assert payload['p_usage_total_tokens'] == 15
    assert all(key not in json.dumps(c.events) for key in KEYS)


@pytest.mark.asyncio
async def test_provider_failover_accounts_each_attempt(rig):
    g,c = rig
    seen = []
    async def provider(key,*args):
        seen.append(key)
        assert sum(bool(r['sent_at']) for r in c.rows.values()) == len(seen)
        if len(seen) == 1:
            raise ProviderError(429)
        return response()
    g._provider_request = provider
    await g.executor.execute('grounded_research',lambda key,t:g._generate(key,t,['fixture']))
    await g.quota.recover()
    assert seen == list(KEYS[:2])
    assert len(c.rows) == 2 and all(r['finalized_at'] for r in c.rows.values())
    finals = [p for name,p in c.events if name == 'google_ai_finalize']
    assert finals[0]['p_usage_total_tokens'] is None  # Unknown usage never releases reservation.
    assert finals[0]['p_error_type'] == 'rate_limited'
    assert finals[1]['p_usage_total_tokens'] == 15


@pytest.mark.asyncio
async def test_finalization_outage_preserves_response_blocks_next_call_and_recovers_after_restart(rig):
    g,c = rig
    calls = []
    async def provider(*args):
        calls.append(True)
        return response()
    g._provider_request = provider
    c.fail = 'google_ai_finalize'
    assert (await g._generate(KEYS[0],20,['fixture'])).text == 'transcript'
    with pytest.raises(GeminiUnavailable):
        await g._generate(KEYS[1],20,['fixture'])
    assert len(calls) == 1
    fresh = SharedQuotaGate(g.settings,g.pool,http=g.quota.http)
    c.fail = None
    await fresh.recover()
    assert all(r['finalized_at'] for r in c.rows.values())
    with g.pool.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM gemini_quota_journal').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_missing_configuration_is_not_local_bypass(tmp_path):
    g = GeminiClient(config(tmp_path))
    with pytest.raises(GeminiUnavailable):
        await g._generate(KEYS[0],20,['fixture'])


@pytest.mark.asyncio
async def test_unregistered_keys_never_reach_provider(rig):
    g,c = rig
    g.quota.env_keys = {}
    async def provider(*args):
        pytest.fail('unregistered provider call')
    g._provider_request = provider
    with pytest.raises(GeminiUnavailable):
        await g.executor.execute('transcription',lambda key,t:g._generate(key,t,['fixture']))
    assert not c.rows


@pytest.mark.asyncio
async def test_unknown_reserve_is_not_repeated_and_is_reconciled(rig):
    g,c = rig
    c.fail = 'google_ai_mark_sent'
    with pytest.raises(GeminiUnavailable):
        await g._generate(KEYS[0],20,['fixture'])
    assert len(c.rows) == 1
    c.fail = None
    fresh = SharedQuotaGate(g.settings,g.pool,http=g.quota.http)
    with pytest.raises(GeminiUnavailable):
        await fresh.recover()
    with g.pool.store.tx() as db:
        db.execute('UPDATE gemini_quota_journal SET deadline=0')
    await fresh.recover()
    assert all(row['finalized_at'] for row in c.rows.values())
    assert sum(name=='google_ai_reserve' for name,_ in c.events) == 1


@pytest.mark.asyncio
async def test_cancelled_provider_leaves_durable_accounting(rig):
    import asyncio
    g,c = rig
    entered = asyncio.Event()
    async def provider(*args):
        entered.set()
        await asyncio.Event().wait()
    g._provider_request = provider
    task = asyncio.create_task(g._generate(KEYS[0],20,['fixture']))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fresh = SharedQuotaGate(g.settings,g.pool,http=g.quota.http)
    await fresh.recover()
    assert all(row['finalized_at'] for row in c.rows.values())
    final = next(p for name,p in c.events if name == 'google_ai_finalize')
    assert final['p_error_type'] == 'cancelled' and final['p_usage_total_tokens'] is None


@pytest.mark.asyncio
async def test_malformed_reserve_is_fail_closed(rig):
    g,c = rig
    original = g.quota.rpc
    async def rpc(name,payload):
        if name == 'reserve':
            return {'ok':True,'api_key_id':str(uuid.uuid4())}
        return await original(name,payload)
    g.quota.rpc = rpc
    with pytest.raises(GeminiUnavailable):
        await g._generate(KEYS[0],20,['fixture'])
    assert not any(name=='google_ai_mark_sent' for name,_ in c.events)


@pytest.mark.asyncio
async def test_full_durable_pipeline_uses_shared_gate_without_repeating_stages(tmp_path,monkeypatch):
    from test_gemini_reliability import pipeline, admit
    svc,g,osm,wiki,clock = pipeline(tmp_path,{KEYS[0]:429})
    for i,key in enumerate(KEYS):
        monkeypatch.setenv(f'GOOGLE_API_KEY{i+1}',key)
    cfg = replace(g.settings,gemini_quota_supabase_url='https://quota.test',gemini_quota_supabase_key='quota-secret')
    c = Controller()
    async with httpx.AsyncClient(transport=httpx.MockTransport(c.handle)) as http:
        g.quota = SharedQuotaGate(cfg,g.pool,http=http)
        mock_provider = g._generate
        async def provider(key,t,contents,config):
            return await mock_provider(key,t,contents,config if config.tools else None)
        g._provider_request = provider
        g._generate = GeminiClient._generate.__get__(g)
        sid = admit(svc)
        await svc.run_once()
        assert svc.story(sid)['state'] == 'review'
        await g.quota.recover()
    assert osm.calls == wiki.calls == 1
    assert len(c.rows) >= 2 and all(r['finalized_at'] for r in c.rows.values())
    with svc.store.connection() as db:
        job = db.execute('SELECT state,attempts FROM jobs').fetchone()
        assert job['state'] == 'done' and job['attempts'] == 1
