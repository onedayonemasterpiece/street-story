"""Local unsent contention waits/re-admits safely, never exhausts a REF."""
import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from test_parallel_visual_routes import pair, ready, seed


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['RESOURCE_NO_CAPACITY', 'RESOURCE_DAILY_BUDGET', 'RESOURCE_CONTROL_UNAVAILABLE'])
async def test_unsent_primary_waits_then_readmits_same_attempt_after_due(tmp_path, monkeypatch, code):
    adapter, story, calls = ready(tmp_path)
    adapter.native_vision.available = False
    args = pair(story)
    now = adapter.service.store.now()
    original = await seed(adapter, args, 'vision_google_pair', 'created',
        route_failure={'code': code, 'retry_at': now+30, 'observed_at': now})
    args[1]['_visual_pair_resume_only'] = True
    with pytest.raises(RetryableProviderError) as waiting:
        await adapter.visual_pair_route('google', *args)
    assert waiting.value.retry_at == now+30 and not calls
    monkeypatch.setattr(adapter.service.store, 'now', lambda: now+31)
    result = await adapter.visual_pair_route('google', *args)
    assert calls == [('google', 'parent:reference-a')]
    assert result['receipt']['binding']['attempt_id'] == original['binding']['attempt_id']


@pytest.mark.asyncio
async def test_unsent_native_contention_after_closed_primary_is_wait_not_exhaustion(tmp_path, monkeypatch):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    now = adapter.service.store.now()
    await seed(adapter, args, 'vision_google_pair', 'failed', provider_send_state='response_closed', retry_safe=True)
    original = await seed(adapter, args, 'vision_native', 'created',
        route_failure={'code': 'RESOURCE_NO_CAPACITY', 'retry_at': now+30, 'observed_at': now})
    with pytest.raises(RetryableProviderError) as waiting:
        await adapter.visual_pair_route('google', *args)
    assert waiting.value.retry_at == now+30 and not calls
    monkeypatch.setattr(adapter.service.store, 'now', lambda: now+31)
    result = await adapter.visual_pair_route('google', *args)
    assert result['receipt']['binding']['attempt_id'] == original['binding']['attempt_id']
    assert calls == [('native', 'created', None)]


@pytest.mark.asyncio
async def test_all_unsent_routes_wait_earliest_deadline_without_any_send(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    now = adapter.service.store.now()
    await seed(adapter, args, 'vision_google_pair', 'failed', provider_send_state='not_sent', retry_safe=True, retry_at=now+60)
    await seed(adapter, args, 'vision_native', 'created',
        route_failure={'code': 'RESOURCE_NO_CAPACITY', 'retry_at': now+20, 'observed_at': now})
    with pytest.raises(RetryableProviderError) as waiting:
        await adapter.visual_pair_route('google', *args)
    assert waiting.value.retry_at == now+20 and not calls


@pytest.mark.asyncio
async def test_unknown_never_becomes_unsent_when_old_admission_deadline_passes(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'unknown', provider_send_state='possibly_sent',
        route_failure={'code': 'RESOURCE_NO_CAPACITY', 'retry_at': adapter.service.store.now()-10})
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await adapter.visual_pair_route('opencode', *args)
    assert not calls


@pytest.mark.asyncio
async def test_closed_native_rpc_rejection_is_not_transient_unsent_admission(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'failed', provider_send_state='response_closed')
    await seed(adapter, args, 'vision_native', 'failed', provider_send_state='not_sent', retry_safe=True,
               rpc_error={'turn_rejected': True, 'code': -32600})
    with pytest.raises(PermanentProviderError, match='routes_closed'):
        await adapter.visual_pair_route('google', *args)
    assert not calls


@pytest.mark.parametrize('category,expected', [
    ('research_provider_quota', ('google', 'native')),
    ('RESOURCE_DAILY_BUDGET', ('google', 'opencode', 'native')),
])
def test_only_local_daily_legacy_health_can_be_readmitted(tmp_path, category, expected):
    adapter, _, _ = ready(tmp_path)
    adapter.service.store.cache_put('research-quota-health:opencode:mimo-v2.6-flash-free',
                                   {'category': category, 'retry_at': adapter.service.store.now()+900}, 900)
    assert adapter.parallel_visual_routes() == expected
