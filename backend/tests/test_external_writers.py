import json
from types import SimpleNamespace

import pytest

from street_story import external_writers
from street_story.providers import GeminiClient

ANSWER = json.dumps({"text": "Рассказ о доме.\n\nУлица 1\n#калининград", "image_notes": ["До 1945 года: пансион"],
                     "image_note_arrows": [""]}, ensure_ascii=False)


def test_answer_is_found_inside_client_banners():
    raw = "\x1b[0m\n> build · glm-5.3-flash-free\n\x1b[0m\n" + ANSWER + "\n"
    assert external_writers.parse_answer(raw)["text"].startswith("Рассказ о доме.")
    with pytest.raises(external_writers.ExternalWriterUnavailable):
        external_writers.parse_answer("Error: Upstream request failed")


@pytest.mark.asyncio
async def test_glm_three_times_then_sol_and_glm_stays_first(monkeypatch):
    # Owner decision 20.10.2026: GLM is the main writer; Sol only after several GLM failures.
    calls = []
    outcomes = iter(["fail", "fail", "fail", "fail", "ok"])

    async def glm(prompt, *, answer_instruction=external_writers.ANSWER):
        calls.append("glm")
        if next(outcomes) == "fail":
            raise external_writers.ExternalWriterUnavailable("writer_timeout")
        return json.loads(ANSWER)

    async def sol(prompt, photo, *, answer_instruction=external_writers.ANSWER):
        calls.append("sol")
        return json.loads(ANSWER)

    monkeypatch.setattr(external_writers, "glm", glm)
    monkeypatch.setattr(external_writers, "sol", sol)
    assert (await external_writers.write("prompt", b"jpeg"))[1] == external_writers.SOL_MODEL
    assert (await external_writers.write("prompt", b"jpeg"))[1] == external_writers.GLM_MODEL
    assert calls == ["glm", "glm", "glm", "sol", "glm", "glm"]


@pytest.mark.asyncio
async def test_writer_uses_external_first_and_gemini_when_both_fail(monkeypatch):
    async def external(prompt, photo):
        return json.loads(ANSWER), external_writers.GLM_MODEL

    monkeypatch.setattr(external_writers, "write", external)
    client = SimpleNamespace(settings=SimpleNamespace(external_writers=True), WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, writer_routes=[])
    written = await GeminiClient.write_caption(client, owner_request="Напиши", current_text="", concept="",
        facts=[{"text": "Факт."}], place={}, literal_fragments=[], photo_jpeg=None)
    assert written["model"] == "GLM 5.3 Flash" and written["text"].endswith("#Калининград")
    assert written["image_note_arrows"] == [""]

    async def unavailable(prompt, photo):
        raise external_writers.ExternalWriterUnavailable("writer_limit")

    monkeypatch.setattr(external_writers, "write", unavailable)
    from street_story.gemini import GeminiUnavailable
    with pytest.raises(GeminiUnavailable):  # falls through to the (empty) Gemini ladder
        await GeminiClient.write_caption(client, owner_request="Напиши", current_text="", concept="",
            facts=[{"text": "Факт."}], place={}, literal_fragments=[], photo_jpeg=None)
