"""Up to four independent qualified lanes, with normal Native quota boundary."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.native_vision import MODEL, TRANSPORT, VERIFICATION_KEY
from street_story.service import canonical
from test_native_vision import setup as native_setup
from test_parallel_visual_routes import pair, ready, seed


def slots(adapter, healthy):
    pool = SimpleNamespace(snapshot=lambda operation: {'healthy_keys': healthy})
    adapter.primary_vision._verified_routes = lambda: [('qualified-google', pool, None, None)]


@pytest.mark.parametrize('healthy,opencode,native,expected', [
    (6, True, True, ('google', 'opencode', 'google', 'native')),
    (6, True, False, ('google', 'opencode', 'google', 'google')),
    (1, True, True, ('google', 'opencode', 'native')),
    (0, True, True, ('opencode', 'native')),
    (4, False, True, ('google', 'google', 'native', 'google')),
    (0, False, True, ('native',)),
    (0, False, False, ()),
])
def test_four_lanes_only_from_existing_healthy_slots_and_qualified_proofs(tmp_path, healthy, opencode, native, expected):
    adapter, _, _ = ready(tmp_path)
    slots(adapter, healthy)
    adapter.native_vision.available = native
    if not opencode:
        adapter.service.store.cache_put('research-vision-verification-v1', {}, 60)
    assert adapter.parallel_visual_routes() == expected
    assert len(expected) <= 4


@pytest.mark.asyncio
async def test_native_active_pair_uses_existing_provider_and_durable_child(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    result = await adapter.visual_pair_route('native', *args)
    assert calls == [('native', 'created', None)] and result['receipt']['provider'] == 'codex_native'
    assert set(adapter.visual_pair_receipts(args[1], args[3])) == {'vision_native'}


@pytest.mark.asyncio
async def test_native_active_lane_cannot_override_google_unknown_for_same_child(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'unknown', provider_send_state='possibly_sent')
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await adapter.visual_pair_route('native', *args)
    assert not calls


def real_native(adapter, story, tmp_path, monkeypatch):
    provider, client, _, original, context, _, sends, _ = native_setup(tmp_path)
    monkeypatch.setattr(adapter.service.store, 'now', lambda: 1000)
    provider.service = SimpleNamespace(store=adapter.service.store,
        settings=SimpleNamespace(data_dir=tmp_path, native_vision_reserve=True))
    provider.permission.store = adapter.service.store
    provider.checkpoint = adapter.checkpoint
    adapter.native_vision = provider
    adapter.service.store.cache_put(VERIFICATION_KEY, {'model': MODEL, 'transport': TRANSPORT,
        'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}}, 3600)
    original.update(id=story['id'])
    return provider, client, original, context, sends


@pytest.mark.asyncio
async def test_native_active_lane_below_three_percent_never_sends_turn(tmp_path, monkeypatch):
    adapter, story, _ = ready(tmp_path)
    provider, client, original, context, sends = real_native(adapter, story, tmp_path, monkeypatch)
    client.used = 98
    assert 'native' in adapter.parallel_visual_routes()  # Capability is not a quota grant.
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await adapter.visual_pair_route('native', None, original, VERDICT_SCHEMA, canonical(context))
    assert not sends and not any(method == 'turn/start' for method, _ in client.calls)
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1
    assert adapter.visual_pair_receipts(original, context)['vision_native']['phase'] == 'created'


@pytest.mark.asyncio
async def test_native_active_independent_pairs_share_existing_fifteen_minute_grant(tmp_path, monkeypatch):
    adapter, story, _ = ready(tmp_path)
    provider, client, original, context, sends = real_native(adapter, story, tmp_path, monkeypatch)
    for index in range(2):
        reply = deepcopy(context)
        reply['comparison_id'] = f'parent:child-{index}'
        result = await adapter.visual_pair_route('native', None, original, VERDICT_SCHEMA, canonical(reply))
        assert result['receipt']['quota_permission']['expires_at'] == 1900
    assert len(sends) == 2
    assert sum(method == 'turn/start' for method, _ in client.calls) == 2
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1
    await provider.close()
