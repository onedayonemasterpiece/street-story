import pytest

from live_interaction import LiveError
from street_story.app import LIVE_PROVIDER_AUDIO_CHARS, live_input_messages


def test_live_http_audio_batch_is_split_into_ordered_provider_chunks():
    audio = "A" * 32_768
    parts = live_input_messages({"audio_base64": audio})
    assert [len(item["audio_base64"]) for item in parts] == [16_000, 16_000, 768]
    assert all(len(item["audio_base64"]) <= LIVE_PROVIDER_AUDIO_CHARS for item in parts)
    assert "".join(item["audio_base64"] for item in parts) == audio


def test_small_live_audio_message_keeps_the_existing_shared_host_path():
    message = {"audio_base64": "A" * 10_924}
    assert live_input_messages(message) == [message]


@pytest.mark.parametrize(
    "message",
    [
        {"audio_base64": "A" * 32_768, "activity_end": True},
        {"audio_base64": "A" * 48_004},
        {"audio_base64": "A" * 32_767},
    ],
)
def test_oversized_live_audio_envelope_fails_closed_when_not_safely_splittable(message):
    with pytest.raises(LiveError) as exc:
        live_input_messages(message)
    assert exc.value.code == "INVALID_ARGUMENT"
