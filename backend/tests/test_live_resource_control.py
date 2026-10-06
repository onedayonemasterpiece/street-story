from __future__ import annotations

import sys
import types
import asyncio
import json

import pytest

from street_story.live import _live_resource_environment, create_live_host
from test_live_editor import make_service, settings


@pytest.mark.asyncio
async def test_live_host_uses_central_shared_resource_controller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_RESOURCE_CONTROL_URL", "https://limiter.example")
    monkeypatch.setenv("AI_RESOURCE_CONTROL_SERVICE_KEY", "fixture-service-key")
    monkeypatch.setenv("AI_RESOURCE_LEDGER_ID", "ledger-fixture")
    monkeypatch.setenv("AI_RESOURCE_KEY_ENVS", "GOOGLE_API_KEY,GOOGLE_API_KEY2")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-one")
    monkeypatch.setenv("GOOGLE_API_KEY2", "fixture-two")
    monkeypatch.setenv("GOOGLE_API_KEY3", "fixture-street-fallback")
    monkeypatch.setenv("LIVE_API_KEY", "must-not-be-used")

    service, _adapter, _session, _events = make_service(tmp_path)
    host = create_live_host(service, settings(tmp_path))
    from live_interaction.provider import TRANSITION_DEADLINE_SECONDS, FRESH_HANDLE_WAIT_SECONDS
    assert host.reconfigure_timeout_ms >= (TRANSITION_DEADLINE_SECONDS + FRESH_HANDLE_WAIT_SECONDS + 10) * 1000
    calls: list[dict] = []

    async def guarded(**kwargs):
        calls.append({**kwargs, "environment": dict(kwargs["environment"])})

    ai_resource_control = pytest.importorskip('ai_resource_control', reason='Private managed SDK is installed by server deployment; verified in retained runtime acceptance')
    monkeypatch.setattr(ai_resource_control, 'run_guarded', guarded)
    initialized = _adapter.initialize(resource_id=_session.resource_id, actor=None, model='gemini-3.8-live')
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps({'type': 'start', 'model': 'gemini-3.8-live',
        'context': initialized['context'], 'configuration': initialized['configuration']}) + '\n').encode())
    await host.managed_runner(
        session=types.SimpleNamespace(id="live_fixture_session"),
        reader=reader,
        on_event=lambda _event: None,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["consumer"] == "street-story"
    assert call["binding"] == "street-story:live_fixture_session"
    from live_interaction.provider import setup_config
    from ai_resource_control.client import estimate_input_tokens
    assert call['control'].config.grant_tokens == estimate_input_tokens(setup_config('gemini-3.8-live',
        initialized['context'], configuration=initialized['configuration'], search=False))
    assert 1024 < call['control'].config.grant_tokens < 20000
    assert call["environment"] == {
        "AI_RESOURCE_CONTROL_URL": "https://limiter.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "fixture-service-key",
        "AI_RESOURCE_LEDGER_ID": "ledger-fixture",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "fixture-street-fallback",
    }
    assert not any(name.startswith("GOOGLE_API_KEY") for name in call["environment"])
    assert "LIVE_API_KEY" not in call["environment"]
    assert host.managed_runner is not None


@pytest.mark.asyncio
async def test_missing_resource_package_never_falls_back_to_direct_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LIVE_API_KEY", "fixture-direct-key")
    service, _adapter, _session, _events = make_service(tmp_path)
    host = create_live_host(service, settings(tmp_path))
    events: list[dict] = []

    monkeypatch.setitem(sys.modules, "ai_resource_control", None)
    await host.managed_runner(
        session=types.SimpleNamespace(id="live_missing_resource"),
        reader=object(),
        on_event=events.append,
    )

    assert events == [
        {
            "type": "error",
            "code": "RESOURCE_PACKAGE_MISSING",
            "message": "RESOURCE_PACKAGE_MISSING",
        }
    ]


def test_live_resource_environment_is_central_and_bounded(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_RESOURCE_CONTROL_URL", "https://limiter.example")
    monkeypatch.setenv("AI_RESOURCE_CONTROL_SERVICE_KEY", "fixture-service-key")
    monkeypatch.setenv("AI_RESOURCE_KEY_ENVS", "GOOGLE_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-one")
    monkeypatch.setenv("GOOGLE_API_KEY3", "fixture-street-fallback")
    monkeypatch.setenv("VIBEPUBLISH_BEARER_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("STREET_STORY_DEVICE_TOKEN", "must-not-be-forwarded")

    environment = _live_resource_environment(settings(tmp_path))

    assert environment == {
        "AI_RESOURCE_CONTROL_URL": "https://limiter.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "fixture-service-key",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "fixture-street-fallback",
    }


def test_live_resource_environment_accepts_compatibility_aliases(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AI_RESOURCE_CONTROL_URL", raising=False)
    monkeypatch.delenv("AI_RESOURCE_CONTROL_SERVICE_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "https://limiter.example")
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "fixture-service-key")
    monkeypatch.setenv("GOOGLE_API_KEY3", "fixture-street-fallback")

    assert _live_resource_environment(settings(tmp_path)) == {
        "AI_RESOURCE_CONTROL_URL": "https://limiter.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "fixture-service-key",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "fixture-street-fallback",
    }
