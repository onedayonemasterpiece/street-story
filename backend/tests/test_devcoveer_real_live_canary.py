from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "devcoveer_real_live_canary.py"


def load_module():
    spec = importlib.util.spec_from_file_location("devcoveer_real_live_canary", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tiny_png_is_stable_valid_png_shape() -> None:
    module = load_module()
    payload = module.tiny_png()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in payload and b"IDAT" in payload and payload.endswith(b"IEND\xaeB\x60\x82")


def test_existing_device_token_never_mints(tmp_path: Path) -> None:
    module = load_module()
    path = tmp_path / "device-token.txt"
    path.write_text("x" * 40, encoding="utf-8")
    path.chmod(0o600)
    installer = SimpleNamespace(
        DEVICE_TOKEN_FILE=path,
        require_mode=lambda actual, mode: None,
        device_token=lambda: ("x" * 40, False),
    )
    assert module.existing_device_token(installer) == "x" * 40


def test_event_summary_accepts_real_central_live_turn() -> None:
    module = load_module()
    events = [
        {"type": "ready"},
        {"type": "input_timing"},
        {"type": "output_transcript", "text": "Готово"},
        {"type": "audio", "data": "opaque"},
        {"type": "turn_complete"},
    ]
    summary = module.event_summary(events)
    module.validate_central_live(summary)
    assert summary["resource_fallback"] is False
    assert summary["output_transcript_chars"] == len("Готово")


@pytest.mark.parametrize(
    ("events", "code"),
    [
        (
            [
                {"type": "resource_fallback", "reason": "authority_unavailable"},
                {"type": "ready"},
                {"type": "output_transcript", "text": "Готово"},
                {"type": "turn_complete"},
            ],
            "central_authority_fell_back",
        ),
        ([{"type": "error", "code": "RESOURCE_CAPACITY"}], "live_provider_error"),
        ([{"type": "output_transcript", "text": "x"}, {"type": "turn_complete"}], "live_ready_missing"),
        ([{"type": "ready"}, {"type": "turn_complete"}], "live_output_missing"),
        ([{"type": "ready"}, {"type": "audio", "data": "x"}], "live_turn_incomplete"),
    ],
)
def test_central_live_validation_fails_closed(events, code) -> None:
    module = load_module()
    with pytest.raises(module.LiveCanaryError, match=code):
        module.validate_central_live(module.event_summary(events))
