import base64
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from direct_visual_fixture import visual_args, opencode_args
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.reference_image_codec import MODEL_PREPARATION
from test_reference_image_codec import jpeg


def verify_prepared(raw, expected=(960, 1280)):
    with Image.open(BytesIO(raw)) as image:
        assert image.size == expected
        assert image.format == "JPEG"
        assert not image.getexif()
    assert len(raw) <= 480 * 1024


def big_story(story, context):
    source = jpeg((4000, 3000), orientation=6)
    story.update(
        _visual_image_parts=[
            {"label": "SOURCE", "mime_type": "image/jpeg", "data": base64.b64encode(source).decode()},
            {"label": "REF 1", "url": "https://example.org/ref.jpg"},
        ],
        _visual_reference_mapping=context["references"],
    )
    return source


@pytest.mark.asyncio
async def test_google_actual_source_and_ref_are_prepared_before_model_send():
    from test_headless_vision import setup

    provider, _, context, calls, _, _ = setup()
    raw = jpeg((4000, 3000), orientation=6)

    async def load(url):
        return "image/jpeg", raw

    provider._load_public_reference = load
    await provider.compare_visual(*visual_args(raw, {"id": "story"}, VERDICT_SCHEMA, context))
    verify_prepared(calls[0]["contents"][1].inline_data.data)
    verify_prepared(calls[0]["contents"][3].inline_data.data)


