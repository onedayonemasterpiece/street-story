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
async def test_sol_first_then_glm_on_failure_or_invalid_output(monkeypatch):
    calls = []
    glm_outcomes = iter(["fail", "fail", "ok"])

    async def sol(prompt, photo, *, answer_instruction=external_writers.ANSWER):
        calls.append("sol")
        if len(calls) > 1:
            raise external_writers.ExternalWriterUnavailable("writer_limit")
        return json.loads(ANSWER)

    async def glm(prompt, *, answer_instruction=external_writers.ANSWER):
        calls.append("glm")
        if next(glm_outcomes) == "fail":
            raise external_writers.ExternalWriterUnavailable("writer_timeout")
        return json.loads(ANSWER)

    monkeypatch.setattr(external_writers, "glm", glm)
    monkeypatch.setattr(external_writers, "sol", sol)
    assert (await external_writers.write("prompt", b"jpeg"))[1] == external_writers.SOL_MODEL
    assert (await external_writers.write("prompt", b"jpeg"))[1] == external_writers.GLM_MODEL
    assert calls == ["sol", "sol", "glm", "glm", "glm"]


@pytest.mark.asyncio
async def test_invalid_sol_card_notes_falls_through_to_glm(monkeypatch):
    calls = []

    async def sol(prompt, photo, *, answer_instruction=external_writers.ANSWER):
        calls.append("sol")
        return {"text": "План", "card_notes": []}

    async def glm(prompt, *, answer_instruction=external_writers.ANSWER):
        calls.append("glm")
        return {"text": "План", "card_notes": [
            {"notes": ["Театр", "В 1910 году открылся театр", "листай →"],
             "arrows": ["", "", ""]},
            {"notes": ["В 1920 году здание перестроили"], "arrows": [""]},
            {"notes": [], "arrows": []},
        ]}

    def validate(answer):
        if len(answer["card_notes"]) != 3:
            raise ValueError("incomplete cards")

    monkeypatch.setattr(external_writers, "glm", glm)
    monkeypatch.setattr(external_writers, "sol", sol)
    plan, model = await external_writers.write("comic", None, validate=validate)
    assert model == external_writers.GLM_MODEL and len(plan["card_notes"]) == 3
    assert calls == ["sol", "glm"]


@pytest.mark.asyncio
async def test_sol_effort_explicitly_medium(monkeypatch):
    calls = []

    async def fake_run(argv, *, timeout, output_file=None):
        calls.append(argv)
        return ANSWER

    monkeypatch.setattr(external_writers, "_run", fake_run)
    assert (await external_writers.sol("prompt", None))["text"]
    assert calls[0][calls[0].index("-c") + 1] == "model_reasoning_effort=medium"


def test_card_notes_writer_validation():
    from street_story.providers import _validated_card_notes
    import copy
    cards = [
        {"notes": ["Театр", "В 1910 году открылся театр", "листай →"], "arrows": ["", "", ""]},
        {"notes": ["После 1920 года здание перестроили"], "arrows": [""]},
        {"notes": [], "arrows": []},
    ]
    facts = [{"text": "Театр открылся в 1910 году, здание перестроили после 1920 года."}]
    assert _validated_card_notes(cards, 3, "Театр", facts) == cards
    for field, value in [
        ((1, 0), "健康新 здание открылось в 1915 году"),
        ((1, 0), "После 1920 года листай →"),
        ((1, 0), "После 1920 года здание перестроили иностранныйUNKNOWN"),
        ((1, 0), "Очень " * 20 + "длинный текст"),
    ]:
        broken = copy.deepcopy(cards)
        broken[field[0]]["notes"][field[1]] = value
        with pytest.raises(ValueError):
            _validated_card_notes(broken, 3, "Театр", facts)
    broken = copy.deepcopy(cards)
    broken[-1]["notes"] = ["Финальная лишняя надпись"]
    broken[-1]["arrows"] = [""]
    with pytest.raises(ValueError, match="final"):
        _validated_card_notes(broken, 3, "Театр", facts)


def test_cover_notes_accept_spaced_number_and_restore_missing_labels():
    from street_story.providers import _validated_card_notes
    place = "Поликлиника городской больницы №3"
    facts = [{"text": "В 1954 году здесь открылась поликлиника городской больницы."}]
    rest = [{"notes": ["В 1954 году открылась городская поликлиника"], "arrows": [""]},
            {"notes": [], "arrows": []}]
    spaced = {"notes": ["Поликлиника городской больницы № 3",
                        "В 1954 году здесь открылась поликлиника", "листай →"],
              "arrows": ["", "", ""]}
    assert _validated_card_notes([spaced, *rest], 3, place, facts)[0] == spaced

    missing = {"notes": ["В 1954 году здесь открылась поликлиника"], "arrows": [""]}
    restored = _validated_card_notes([missing, *rest], 3, place, facts)[0]
    assert restored == {"notes": [place, missing["notes"][0], "листай →"],
                        "arrows": ["", "", ""]}

    four = {"notes": [place, "В 1954 году здесь открылась поликлиника",
                       "На фасаде видны большие окна", ""],
            "arrows": ["", "", "", ""]}
    assert _validated_card_notes([four, *rest], 3, place, facts)[0]["notes"][-1] == "листай →"


@pytest.mark.asyncio
async def test_caption_and_three_card_notes_are_one_writer_result(monkeypatch):
    calls = []

    async def external(prompt, photo, *, answer_instruction, validate):
        calls.append(prompt)
        assert "photo_count" in prompt and "card_notes" in answer_instruction
        answer = {"text": "История театра.\n\nУлица 1\n#калининград",
                  "card_notes": [
                      {"notes": ["Театр", "В 1910 году здесь открылся театр", "листай →"],
                       "arrows": ["", "", ""]},
                      {"notes": ["В 1920 году здание перестроили"], "arrows": [""]},
                      {"notes": [], "arrows": []}]}
        validate(answer)
        return answer, external_writers.SOL_MODEL

    monkeypatch.setattr(external_writers, "write", external)
    client = SimpleNamespace(settings=SimpleNamespace(external_writers=True),
        WRITER_RULES=GeminiClient.WRITER_RULES, WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA,
        writer_routes=[])
    result = await GeminiClient.write_caption(
        client, owner_request="История театра", current_text="", concept="",
        facts=[{"text": "В 1910 году здесь открылся театр; в 1920 году здание перестроили."}],
        place={"city": "Калининград"}, place_name="Театр", photo_count=3,
        literal_fragments=[], photo_jpeg=None)
    assert len(calls) == 1
    assert result["model"] == "GPT-6.1 Sol"
    assert len(result["card_notes"]) == 3
    assert result["card_notes"][2] == {"notes": [], "arrows": []}


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
