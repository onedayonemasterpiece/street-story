from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr


def secret(value: str | SecretStr | None) -> SecretStr:
    return value if isinstance(value, SecretStr) else SecretStr(value or '')


def reveal(value: str | SecretStr | None) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value or ''


def _json_list(name: str) -> list[str]:
    try:
        data = json.loads(os.environ[name])
        if not isinstance(data, list) or not 1 <= len(data) <= 32 or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in data):
            raise ValueError
        return [v.strip() for v in data]
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be a nonempty JSON list of at most 32 nonempty strings') from None


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f'{name} must be a finite number between {minimum} and {maximum}') from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    device_token: SecretStr | str = field(repr=False)
    gemini_api_key: SecretStr | str | None = field(repr=False)
    gemini_model: str
    vibepublish_base_url: str | None
    vibepublish_bearer_token: SecretStr | str | None = field(repr=False)
    osm_user_agent: str
    worker_poll_seconds: float = 1.0
    gemini_api_keys: tuple[SecretStr, ...] = field(default=(), repr=False)
    gemini_key_refs: tuple[str, ...] = ()
    gemini_quota_supabase_url: str | None = None
    gemini_quota_supabase_key: SecretStr | str | None = field(default=None, repr=False)
    gemini_call_timeout_seconds: float = 20.0
    gemini_attempt_timeout_seconds: float = 60.0
    gemini_transcription_rpm: int = 0
    gemini_grounded_research_rpm: int = 0
    processing_delayed_after_seconds: float = 1800.0

    def __post_init__(self):
        for name in ('device_token', 'gemini_api_key', 'vibepublish_bearer_token', 'gemini_quota_supabase_key'):
            object.__setattr__(self, name, secret(getattr(self, name)))
        object.__setattr__(self, 'gemini_api_keys', tuple(secret(k) for k in self.gemini_api_keys))

    @property
    def gemini_keys(self) -> tuple[SecretStr, ...]:
        values = self.gemini_api_keys or ((secret(self.gemini_api_key),) if reveal(self.gemini_api_key) else ())
        return tuple(dict.fromkeys(values))

    def redact(self, message: str) -> str:
        for value in (*self.gemini_keys, self.gemini_api_key, self.device_token, self.vibepublish_bearer_token, self.gemini_quota_supabase_key):
            raw = reveal(value)
            if raw:
                message = message.replace(raw, '[redacted]')
        message = re.sub(r'(?i)(Bearer\s+|(?:key|api_key|access_token)=)[^\s&\"\']+', r'\1[redacted]', message)
        return message[:400]

    @classmethod
    def from_env(cls) -> Settings:
        keys: list[str] = []
        refs: list[str] = []
        if os.getenv('GEMINI_API_KEYS') and os.getenv('GEMINI_API_KEY_REFS'):
            raise ValueError('Configure only one of GEMINI_API_KEYS and GEMINI_API_KEY_REFS')
        if os.getenv('GEMINI_API_KEYS'):
            keys = _json_list('GEMINI_API_KEYS')
        elif os.getenv('GEMINI_API_KEY_REFS'):
            refs = _json_list('GEMINI_API_KEY_REFS')
            for ref in refs:
                if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', ref):
                    raise ValueError('GEMINI_API_KEY_REFS contains an invalid environment variable name')
                value = os.getenv(ref, '').strip()
                if not value:
                    raise ValueError('A configured GEMINI_API_KEY_REFS entry is unset or empty')
                keys.append(value)
        return cls(
            data_dir=Path(os.getenv('DATA_DIR', './data')).expanduser().resolve(),
            device_token=os.getenv('STREET_STORY_DEVICE_TOKEN', '').strip(),
            gemini_api_key=os.getenv('GEMINI_API_KEY', '').strip(),
            gemini_api_keys=tuple(SecretStr(k) for k in dict.fromkeys(keys)),
            gemini_key_refs=tuple(refs),
            gemini_quota_supabase_url=os.getenv('GEMINI_QUOTA_SUPABASE_URL', '').rstrip('/') or None,
            gemini_quota_supabase_key=os.getenv('GEMINI_QUOTA_SUPABASE_KEY'),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-3.1-flash-lite'),
            vibepublish_base_url=os.getenv('VIBEPUBLISH_BASE_URL', '').rstrip('/') or None,
            vibepublish_bearer_token=os.getenv('VIBEPUBLISH_BEARER_TOKEN'),
            osm_user_agent=os.getenv('STREET_STORY_OSM_USER_AGENT', 'StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)'),
            worker_poll_seconds=_number('WORKER_POLL_SECONDS', 1, .01, 60),
            gemini_call_timeout_seconds=_number('GEMINI_CALL_TIMEOUT_SECONDS', 20, .1, 120),
            gemini_attempt_timeout_seconds=_number('GEMINI_ATTEMPT_TIMEOUT_SECONDS', 60, .1, 300),
            gemini_transcription_rpm=int(_number('GEMINI_TRANSCRIPTION_RPM', 0, 0, 10000)),
            gemini_grounded_research_rpm=int(_number('GEMINI_GROUNDED_RESEARCH_RPM', 0, 0, 10000)),
            processing_delayed_after_seconds=_number('PROCESSING_DELAYED_AFTER_SECONDS', 1800, 1, 86400),
        )