@pytest.mark.asyncio
async def test_native_new_prepared_inputs_reconcile_with_marker_without_new_turn(tmp_path):
    from test_native_vision import setup

    provider, client, _, story, context, _, sends, _ = setup(tmp_path)
    raw = big_story(story, context)

    async def load(url):
        return "image/jpeg", raw

    provider.public_image_loader = load
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {"attempt_id": "prepared"})
    receipt = result["receipt"]
    assert receipt["image_preparation"] == receipt["binding"]["image_preparation"] == MODEL_PREPARATION
    for part in client.inputs[receipt["thread_id"]]:
        if part["type"] == "image":
            verify_prepared(base64.b64decode(part["url"].split(",", 1)[1]))
    binding = dict(receipt["binding"])
    binding.update(
        {key: receipt[key] for key in ("phase", "thread_id", "turn_id", "profile_verified", "image_transport", "image_preparation")}
    )
    await provider.compare_visual(None, story, VERDICT_SCHEMA, context, binding)
    assert len(sends) == 1
    assert sum(method == "turn/start" for method, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_native_old_submitted_without_marker_readback_keeps_original_input(tmp_path):
    from test_native_vision import setup

    provider, client, _, story, context, _, sends, _ = setup(tmp_path)
    raw = big_story(story, context)

    async def load(url):
        return "image/jpeg", raw

    provider.public_image_loader = load
    first = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {"attempt_id": "fixture-seed"})
    receipt = first["receipt"]
    # Existing historical turn fixture contains unprepared original inline data.
    for part in client.inputs[receipt["thread_id"]]:
        if part["type"] == "image":
            part["url"] = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    binding = {key: receipt[key] for key in ("phase", "thread_id", "turn_id", "profile_verified", "image_transport")}
    binding["attempt_id"] = "historical"
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, binding)
    assert result["receipt"]["phase"] == "completed"
    assert "image_preparation" not in result["receipt"]
    assert len(sends) == 1
    assert sum(method == "turn/start" for method, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_opencode_source_ref_prepared_inline_and_marker_resumes_same_message():
    from test_opencode_research import Harness

    h = Harness()
    h.result = {"status": "mismatch"}
    raw = jpeg((4000, 3000), orientation=6)

    async def load(url):
        return "image/jpeg", raw

    adapter = h.adapter(public_image_loader=load)
    binding = {"request_id": "prepared-new"}
    args = opencode_args(raw, binding, {"type": "object"})
    first = await adapter.compare_image(*args)
    for part in h.parts:
        if part["type"] == "file":
            verify_prepared(base64.b64decode(part["url"].split(",", 1)[1]))
    receipt = first["receipt"]
    assert receipt["image_preparation"] == receipt["binding"]["image_preparation"] == MODEL_PREPARATION
    resumed = {**binding, **{k: receipt[k] for k in ("session_id", "message_id", "phase", "image_transport", "image_preparation")}}
    await adapter.compare_image(*opencode_args(raw, resumed, {"type": "object"}))
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_identity_suggest_source_prepared_before_generate():
    from street_story import identity_discovery

    raw = jpeg((4000, 3000), orientation=6)
    seen = []

    class Executor:
        async def execute(self, operation, call):
            return await call("fixture", 10)

    async def generate(key, timeout, contents, config, **kwargs):
        seen.append(contents[0].inline_data.data)
        return SimpleNamespace(
            text='{"entity_name":"", "wikipedia_queries":[], "visual_query":"", "commons_query":"", "article_queries":[], "first_wave_hypotheses":[]}'
        )

    gemini = SimpleNamespace(executor=Executor(), _generate=generate, research_routes=[])
    svc = SimpleNamespace(_source_photo_bytes=lambda _: raw, providers=SimpleNamespace(gemini=gemini))
    await identity_discovery.suggest(svc, {"id": "story"}, "", [])
    verify_prepared(seen[0])


@pytest.mark.asyncio
async def test_legacy_photo_research_source_prepared_before_generate(tmp_path):
    from street_story.providers import GeminiClient
    from street_story.db import Store
    from test_backend import config

    client = GeminiClient(config(tmp_path), Store(tmp_path / "model-prep-db"))
    raw = jpeg((4000, 3000), orientation=6)
    seen = []

    class Executor:
        async def execute(self, operation, call):
            return await call("fixture", 10)

    async def generate(key, timeout, contents, configuration, **kwargs):
        seen.append(contents[0].inline_data.data)
        return SimpleNamespace(text='{"place_name":"", "summary":"", "draft_text":"", "facts":[]}', candidates=[])

    client.research_routes = [("fixture", None, None, Executor())]
    client._generate = generate
    await client.research(raw, "image/jpeg", "", {}, [], [])
    verify_prepared(seen[0])


@pytest.mark.asyncio
async def test_adapter_carries_preparation_marker_for_durable_attempt_and_native_readback(tmp_path):
    from street_story.research_adapter import ProductResearchAdapter
    from test_research_control import fixture

    svc, sid, _ = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = svc
    story = {"id": sid, "_identity_generation": 0}
    unit = ["prepared-comparison", "reference-1"]
    binding, _ = adapter.attempt(story, "vision_native", unit)
    await adapter.checkpoint(
        binding,
        {
            "binding": binding,
            "phase": "submitted",
            "thread_id": "thread-old",
            "turn_id": "turn-old",
            "image_transport": "inline_data_uri_v1",
            "image_preparation": MODEL_PREPARATION,
        },
    )
    resumed, _ = adapter.attempt(story, "vision_native", unit)
    assert resumed["image_preparation"] == MODEL_PREPARATION
    assert resumed["turn_id"] == "turn-old"
    readback = adapter._native_visual_readback_binding(story, unit)
    assert readback["image_preparation"] == MODEL_PREPARATION
    assert readback["turn_id"] == "turn-old"


@pytest.mark.asyncio
async def test_prepared_opencode_readback_succeeds_despite_reference_url_unavailable():
    from test_opencode_research import Harness, sheet

    h = Harness()
    h.result = {"status": "mismatch"}
    adapter = h.adapter()
    original = {"request_id": "prepared-readback"}
    first = await adapter.compare_image(*opencode_args(sheet(), original, {"type": "object"}))
    receipt = first["receipt"]
    binding = {**original, **{k: receipt[k] for k in ("session_id", "message_id", "image_transport", "image_preparation")}}
    binding["phase"] = "submitted"

    async def broken(url):
        raise TimeoutError("readback REF unavailable")

    adapter.public_image_loader = broken
    before_admissions = len(h.admissions)
    result = await adapter.compare_image(*opencode_args(sheet(), binding, {"type": "object"}))
    assert result["receipt"]["phase"] == "completed"
    assert result["receipt"]["readback_only"] is True
    assert result["receipt"]["image_preparation"] == MODEL_PREPARATION
    assert len(h.admissions) == before_admissions
    assert len(h.sends) == 1
