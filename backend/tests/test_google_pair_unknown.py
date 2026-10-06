from direct_visual_fixture import visual_args
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.gemini import GeminiExecutor, GeminiPolicy, GeminiUnavailable
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.research_adapter import ProductResearchAdapter
from test_headless_vision import setup
from test_reference_image_codec import jpeg
from test_research_control import fixture


@pytest.mark.asyncio
async def test_unsent_google_refusal_can_recover_after_route_returns(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = service
    calls = []
    async def compare(*args):
        calls.append('sent')
        return {'result':{'status':'mismatch'}, 'receipt':{'provider':'google'}}
    adapter.primary_vision = SimpleNamespace(available=False, compare_visual=compare)
    story = {'id':sid, 'photo_sha256':photo}
    _, story, _, context = visual_args(b'raw', story, {}, {'references': [{'candidate_id': 'wiki:1'}]})
    context = json.dumps(context)
    with pytest.raises(GeminiUnavailable):
        await adapter._google_visual_verdict(b'pixels', story, {}, context, grouped=False)
    assert not calls
    adapter.primary_vision.available = True
    result = await adapter._google_visual_verdict(b'pixels', story, {}, context, grouped=False)
    assert result['result']['status'] == 'mismatch' and calls == ['sent']
    with service.store.connection() as db:
        records = [json.loads(row[0]) for row in db.execute(
            "SELECT receipt_json FROM research_provider_attempts WHERE role='vision_google_pair' ORDER BY created_at,rowid")]
    assert [record['phase'] for record in records] == ['failed','completed']
    assert records[0]['provider_send_state'] == 'not_sent'


def ready(tmp_path, primary):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.primary_vision, adapter.client = service, primary, None
    native_calls = []
    async def native(snapshot, story, schema, context, binding):
        native_calls.append(binding)
        result = {'status': 'uncertain'}
        receipt = {'provider': 'codex_native', 'phase': 'completed', 'binding': binding, 'result': result}
        await adapter.checkpoint(binding, receipt)
        return {'result': result, 'receipt': receipt}
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    story = {'id': sid, 'photo_sha256': photo}
    return adapter, service, story, native_calls


@pytest.mark.asyncio
async def test_pair_timeout_sends_once_blocks_other_google_keys_models_and_native(tmp_path):
    primary, verdict, context, calls, first, second = setup()
    class Pool:
        policy = GeminiPolicy()
        keys = (SimpleNamespace(get_secret_value=lambda: 'test-key'),) * 2
        ids = ('key0', 'key1')
        finished = []
        def reserve(self, operation, excluded, timeout):
            return next((key for key in self.ids if key not in excluded), None)
        def finish(self, key, operation, failure):
            self.finished.append(key)
        def event(self, *args, **kwargs):
            pass
        def clock(self):
            return 1000
        def all_enabled_keys_blocked_for_model(self, operation):
            return False
        def unavailable(self, operation):
            return GeminiUnavailable(1200, 'all_keys_unavailable')
    pool = Pool()
    primary.client.research_routes[0] = ('gemini-primary', pool, 'test-quota', GeminiExecutor(pool))
    async def timeout(*args, **kwargs):
        kwargs['before_provider_send']()
        calls.append('sent')
        raise TimeoutError
    primary.client._generate = timeout
    adapter, service, story, native_calls = ready(tmp_path, primary)
    for index in range(2):
        if index:
            primary.service.store.cache_get = lambda key: {}
        with pytest.raises(RetryableProviderError, match='pair_outcome_unknown'):
            await adapter.visual_verdict(*visual_args(jpeg(), story, VERDICT_SCHEMA, json.dumps(context)))
    assert calls == ['sent'] and not native_calls and not second.operations
    assert pool.finished == ['key0'] and pool.policy.max_failover_keys == 32
    with service.store.connection() as db:
        rows = list(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND role='vision_google_pair'", (story['id'],)))
    assert len(rows) == 1
    receipt = json.loads(rows[0]['receipt_json'])
    assert receipt['phase'] == 'unknown' and receipt['provider_send_state'] == 'possibly_sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['known_unsent', 'closed_malformed'])
async def test_known_google_failure_uses_native_without_repeating_inference(tmp_path, kind):
    google_calls = []
    async def compare(*args):
        google_calls.append('route_entry')
        error = GeminiUnavailable(1200, 'all_keys_unavailable')
        error.receipt = {'model_attempts': [{'category': 'rate_limited', 'provider_send_state': 'not_sent'}]} if kind == 'known_unsent' else {
            'model_attempts': [{'category': 'malformed_response', 'provider_request_id': 'closed-response',
                                'usage': {'total_tokens': 8559}}]}
        raise error
    adapter, service, story, native_calls = ready(tmp_path, SimpleNamespace(available=True, compare_visual=compare))
    context = json.dumps({'references': [{'candidate_id': 'wiki:1'}]})
    for _ in range(2):
        response = await adapter.visual_verdict(*visual_args(jpeg(), story, VERDICT_SCHEMA, context))
        assert response['result']['status'] == 'uncertain'
    expected_entries = 2 if kind == 'known_unsent' else 1
    assert google_calls == ['route_entry'] * expected_entries and len(native_calls) == 1
    with service.store.connection() as db:
        rows = list(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND role='vision_google_pair'", (story['id'],)))
    assert len(rows) == expected_entries
    receipt = json.loads(rows[0]['receipt_json'])
    assert receipt['phase'] == 'failed' and receipt['fallback_mode'] == 'native'
    assert receipt['retry_safe'] is True
    assert receipt['provider_send_state'] == ('not_sent' if kind == 'known_unsent' else 'response_closed')


@pytest.mark.asyncio
async def test_real_generate_quota_denial_never_crosses_provider_send_boundary_and_uses_native(tmp_path):
    from types import MethodType
    from street_story.providers import GeminiClient
    primary, verdict, context, calls, first, second = setup()
    quota_calls, provider_calls = [], []
    class Quota:
        async def run(self, key, timeout, size, invoke):
            quota_calls.append('refused')
            raise GeminiUnavailable(1200, 'shared_control_unavailable')
    async def provider_request(*args, **kwargs):
        provider_calls.append('sent')
        raise AssertionError('admission denial must precede send')
    primary.client._generate = MethodType(GeminiClient._generate, primary.client)
    primary.client._provider_request = provider_request
    primary.client.research_routes = [('gemini-primary', None, Quota(), first)]
    adapter, service, story, native_calls = ready(tmp_path, primary)
    response = await adapter.visual_verdict(*visual_args(jpeg(), story, VERDICT_SCHEMA, json.dumps(context)))
    assert response['receipt']['provider'] == 'codex_native'
    assert quota_calls == ['refused'] and not provider_calls and len(native_calls) == 1
    with service.store.connection() as db:
        row = db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND role='vision_google_pair'", (story['id'],)).fetchone()
    receipt = json.loads(row['receipt_json'])
    assert receipt['phase'] == 'failed' and receipt['provider_send_state'] == 'not_sent'
    assert receipt['observation']['model_attempts'][0]['provider_send_state'] == 'not_sent'
