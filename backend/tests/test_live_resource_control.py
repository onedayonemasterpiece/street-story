from __future__ import annotations

import sys
import types

import pytest

from street_story.live import _live_resource_environment, create_live_host
from test_live_editor import make_service, settings


@pytest.mark.asyncio
async def test_live_host_uses_shared_resource_controller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "https://limiter.example")
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "fixture-service-key")
    monkeypatch.setenv("AI_RESOURCE_KEY_ENVS", "GOOGLE_API_KEY,GOOGLE_API_KEY2")
    monkeypatch.setenv("AI_RESOURCE_LEDGER_ID", "ledger-fixture")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-one")
    monkeypatch.setenv("GOOGLE_API_KEY2", "fixture-two")
    monkeypatch.setenv("LIVE_API_KEY", "must-not-be-used")

    service, _adapter, _session, _events = make_service(tmp_path)
    host = create_live_host(service, settings(tmp_path))
    calls: list[dict] = []

    async def guarded(**kwargs):
        calls.append({**kwargs, "environment": dict(kwargs["environment"])})

    monkeypatch.setitem(sys.modules, "ai_resource_control", types.SimpleNamespace(run_guarded=guarded))
    await host.managed_runner(
        session=types.SimpleNamespace(id="live_fixture_session"),
        reader=object(),
        on_event=lambda _event: None,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["consumer"] == "street-story"
    assert call["binding"] == "street-story:live_fixture_session"
    assert call["environment"]["AI_RESOURCE_KEY_ENVS"] == "GOOGLE_API_KEY,GOOGLE_API_KEY2"
    assert call["environment"]["GOOGLE_API_KEY"] == "fixture-one"
    assert call["environment"]["GOOGLE_API_KEY2"] == "fixture-two"
    assert call["environment"]["AI_RESOURCE_LEDGER_ID"] == "ledger-fixture"
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


def test_live_resource_environment_is_bounded(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "https://limiter.example")
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "fixture-service-key")
    monkeypatch.setenv("AI_RESOURCE_KEY_ENVS", "GOOGLE_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-one")
    monkeypatch.setenv("VIBEPUBLISH_BEARER_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("STREET_STORY_DEVICE_TOKEN", "must-not-be-forwarded")

    environment = _live_resource_environment(settings(tmp_path))

    assert environment == {
        "GOOGLE_AI_LIMITER_SUPABASE_URL": "https://limiter.example",
        "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY": "fixture-service-key",
        "AI_RESOURCE_KEY_ENVS": "GOOGLE_API_KEY",
        "GOOGLE_API_KEY": "fixture-one",
    }
