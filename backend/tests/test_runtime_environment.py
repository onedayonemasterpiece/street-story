from __future__ import annotations

from street_story.config import Settings, reveal
from street_story.providers import GeminiClient


def _clear_runtime_env(monkeypatch) -> None:
    for name in (
        "GEMINI_API_KEYS",
        "GEMINI_API_KEY_REFS",
        "GEMINI_API_KEY",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_TRANSCRIPTION_MODEL",
        "GEMINI_TRANSCRIPTION_FALLBACK_MODEL",
        "GEMINI_QUOTA_SUPABASE_URL",
        "GEMINI_QUOTA_SUPABASE_KEY",
        "GOOGLE_AI_LIMITER_SUPABASE_URL",
        "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for ordinal in range(1, 33):
        suffix = "" if ordinal == 1 else str(ordinal)
        monkeypatch.delenv(f"GOOGLE_API_KEY{suffix}", raising=False)


def test_shared_devcoveer_google_environment_is_discovered(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_API_KEY3", "key-three")
    monkeypatch.setenv("GOOGLE_API_KEY", "key-one")
    monkeypatch.setenv("GOOGLE_API_KEY2", "key-two")
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "https://quota.example/")
    monkeypatch.setenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "quota-key")

    settings = Settings.from_env()

    assert settings.gemini_key_refs == (
        "GOOGLE_API_KEY",
        "GOOGLE_API_KEY2",
        "GOOGLE_API_KEY3",
    )
    assert [reveal(value) for value in settings.gemini_keys] == [
        "key-one",
        "key-two",
        "key-three",
    ]
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.gemini_fallback_model == "gemini-3.1-flash-lite"
    assert settings.gemini_transcription_model == "gemini-3.5-flash-lite"
    assert settings.gemini_transcription_fallback_model == "gemini-3.1-flash-lite"
    client = GeminiClient(settings)
    assert client.pool.model == "gemini-3.5-flash-lite"
    assert client.transcription_pool.model == "gemini-3.5-flash-lite"
    assert [route[0] for route in client.transcription_routes] == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    assert [route[0] for route in client.research_routes] == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    assert settings.gemini_quota_supabase_url == "https://quota.example"
    assert reveal(settings.gemini_quota_supabase_key) == "quota-key"


def test_explicit_gemini_refs_override_shared_discovery(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_API_KEY", "shared-key")
    monkeypatch.setenv("PRIVATE_GEMINI_KEY", "explicit-key")
    monkeypatch.setenv("GEMINI_API_KEY_REFS", '["PRIVATE_GEMINI_KEY"]')

    settings = Settings.from_env()

    assert settings.gemini_key_refs == ("PRIVATE_GEMINI_KEY",)
    assert [reveal(value) for value in settings.gemini_keys] == ["explicit-key"]
