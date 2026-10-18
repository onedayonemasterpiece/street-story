from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from street_story.db import Store
from street_story.providers import GeminiClient
from test_backend import config


class ProviderError(Exception):
    def __init__(self, code: int):
        super().__init__("provider failure")
        self.code = code
        self.status = "UNAVAILABLE" if code == 503 else ""
        self.response = SimpleNamespace(headers={})
        self.details = {"error": {"code": code}}


@pytest.mark.asyncio
async def test_transcription_fails_over_between_models(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr(""),
        gemini_api_keys=(SecretStr("key-a"), SecretStr("key-b")),
        gemini_transcription_model="gemini-3.5-flash-lite",
        gemini_transcription_fallback_model="gemini-3.1-flash-lite",
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    calls: list[str] = []

    async def generate(
        key,
        timeout,
        contents,
        config=None,
        *,
        operation="grounded_research",
        model=None,
        quota=None,
    ):
        assert operation == "transcription"
        assert model is not None
        calls.append(model)
        if model == "gemini-3.5-flash-lite":
            raise ProviderError(503)
        return SimpleNamespace(text="успешный транскрипт")

    client._generate = generate
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"prepared-audio")

    assert await client.transcribe(audio, "audio/wav") == "успешный транскрипт"
    assert calls == [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    assert client.transcription_routes[0][1].snapshot("transcription")["cooling_down_keys"] == 2
    assert client.transcription_routes[1][1].snapshot("transcription")["healthy_keys"] == 2
