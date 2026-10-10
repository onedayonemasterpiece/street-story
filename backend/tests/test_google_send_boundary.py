"""Admission accounting and provider dispatch have distinct authoritative states."""
import asyncio

import pytest

from street_story.gemini import GeminiUnavailable
from street_story.identity_plan_diagnostics import provider_outcome
from test_gemini_reliability import KEYS
from test_shared_quota import rig as rig


@pytest.mark.asyncio
async def test_missing_local_admission_sdk_stops_without_key_rotation_or_provider_send(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from street_story.providers import GeminiClient
    from test_backend import config
    monkeypatch.setitem(sys.modules, 'ai_resource_control.client', None)
    client = GeminiClient(config(tmp_path))
    calls = []
    async def unexpected(*args, **kwargs):
        calls.append('provider_or_reservation')
        pytest.fail('Missing admission SDK must stop before reservation or inference')
    client._provider_request = unexpected
    client.quota = SimpleNamespace(run=unexpected)
    with pytest.raises(GeminiUnavailable, match='resource_sdk_unavailable') as caught:
        await client.executor.execute('grounded_research', lambda key, timeout: client._generate(key, timeout, ['fixture']))
    assert calls == []
    assert provider_outcome(caught.value) == ('not_sent', None)


@pytest.mark.asyncio
@pytest.mark.parametrize('controller_step', ['google_ai_api_keys', 'google_ai_reserve',
    'google_ai_mark_sent', 'google_ai_requests'])
async def test_pre_provider_controller_failure_proves_unsent_without_refunding_journal(rig, controller_step):
    client, controller = rig
    controller.fail = controller_step
    calls = []
    async def provider(*args, **kwargs):
        calls.append(True)
        pytest.fail('Failed admission must not reach the SDK')
    client._provider_request = provider
    with pytest.raises(GeminiUnavailable) as caught:
        await client._generate(KEYS[0], 20, ['fixture'])
    assert not calls
    assert provider_outcome(caught.value) == ('not_sent', None)
    assert not any(name == 'google_ai_finalize' for name, _ in controller.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [TimeoutError, asyncio.CancelledError])
async def test_called_sdk_lost_outcome_stays_unknown(rig, failure):
    client, controller = rig
    calls = []
    async def provider(*args, **kwargs):
        calls.append(True)
        raise failure()
    client._provider_request = provider
    with pytest.raises(failure) as caught:
        await client._generate(KEYS[0], 20, ['fixture'])
    assert calls == [True]
    assert provider_outcome(caught.value) == ('unknown', None)
    await client.quota.recover()
    final = next(payload for name, payload in controller.events if name == 'google_ai_finalize')
    assert final['p_usage_total_tokens'] is None


@pytest.mark.asyncio
async def test_before_send_guard_failure_does_not_claim_sdk_dispatch(rig):
    client, controller = rig
    calls = []
    async def provider(*args, **kwargs):
        calls.append(True)
        pytest.fail('Rejected guard cannot reach the SDK')
    def guard():
        raise RuntimeError('fixture_local_guard')
    client._provider_request = provider
    with pytest.raises(RuntimeError) as caught:
        await client._generate(KEYS[0], 20, ['fixture'], before_provider_send=guard)
    assert not calls
    assert provider_outcome(caught.value) == ('not_sent', None)
    assert any(row['sent_at'] for row in controller.rows.values())
