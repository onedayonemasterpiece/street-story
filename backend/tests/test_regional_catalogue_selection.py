"""Observed catalogue inventories reach SOURCE selection before body reads."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from street_story import identity_architectural_context as context, identity_discovery, prussia39
from street_story.identity_proof import accepted_identity
from test_architectural_text_identity import TEXT, text_inputs, with_received_physical_links
from test_geometry_identity_plan import Executor, geometry_setup, geometry_decision, payload
from test_prussia39 import Cache, html, response, resolver


def candidate(cid="osm:way:2", street="Тестовая улица", number="6"):
    return {
        "candidate_id": cid,
        "identity_eligible": True,
        "map_address": {"city": "Город", "street": street, "house_number": number},
        "map_object": {"tags": {"building": "yes"}},
    }


def story(*items):
    return {"id": "fixture", "latitude": 54.7, "longitude": 20.5, "photo_sha256": "a" * 64, "_identity_observed_candidates": list(items)}


def inventory(start=1, stop=20, total=35, *, next_page=True):
    rows = ""
    for index in range(start, stop + 1):
        sid = 42 if index in (1, 2, 3) else index
        number = {1: "53", 2: "57", 3: "61"}.get(index, str(index))
        rows += (
            f'<tr><td style="text-align:justify;"><b>Дом {index}</b>'
            f'<p style="font-size:8pt;">Город, Тестовая, {number}</p>Описание фасада {index}.'
            f'<a href="index.php?sid={sid}">Подробнее</a></td><td>'
            f'<a href="index.php?sid={sid}"><img src="/thumb.jpg"></a></td></tr>'
        )
    page = '<a href="database.php?text_adr=%D2%E5%F1%F2%EE%E2%E0%FF&p=2">2</a>' if next_page else ""
    return html(f"<div>Результаты поиска (всего совпадений: {total})<table>{rows}</table>{page}</div>")


def selection(aid="prussia39:sid:34", cid="osm:way:2"):
    return {
        "article_id": aid,
        "candidate_id": cid,
        "scope": "The main physical building at literal6, not6A.",
        "binding_basis": "Observed card scope and received physical address agree; neighbor remains separate.",
        "physical_binding_resolved": True,
    }


def offline(monkeypatch, handler):
    cls = httpx.AsyncClient
    monkeypatch.setattr(
        context,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kwargs: cls(transport=httpx.MockTransport(handler), **kwargs), HTTPError=httpx.HTTPError),
    )
    adapter = prussia39.Prussia39Adapter

    class OfflineAdapter(adapter):
        def __init__(self, store, http):
            super().__init__(store, http, resolver=resolver)

    monkeypatch.setattr(prussia39, "Prussia39Adapter", OfflineAdapter)


def test_preparation_anchor_is_literal_and_mixed_pool_never_chooses_first():
    first, other = candidate(), candidate("osm:way:3", "Другая улица", "6А")
    assert context.regional_preparation_query(story(first, other), [first, other]) is None
    camera = story(first, other)
    camera["_identity_search_context"] = {"reverse_address": {"city": "Город", "road": "Камерная улица"}}
    q = context.regional_preparation_query(camera, [first])
    assert q["street"] == "Тестовая улица" and q["provenance"] == "physical_subject_addresses"
    assert q["camera_reverse_road_hint"] == "Камерная улица"
    assert q["candidate_ids"] == [first["candidate_id"]]
    assert q["anchor_kind"] == "subject_retrieval_context" and q["target_identity_established"] is False
    camera["_identity_search_context"]["reverse_address"]["pedestrian"] = "Иная улица"
    assert context.regional_preparation_query(camera, [first, other]) is None
    camera.pop("_identity_search_context")
    camera["_identity_observed_candidates"] = [first, candidate("osm:way:3", number="6А")]
    assert context.regional_preparation_query(camera, [first])["street"] == "Тестовая улица"


@pytest.mark.asyncio
async def test_cold35_cards20plus15_keep_same_sid_aliases_and_cache_without_bodies(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/sight/database.php"
        return response(inventory(21, 35, next_page=False) if b"p=2" in request.url.query else inventory())

    offline(monkeypatch, handler)
    obj = candidate()
    s = story(obj)
    service = SimpleNamespace(store=Cache(tmp_path / "cache"))
    receipt = await context.prepare_regional_catalogue(service, s, [obj])
    assert len(calls) == 2 and len(receipt["results"]) == 35
    assert receipt["total_count"] == 35 and receipt["received_row_count"] == 35
    assert receipt["unique_article_count"] == 33 and receipt["inventory_complete"]
    variants = [r for r in receipt["results"] if r["article_id"] == "prussia39:sid:42"]
    assert [r["address_text"] for r in variants] == ["Город, Тестовая, 53", "Город, Тестовая, 57", "Город, Тестовая, 61"]
    assert await context.prepare_regional_catalogue(service, s, [obj]) == receipt
    s2 = story(obj)
    s2["photo_sha256"] = "b" * 64
    reused = await context.prepare_regional_catalogue(service, s2, [obj])
    assert len(calls) == 2 and reused["cache_hit"] and reused["scope"] != receipt["scope"]
    model = context.catalogue_model_context(receipt)
    assert len(model["results"]) == 35 and model["total_count"] == 35
    assert all(r["metadata_excerpt"] for r in model["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["unselected", "foreign_sid", "foreign_building", "unresolved6_6A", "empty_scope", "duplicate", "tenant"]
)
async def test_unselected_or_unbound_cards_never_fetch_bodies(tmp_path, monkeypatch, change):
    def forbidden(request):
        pytest.fail("Unselected/foreign/unresolved cards cannot dispatch a body read")

    offline(monkeypatch, forbidden)
    obj = candidate()
    s = story(obj)
    choices = [selection()]
    cat = {
        "status": "completed",
        "inventory_complete": False,
        "total_count": 35,
        "results": [{"article_id": "prussia39:sid:34", "canonical_url": prussia39.canonical_article(34)[1]}],
    }
    if change == "unselected":
        choices = []
    elif change == "foreign_sid":
        choices[0]["article_id"] = "prussia39:sid:999"
    elif change == "foreign_building":
        choices[0]["candidate_id"] = "osm:way:999"
    elif change == "unresolved6_6A":
        choices[0]["physical_binding_resolved"] = False
    elif change == "empty_scope":
        choices[0]["scope"] = ""
    elif change == "duplicate":
        choices *= 2
    else:
        obj["identity_eligible"] = False
    articles, receipt = await context.acquire_selected_regional_text(
        SimpleNamespace(store=Cache(tmp_path / "cache")), s, [obj], choices, cat
    )
    assert not articles and receipt["status"] == "not_sent"


@pytest.mark.asyncio
async def test_partial_inventory_can_read_a_selected_known_card_without_claiming_exhaustion(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return response(html(f'<td style="text-align:justify">{TEXT}</td>'))

    offline(monkeypatch, handler)
    obj = candidate()
    s = story(obj)
    cat = {
        "status": "completed",
        "inventory_complete": False,
        "total_count": 35,
        "results": [
            {"article_id": "prussia39:sid:34", "canonical_url": prussia39.canonical_article(34)[1], "address_text": "Город, Тестовая, 6"}
        ],
    }
    articles, receipt = await context.acquire_selected_regional_text(
        SimpleNamespace(store=Cache(tmp_path / "cache")), s, [obj], [selection()], cat
    )
    assert len(calls) == 1 and len(articles) == 1
    assert receipt["catalogue"]["total_count"] == 35 and not receipt["catalogue"]["inventory_complete"]
    assert articles[0]["lookup_candidate_ids"] == ["osm:way:2"]
    assert articles[0]["scope"] == selection()["scope"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text_resolves_binding", [False, True])
async def test_unconfirmed_selected_card_reaches_existing_t_even_when_search_coverage_is_incomplete(
        tmp_path, monkeypatch, text_resolves_binding):
    service, s, active = geometry_setup(tmp_path)
    s.update(latitude=54.7, longitude=20.5)
    s["_identity_search_context"] = {"reverse_address": {"city": "Город", "road": "Тестовая улица"}}
    for item in s["_identity_observed_candidates"]:
        item["map_address"] = {"city": "Город", "street": "Тестовая улица",
            "house_number": "6" if item["candidate_id"] == "osm:way:2" else "6А"}
    calls, reads = [], []
    def handler(request):
        reads.append(request)
        if request.url.path == "/sight/database.php":
            return response(inventory(21, 35, next_page=False) if b"p=2" in request.url.query else inventory())
        assert len(calls) == 1 and b"sid=34" in request.url.query
        return response(html(f'<td style="text-align:justify">{TEXT}</td>'))
    offline(monkeypatch, handler)
    initial = geometry_decision()
    initial.update(decision="uncertain", candidate_id="", next_action={
        "kind": "reference_image", "target_candidate_ids": ["osm:way:2"], "reason": "Inspect this unconfirmed body."})
    chosen = dict(selection(), physical_binding_resolved=False)
    first = {**payload(initial), "regional_article_selections": [chosen]}
    _story, _catalog, decision, _receipt = text_inputs(candidate_id="osm:way:2")
    for field in ("article_bindings", "correspondences"):
        decision[field][0]["article_id"] = "prussia39:sid:34"
    decision["material_alternatives"] = [{"candidate_id": "osm:way:3",
        "reason": "The SOURCE/text bay precedes the return; the neighboring body reverses that arrangement."}]
    if not text_resolves_binding:
        decision["decision"] = "uncertain"
        decision["article_bindings"][0]["physical_binding_resolved"] = False
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(first))
        assert len(calls) == 2 and len(reads) == 3
        assert "initial_geometry_rejection_not_identity" in contents[-1]
        assert '"physical_binding_claimed":false' in contents[-1]
        assert TEXT in contents[-1]
        assert "first_wave_hypotheses" not in config.response_json_schema["properties"]
        assert "_identity_geometry_result" not in s
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        return SimpleNamespace(text=json.dumps(with_received_physical_links(decision, contents)))
    async def forbidden(*args, **kwargs):
        pytest.fail("No repeated G, extra planner or REF after independent T")
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    if text_resolves_binding:
        await identity_discovery.prepare_search_plan(service, s, "", active)
        assert s["_identity_geometry_result"]["proof_kind"] == "architectural_text"
    else:
        history, _ = await identity_discovery.prepare_search_plan(service, s, "", active)
        assert "_identity_geometry_result" not in s
        partial = history['search_plan']['payload']
        assert partial['physical_research_priority']['active_candidate_ids']
        assert partial['first_wave_hypotheses'] == []
        assert partial['physical_research_priority']['identity_established'] is False
        from street_story import article_media
        media_reads = []
        async def actual_selected_media(svc, snapshot, sources, excluded, *, receipts, first_ready):
            media_reads.append(sources)
            assert first_ready and len(sources) == 1
            assert sources[0]['url'] == partial['source_text_receipt']['articles'][0]['url']
            return [{'candidate_id': 'web:fixture', 'url': sources[0]['url'],
                'reference_image_urls': ['https://example.org/actual-facade.jpg']}]
        monkeypatch.setattr(article_media, 'article_candidates', actual_selected_media)
        result, pending = await identity_discovery.recover(service, s, '', active, set())
        assert result['status'] == 'uncertain' and result['_article_media_pending']
        assert pending[0]['candidate_id'] == 'web:fixture'
        assert len(media_reads) == 1 and len(calls) == 2
    saved = service._identity_snapshot(s["id"])[1]["identity_physical_hypothesis"]
    assert saved["identity_accepted"] is False
    assert saved["closed_payload"]["regional_article_selections"][0]["physical_binding_resolved"] is False
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,status",
    [
        (b"", "transport_failed"),
        (html("<form>Адрес</form>"), "parse_failed"),
        (html("<div>Результаты поиска всего совпадений: 0</div>"), "completed_empty"),
    ],
)
async def test_preparation_truthful_empty_form_transport(tmp_path, monkeypatch, body, status):
    offline(monkeypatch, lambda request: response(body))
    obj = candidate()
    s = story(obj)
    receipt = await context.prepare_regional_catalogue(SimpleNamespace(store=Cache(tmp_path / "cache")), s, [obj])
    assert receipt["status"] == status and not receipt["results"]


@pytest.mark.asyncio
async def test_slow_optional_preparation_has_reader_bound_and_drains_request(tmp_path, monkeypatch):
    cancelled = asyncio.Event()

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    offline(monkeypatch, handler)
    # Exercise actual cancellation without sleeping for the production envelope.
    monkeypatch.setattr(prussia39, "READ_TIMEOUT_SECONDS", .1)
    obj = candidate()
    s = story(obj)
    start = time.monotonic()
    receipt = await context.prepare_regional_catalogue(SimpleNamespace(store=Cache(tmp_path / "cache")), s, [obj])
    assert time.monotonic() - start < .7 and cancelled.is_set()
    assert receipt["status"] == "transport_failed" and receipt["error_code"] == "regional_preparation_wait_expired"
    assert not receipt["inventory_complete"]


@pytest.mark.asyncio
async def test_cold_cards_survive_old_three_second_preparation_cutoff(tmp_path, monkeypatch):
    async def handler(request):
        await asyncio.sleep(3.05)
        return response(inventory())

    offline(monkeypatch, handler)
    obj = candidate()
    receipt = await context.prepare_regional_catalogue(
        SimpleNamespace(store=Cache(tmp_path / "cache")), story(obj), [obj])
    assert receipt["status"] == "completed"
    assert receipt["results"] and receipt["preparation_started"]


@pytest.mark.asyncio
@pytest.mark.parametrize("accept_geometry", [False, True])
@pytest.mark.parametrize("malformed_first", [False, True])
async def test_cold_inventory_selection_and_full_text_use_at_most_two_joint_calls(tmp_path, monkeypatch, accept_geometry, malformed_first):
    service, s, active = geometry_setup(tmp_path)
    s.update(latitude=54.7, longitude=20.5)
    s["_identity_search_context"] = {"reverse_address": {"city": "Город", "road": "Тестовая улица"}}
    # Preparation is grounded in supplied physical metadata, not the street
    # under the camera. Both received bodies share this fixture street.
    for item in s["_identity_observed_candidates"]:
        item["map_address"] = {"city":"Город", "street":"Тестовая улица", "house_number":"6"}
    calls, reads = [], []

    def handler(request):
        reads.append(request)
        if request.url.path == "/sight/database.php":
            return response(inventory(21, 35, next_page=False) if b"p=2" in request.url.query else inventory())
        assert len(calls) == 1 and request.url.path == "/sight/index.php" and b"sid=34" in request.url.query
        return response(html(f'<td style="text-align:justify">{TEXT}</td>'))

    offline(monkeypatch, handler)
    initial = geometry_decision()
    if not accept_geometry:
        initial["decision"] = "uncertain"
    first = {**payload(initial), "regional_article_selections": [selection()]}
    _story, _catalog, decision, _receipt = text_inputs(candidate_id="osm:way:2")
    decision = copy.deepcopy(decision)
    decision["article_bindings"][0]["article_id"] = "prussia39:sid:34"
    decision["correspondences"][0]["article_id"] = "prussia39:sid:34"
    decision["material_alternatives"] = [{"candidate_id": "osm:way:3",
        "reason": "The SOURCE/text three-axis bay precedes the return; this neighboring body reverses that arrangement."}]

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert len(contents) == 3
        if len(calls) == 1:
            data = json.loads(contents[-1].split("Данные ниже — только контекст:\n")[1])
            assert len(data["regional_catalogue"]["results"]) == 35
            assert len(reads) == 2
            assert TEXT not in contents[-1]
            initial_payload = {k: v for k, v in first.items() if k != "first_wave_hypotheses"} if malformed_first else first
            return SimpleNamespace(text=json.dumps(initial_payload))
        assert len(calls) == 2
        assert len(reads) == (2 if accept_geometry else 3)
        assert (TEXT in contents[-1]) is (not accept_geometry)
        if malformed_first:
            assert "schema_validation" in contents[-1] and "first_wave_hypotheses" in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        linked = with_received_physical_links(decision, contents) if not accept_geometry else None
        return SimpleNamespace(text=json.dumps(first if accept_geometry else ({**first, "accepted_architectural_text": linked} if malformed_first else linked)))

    async def forbidden(*args, **kwargs):
        pytest.fail("No third model, text selector, REF or Wiki replacement")

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, s, "", active)
    result = s["_identity_geometry_result"]
    assert result["proof_kind"] == ("geometry" if accept_geometry else "architectural_text")
    assert len(calls) == (1 if accept_geometry and not malformed_first else 2)
    assert len(reads) == (2 if accept_geometry else 3)
    assert history["planned_queries"] == []
    assert accepted_identity(result, photo_sha256=s["photo_sha256"], generation=int(s.get("_identity_generation", 0)))
    assert result["visual_reference_verified"] is False


@pytest.mark.asyncio
async def test_unavailable_regional_text_keeps_independently_selected_wiki_in_same_followup(tmp_path, monkeypatch):
    from test_selected_wikipedia_architectural_text import pages, selected

    service, s, active = geometry_setup(tmp_path)
    s["_identity_wikipedia_metadata"] = pages()
    uncertain = geometry_decision()
    uncertain["decision"] = "uncertain"
    first = {
        **payload(uncertain),
        **selected(),
        "regional_lookup": {"route": "address", "candidate_ids": ["osm:way:2"], "reason": "Read distinguishing regional architecture."},
    }
    _story, _candidates, decision, receipt = text_inputs(candidate_id="osm:way:2")
    decision["material_alternatives"] = [{"candidate_id": "osm:way:3",
        "reason": "The acquired Wiki text and SOURCE agree on the bay/cornice ordering; the neighbor has the reverse return."}]
    reads, calls = [], []

    async def regional(*args):
        reads.append("regional")
        return [], {"status": "transport_failed", "error_code": "empty_body"}

    async def wiki(*args):
        reads.append("wiki")
        return receipt["articles"], {"status": "completed", "selected_page_ids": ["13"]}

    monkeypatch.setattr(context, "acquire_regional_text", regional)
    monkeypatch.setattr(context, "acquire_selected_wikipedia_text", wiki)

    async def generate(*args, **kwargs):
        calls.append("joint")
        return SimpleNamespace(text=json.dumps(first if len(calls) == 1 else with_received_physical_links(decision, args[2])))

    async def forbidden(*args, **kwargs):
        pytest.fail("No additional lookup, replan, third judge or reference acquisition")

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, s, "", active)
    assert reads == ["regional", "wiki"] and calls == ["joint", "joint"]
    assert s["_identity_geometry_result"]["proof_kind"] == "architectural_text"
    lookup = history["search_plan"]["payload"]["regional_lookup_receipt"]
    assert lookup["regional"]["status"] == "transport_failed"
    assert lookup["wikipedia"]["status"] == "completed"


@pytest.mark.asyncio
async def test_cached_catalogue_survives_cooldown_and_closed_http_admission(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return response(inventory(1, 1, total=1, next_page=False))

    offline(monkeypatch, handler)
    obj = candidate()
    s = story(obj)
    cache = Cache(tmp_path / "cache")
    service = SimpleNamespace(store=cache)
    first = await context.prepare_regional_catalogue(service, s, [obj])
    cache.cache_put(prussia39.COOLDOWN, {"retry_at": 1100}, 100)
    s2 = story(obj)
    s2["photo_sha256"] = "b" * 64
    second = await context.prepare_regional_catalogue(service, s2, [obj], allow_network=False)
    assert len(calls) == 1 and first["status"] == second["status"] == "completed"
    assert second["cache_hit"]
    # Same publisher street cache, different supplied physical scope. The old
    # scoped receipt must not falsely bind the inventory to the previous body.
    another = candidate("osm:way:3", number="6А")
    rebound = await context.prepare_regional_catalogue(service, s, [another], allow_network=False)
    assert len(calls) == 1 and rebound["cache_hit"]
    assert rebound["query_scope"]["candidate_ids"] == ["osm:way:3"]
    assert rebound["scope"] != first["scope"]
    s3 = story(candidate(street="Новая улица"))
    denied = await context.prepare_regional_catalogue(service, s3, s3["_identity_observed_candidates"], allow_network=False)
    assert denied["status"] == "not_sent" and len(calls) == 1


@pytest.mark.asyncio
async def test_full_deadline_and_page_admission_precede_new_optional_http(tmp_path, monkeypatch):
    from street_story import research_budget

    def forbidden(request):
        pytest.fail("Admission denied before any HTTP dispatch")

    offline(monkeypatch, forbidden)
    obj = candidate()
    s = story(obj)
    service = SimpleNamespace(store=Cache(tmp_path / "cache"), settings=object())
    monkeypatch.setattr(research_budget, "require_remaining", lambda *args: 1.0)

    def denied(*args, **kwargs):
        raise research_budget.ResearchTerminated("search_exhausted", "identity_page_envelope_exhausted")

    monkeypatch.setattr(research_budget, "reserve_work", denied)
    receipt = await context.prepare_regional_catalogue(service, s, [obj])
    assert receipt["status"] == "not_sent" and receipt["error_code"] == "identity_page_envelope_exhausted"
