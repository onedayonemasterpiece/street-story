"""Mira conducts; a lite text model writes the caption, and any answering model in the chain will do."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient
from street_story.service import canonical

from test_live_editor import make_service, mark_identity_ready


def prepared(tmp_path):
    from test_map_links import tower
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, story_id)["research_json"] or "{}")
        research["place_selection"] = tower()["place_selection"]
        db.execute("UPDATE stories SET research_json=?,draft_text=? WHERE id=?",
                   (canonical(research), "Старый текст.\n\nКалининград, улица 1\n#Калининград", story_id))
        db.execute(
            "INSERT OR IGNORE INTO live_editor_state(story_id,text_revision,literal_json,history_json,updated_at) VALUES(?,0,'[]','[]',?)",
            (story_id, svc.store.now()),
        )
    return svc, adapter, session


@pytest.mark.asyncio
async def test_writing_request_is_composed_by_the_writer_and_gets_the_server_footer(tmp_path):
    svc, adapter, session = prepared(tmp_path)
    calls = []

    async def write_caption(**kwargs):
        calls.append(kwargs)
        return {"text": "Новая подпись о доме.\n\nУлица 1\n#Калининград", "model": "gemini-3.5-flash-lite"}

    svc.providers.gemini.write_caption = write_caption
    edited = await adapter.execute_tool(session, {"name": "edit_text", "id": "write-1", "args": {
        "expected_text_revision": 0, "change_summary": "Переписано", "writing_request": "Перепиши подпись."}})
    assert edited["draft_text"].startswith("Новая подпись о доме.\n\nУлица 1\n#Калининград\n\n[ЯндексКарты](")
    request = calls[0]
    assert request["owner_request"] == "Перепиши подпись."
    assert request["current_text"].startswith("Старый текст.")
    assert request["photo_jpeg"]  # the writer sees the photo to tell visible from unseen parts
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, session.resource_id)["research_json"])
    assert research["draft_composed_by"] == "writer:gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_unavailable_writer_leaves_the_text_and_tells_mira_to_write_it_herself(tmp_path):
    svc, adapter, session = prepared(tmp_path)

    async def write_caption(**kwargs):
        raise GeminiUnavailable(0, "all_caption_writer_models_unavailable")

    svc.providers.gemini.write_caption = write_caption
    with pytest.raises(Exception) as failure:
        await adapter.execute_tool(session, {"name": "edit_text", "id": "write-2", "args": {
            "expected_text_revision": 0, "change_summary": "Переписано", "writing_request": "Перепиши подпись."}})
    assert getattr(failure.value, "code", None) == "caption_writer_unavailable"
    with svc.store.connection() as db:
        assert svc._story_row(db, session.resource_id)["draft_text"].startswith("Старый текст.")
    exact = await adapter.execute_tool(session, {"name": "edit_text", "id": "write-3", "args": {
        "expected_text_revision": 0, "change_summary": "Сама", "new_text": "Текст Миры.\n\nУлица 1\n#Калининград"}})
    assert exact["draft_text"].startswith("Текст Миры.")


class Route:
    def __init__(self, outcome):
        self.outcome = outcome

    async def execute(self, _operation, call):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return await call("key", 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["unavailable", "unsupported", "malformed"])
async def test_writer_uses_the_next_lite_model_when_one_does_not_answer(first):
    from street_story.errors import PermanentProviderError
    answers = {"gemini-3.5-flash-lite": json.dumps({"text": "Подпись.\n\nУлица 1\n#Калининград",
                                                    "image_notes": ["Построен в 1905 году"]}),
               "gemini-3.1-flash-lite": "not json" if first == "malformed" else "{}"}

    async def generate(_key, _timeout, contents, _config, *, operation, model, quota):
        assert contents[0].startswith(GeminiClient.WRITER_RULES)
        return SimpleNamespace(text=answers[model])

    failing = {"unavailable": GeminiUnavailable(0, "busy"),
               "unsupported": PermanentProviderError("gemini:unsupported_model"),
               "malformed": None}[first]
    client = SimpleNamespace(_generate=generate, WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, writer_routes=[
        ("gemini-3.1-flash-lite", None, None, Route(failing) if failing else Route(None)),
        ("gemini-3.5-flash-lite", None, None, Route(None)),
    ])
    result = await GeminiClient.write_caption(client, owner_request="Перепиши", current_text="", concept="",
        facts=[{"text": "Дом построен в 1905 году."}], place={"city": "Калининград"},
        literal_fragments=[], photo_jpeg=None)
    assert result == {"text": "Подпись.\n\nУлица 1\n#Калининград", "model": "gemini-3.5-flash-lite",
                      "image_notes": ["Построен в 1905 году"]}


@pytest.mark.asyncio
async def test_writer_reports_unavailable_when_no_model_answers():
    async def generate(*_args, **_kwargs):
        raise AssertionError("no route admits a call")

    client = SimpleNamespace(_generate=generate, WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, writer_routes=[
        ("gemini-3.1-flash-lite", None, None, Route(GeminiUnavailable(123.0, "busy"))),
        ("gemini-3.6-flash-lite", None, None, Route(GeminiUnavailable(99.0, "busy"))),
    ])
    with pytest.raises(GeminiUnavailable):
        await GeminiClient.write_caption(client, owner_request="Перепиши", current_text="", concept="",
            facts=[], place={}, literal_fragments=[], photo_jpeg=None)


def test_writer_image_notes_are_lettered_verbatim_by_the_image_brief(tmp_path):
    from test_mvp_visual import service
    svc = service(tmp_path)
    brief = svc._visual_brief({"place_name": "Калининград"}, {
        "selected_facts": [{"fact_id": "f1", "text": "Здание включено в реестр объектов культурного наследия местного значения."}],
        "caption_image_notes": ["Объект культурного наследия", "Построен в 1901–1907 годах"]})
    assert "Надписи на картинке — ровно эти, дословно: «Объект культурного наследия»; «Построен в 1901–1907 годах»" in brief
    assert len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT
    assert "Основание для подписей" not in brief  # the written notes already rest on the facts
    assert "на светлой бумажной подложке" in brief
    assert "Стрелка — только от надписи к части дома, которая ясно видна на фото" in brief


def test_long_selected_facts_do_not_crowd_out_the_written_image_notes(tmp_path):
    # Story 105: a long arson fact plus the rest exceeded the brief and blocked the new image.
    from test_mvp_visual import service
    svc = service(tmp_path)
    long_fact = "1 августа 1932 года около 6:05 национал-социалисты совершили бомбовый поджог здания. " * 30
    brief = svc._visual_brief({"place_name": "Калининград"}, {
        "selected_facts": [{"fact_id": f"f{i}", "text": long_fact} for i in range(4)],
        "caption_image_notes": ["1932: нацисты подожгли дом", "Прежде: маслобаза"]})
    assert "«1932: нацисты подожгли дом»" in brief and len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT


def eligible_story(tmp_path, c_source='https://source.example/c'):
    """A story with three verified facts about the house (claim keys a, b, c)."""
    from street_story.fact_ledger import accept_fact_basis, refresh_review_status
    from test_fact_ledger import fact, persist
    svc, adapter, session, _ = make_service(tmp_path)
    sid = session.resource_id
    mark_identity_ready(svc, sid)
    items = [fact('a', 'Здание — объект культурного наследия.', 'https://source.example/a', 'Heritage object.'),
             fact('b', 'Построено в 1930 году.', 'https://source.example/b', 'Built 1930.'),
             fact('c', '1 августа 1932 года дом подожгли.', c_source, 'Arson 1932.')]
    ids = persist(svc.store, sid, items, 'run', 'batch')
    with svc.store.tx() as db:
        for fact_id in ids:
            own = [row[0] for row in db.execute(
                "SELECT e.evidence_id FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id "
                "WHERE o.story_id=? AND o.assertion_id=?", (sid, fact_id))]
            accept_fact_basis(db, sid, fact_id, own, operation={'confirmation_model': 'controlled'}, now=svc.store.now())
        refresh_review_status(db, sid, svc.store.now())
    return svc, adapter, session, ids


@pytest.mark.asyncio
async def test_one_lite_decision_with_the_researcher_candidate_and_the_owner_keeps_the_last_word(tmp_path):
    from street_story import strongest_fact
    svc, _adapter, session, ids = eligible_story(tmp_path)
    sid = session.resource_id
    seen = []

    async def strongest_hook(*, facts, photo_jpeg, researcher_choice=None):
        seen.append((list(facts), researcher_choice))
        return {"ranked": [{"index": len(facts) - 1, "reason": "последний факт сильнее"}], "model": "gemini-3.1-flash-lite"}

    svc.providers.gemini.strongest_hook = strongest_hook
    record = await strongest_fact.decide(svc, sid, luna_candidate={"claim_key": "b", "reason": "дата"}, run_id="run")
    assert len(seen[0][0]) == 3 and seen[0][1]["index"] == 1  # whole inventory, researcher candidate by claim_key
    assert record["fact_id"] == ids[2] and record["source"] == "auto"
    assert record["researcher"]["fact_id"] == ids[1] and record["agrees_with_researcher"] is False
    assert [f["fact_id"] for f in svc.story(sid)["facts"] if f.get("strongest")] == [ids[2]]

    strongest_fact.choose(svc, sid, ids[1], "автор считает дату постройки главной")
    await strongest_fact.decide(svc, sid, run_id="run-2")  # a later research run proposes, never overrides
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, sid)["research_json"])
    assert research["strongest_fact"]["fact_id"] == ids[1] and research["strongest_fact"]["source"] == "owner"
    assert research["strongest_proposal"]["fact_id"] == ids[2]

    disputed = await strongest_fact.dispute(svc, sid, "не согласен")
    assert "Построено в 1930 году." not in seen[-1][0] and len(seen[-1][0]) == 2  # disputed fact excluded
    assert disputed["fact_id"] == ids[2] and disputed["source"] == "auto" and ids[1] in disputed["disputed"]


@pytest.mark.asyncio
async def test_the_writer_opens_with_the_stored_strongest_fact_which_joins_the_selection(tmp_path):
    from street_story import strongest_fact
    svc, adapter, session, ids = eligible_story(tmp_path)
    sid = session.resource_id

    async def strongest_hook(*, facts, photo_jpeg, researcher_choice=None):
        return {"ranked": [{"index": 2, "reason": "поджог"}], "model": "lite"}

    svc.providers.gemini.strongest_hook = strongest_hook
    await strongest_fact.decide(svc, sid)
    state = adapter._topic_state(sid)
    lead = await adapter._strongest_hook(sid, state, {ids[0]}, None)
    assert lead == {"fact_id": ids[2], "text": "1 августа 1932 года дом подожгли."}
    assert sorted(f["fact_id"] for f in svc.story(sid)["facts"] if f.get("selected")) == sorted([ids[0], ids[2]])


@pytest.mark.asyncio
async def test_a_fact_resting_only_on_visitor_reviews_does_not_open_the_post_when_documented_facts_exist(tmp_path):
    # Story 102: the lite model chose a visitor review ("дом пережил бомбардировку") as the opening.
    from street_story import strongest_fact
    svc, adapter, session, ids = eligible_story(tmp_path, c_source='https://2gis.ru/geo/20.5%2C54.7?m=20.5%2C54.7%2F18#reviews')
    sid = session.resource_id
    seen = []

    async def strongest_hook(*, facts, photo_jpeg, researcher_choice=None):
        seen.append(list(facts))
        return {"ranked": [{"index": len(facts) - 1, "reason": "последний"}], "model": "lite"}

    svc.providers.gemini.strongest_hook = strongest_hook
    record = await strongest_fact.decide(svc, sid)
    assert seen[-1] == ['Здание — объект культурного наследия.', 'Построено в 1930 году.']
    assert record["fact_id"] == ids[1]
    with svc.store.tx() as db:  # an earlier automatic choice of the review fact is decided again by the writer
        research = json.loads(svc._story_row(db, sid)["research_json"])
        research["strongest_fact"] = {**research["strongest_fact"], "fact_id": ids[2]}
        db.execute("UPDATE stories SET research_json=? WHERE id=?", (json.dumps(research), sid))
    lead = await adapter._strongest_hook(sid, adapter._topic_state(sid), set(), None)
    assert lead["fact_id"] == ids[1]


@pytest.mark.asyncio
async def test_an_independent_editor_reorders_the_story_and_keeps_the_ending():
    # 105/120 (owner, 18.10.2026): a building detail interrupted the event thread; the editor moves it.
    written = {"text": "В 1877 году здесь открылась клиника. Фасад из жёлтого кирпича. Перед входом стоял памятник основателю."
                       "\n\nБарнаульская улица 8\n#Калининград", "model": "writer", "image_notes": []}
    seen = []

    async def lite_chain(contents, config, parse):
        seen.append(json.loads(contents[0].split("\n\n", 1)[1]))
        return parse(json.dumps({"issues": [{"sentence": "Фасад из жёлтого кирпича.", "kind": "скачок", "fix": "перенесено"}],
                                 "text": "В 1877 году здесь открылась клиника. Перед входом стоял памятник основателю.\n\nСам дом — из жёлтого кирпича."}),
                     "lite")

    client = SimpleNamespace(EDITOR_RULES=GeminiClient.EDITOR_RULES, EDITOR_SCHEMA=GeminiClient.EDITOR_SCHEMA, _lite_chain=lite_chain)
    edited = await GeminiClient._edit_caption(client, written, facts=[{"text": "клиника"}], lead_fact="клиника")
    assert "Барнаульская" not in seen[0]["caption"]  # the ending is not the editor's business
    assert edited["text"].endswith("Сам дом — из жёлтого кирпича.\n\nБарнаульская улица 8\n#Калининград")
    assert edited["image_notes"] == [] and edited["editor_model"] == "lite"

    async def unavailable(*_args):
        raise GeminiUnavailable(0, "busy")

    client._lite_chain = unavailable
    assert await GeminiClient._edit_caption(client, written, facts=[], lead_fact=None) == written


def test_the_hook_is_never_a_list_of_institutions_while_a_story_about_the_building_exists():
    # 102: the lite model chose "insurance society, grocery, coffee shop on the ground floor" as the opening.
    facts = [
        {"index": 0, "kind": "institutions", "unexpected": 1, "human_drama": 0, "concrete": 2, "relates_to_photo": 0},
        {"index": 1, "kind": "building_detail", "unexpected": 3, "human_drama": 1, "concrete": 3, "relates_to_photo": 1},
        {"index": 2, "kind": "date_only", "unexpected": 0, "human_drama": 0, "concrete": 1, "relates_to_photo": 0},
    ]
    weak = GeminiClient._hook_choice({"facts": facts, "choice": {"index": 0, "reason": "много учреждений"}}, 3)
    assert weak[0]["index"] == 1
    strong = GeminiClient._hook_choice({"facts": facts, "choice": {"index": 1, "reason": "клеймо на кирпичах"}}, 3)
    assert strong == [{"index": 1, "reason": "клеймо на кирпичах"}]
    only_weak = [dict(facts[0]), dict(facts[2])]
    assert GeminiClient._hook_choice({"facts": only_weak, "choice": {"index": 0, "reason": "больше нечего"}}, 3)[0]["index"] == 0


@pytest.mark.asyncio
async def test_image_notes_come_only_from_documented_facts():
    # Story 102: "Дом чудом уцелел в 1945 году" (a visitor review) appeared on the image as history.
    written = {"text": "Текст.\n\nУлица 1\n#Калининград", "model": "writer",
               "image_notes": ["Дом чудом уцелел в 1945 году", "Построен около 1905 года"]}
    seen = []

    async def lite_chain(contents, config, parse):
        seen.append(json.loads(contents[0].split("\n\n", 1)[1]))
        return parse(json.dumps({"issues": [{"sentence": "Дом чудом уцелел в 1945 году", "kind": "отзыв", "fix": "заменено"}],
                                 "text": "Текст.", "image_notes": ["2017: немецкие кирпичи с клеймом", "Построен около 1905 года"]}), "lite")

    client = SimpleNamespace(EDITOR_RULES=GeminiClient.EDITOR_RULES, EDITOR_SCHEMA=GeminiClient.EDITOR_SCHEMA, _lite_chain=lite_chain)
    facts = [{"text": "Дом пережил бомбардировку.", "from_visitor_reviews": True}, {"text": "Построен около 1905 года."},
             {"text": "При расчистке стен открылись немецкие кирпичи."}]
    edited = await GeminiClient._edit_caption(client, written, facts=facts, lead_fact=None)
    assert seen[0]["image_facts"] == ["Построен около 1905 года.", "При расчистке стен открылись немецкие кирпичи."]
    assert edited["image_notes"] == ["2017: немецкие кирпичи с клеймом", "Построен около 1905 года"]


def test_the_post_never_names_where_a_fact_came_from():
    # Owner, 18.10.2026: "По свидетельству посетителя" means nothing to a reader of the channel.
    assert "по свидетельству посетителя" in GeminiClient.WRITER_RULES and "в тексте не отражается" in GeminiClient.WRITER_RULES
    assert "посетители пишут, что…»), не как" not in GeminiClient.WRITER_RULES
    assert "в тексте не отражается" in GeminiClient.EDITOR_RULES


def test_city_hashtag_is_capitalized():
    from street_story.providers import _capitalized_hashtags
    assert _capitalized_hashtags("Текст.\n\nулица Суворова 11\n#черняховск") == "Текст.\n\nулица Суворова 11\n#Черняховск"
    assert _capitalized_hashtags("#Калининград") == "#Калининград"
