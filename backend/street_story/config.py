from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    device_token: str
    gemini_api_key: str | None
    gemini_model: str
    vibepublish_base_url: str | None
    vibepublish_bearer_token: str | None
    osm_user_agent: str
    worker_poll_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            device_token=os.getenv("STREET_STORY_DEVICE_TOKEN", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            vibepublish_base_url=(os.getenv("VIBEPUBLISH_BASE_URL") or "").rstrip("/") or None,
            vibepublish_bearer_token=os.getenv("VIBEPUBLISH_BEARER_TOKEN") or None,
            osm_user_agent=os.getenv(
                "STREET_STORY_OSM_USER_AGENT",
                "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)",
            ),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1")),
        )
