"""Source-confirmed cover titles: real verifier -> ledger -> readback, not injected facts."""
import json
from uuid import uuid4

import pytest

from street_story.agent_evidence import AgentEvidenceMixin, NETWORK_CONTRACT
from street_story.fact_agents import _finish
from street_story.headless_facts import HeadlessFacts
from street_story.multiphoto import StoryPhotosMixin
from street_story.research_runs import begin_research_run
from test_headless_facts import URL, fixture
from test_multiphoto import shot

DETAIL = "в 1912 году здесь действовал пансион."
AGENT_NAME = "Вилла Зее-Адлер"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "accepted", "site", "unsupported_name", "owner_voice",
    "same_name_twice", "stronger_next_run", "manual_cover",
])
async def test_confirmed_name_through_verifier_ledger_and_card_readback(tmp_path, case):
    source = f"{AGENT_NAME}: {DETAIL} Новое название: Вилла Зее-Адлер у моря."
    service, job, _, reader, _ = await fixture(tmp_path, text=source)
    service.__class__ = type("ConfirmedStory", (StoryPhotosMixin, AgentEvidenceMixin, type(service)), {})
    sid = job["story_id"]
    photo_id = str(uuid4())
    service.add_story_photo(sid, "extra", photo_id, shot("blue"), "image/jpeg")
    service.order_story_photos(sid, "two-photos", {"photo_ids": ["source", photo_id]})
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row["research_json"])
        research["visual_identity"]["candidate_name"] = "Вилла"
        research["visual_identity"]["candidate_name_provenance"] = (
            "owner_voice" if case == "owner_voice" else "osm.tags.name")
        research["caption_image_notes"] = {
            "photo_ids": ["source", photo_id],
            "cards": [
                {"notes": ["Морская вилла" if case == "manual_cover" else "Вилла",
                           "Вход в пансион", "листай →"], "arrows": ["", "", ""]},
                {"notes": ["В доме были гости"], "arrows": [""]},
            ],
        }
        db.execute("UPDATE stories SET place_name=?,research_json=? WHERE id=?",
                   ("Вилла", json.dumps(research, ensure_ascii=False), sid))

    calls = []
    async def verify(payload, *, checkpoint, guard):
        guard()
        candidate = payload["candidate"]
        name = candidate["claim_key"].split(":", 1)[-1]
        relation = "documented_site" if case == "site" else "selected_building"
        fragment = (f"{name}: {DETAIL}" if case != "unsupported_name"
                    else f"В доме {DETAIL}")
        chunk_id = payload["source"]["chunks"][0]["chunk_id"]
        row = {"verdict": "supported", "text": f"{name}: в начале XX века здесь действовал пансион.",
               "claim_key": "verified-independent-semantic-key-" + str(len(calls)),
               "subject_scope": "complex_or_site" if case == "site" else "selected_building",
               "confirmed_fragment": fragment, "chunk_ids": [chunk_id],
               "reason": "Source and physical subject checked independently.",
               "source_date": None, "existing_fact_id": "", "conflicts_with_existing": [],
               "checks": {
                   "source_physical_subject": name,
                   "object_relation": relation,
                   "relation_basis": {"fragment": fragment, "chunk_ids": [chunk_id]},
                   "material_kind": "history", "temporal_basis": "historical",
                   "temporal_preserved": True}}
        receipt = {"attempt_id": f"verification-{len(calls)}", "phase": "completed",
                   "contract": NETWORK_CONTRACT, "model_id": "verifier",
                   "provider_id": "google", "result": row}
        checkpoint(receipt)
        calls.append(row)
        return {"result": row, "receipt": receipt}
    service.providers.gemini.verify_agent_evidence = verify

    async def accept(name, run_id="headless-run", index=1):
        claim = f"{name}: {DETAIL} Вариант {index}."
        candidate = {"claim_key": f"cover-name:{name}", "text": claim, "url": URL,
                     "quote": claim, "subject_scope": "selected_building", "source_date": None}
        return await service.confirm_agent_fact(job, run_id, candidate, {
            "route_id": "controlled", "provider_id": "google",
            "model_id": "researcher", "attempt_id": f"agent-{run_id}-{index}"})

    def finish(run_id="headless-run"):
        return _finish(type("Adapter", (), {"service": service})(), job, run_id,
                       HeadlessFacts(service), 0, "research_agent_completed", pending=False)

    try:
        first = await accept(AGENT_NAME)
        assert first["status"] == "supported"
        if case == "same_name_twice":
            assert (await accept(AGENT_NAME, index=2))["status"] == "supported"
        assert finish() is not None
        if case == "stronger_next_run":
            with service.store.tx() as db:
                row = service._story_row(db, sid)
                begin_research_run(db, story_id=sid, poi_key="wiki:77",
                                   goal="Further confirmed title", scope="history",
                                   expected_story_revision=row["revision"],
                                   identity_generation=0, run_id="new-run", now=service.store.now())
            assert (await accept("Вилла Зее-Адлер у моря", "new-run"))["status"] == "supported"
            assert finish("new-run") is not None
        proposed = ("Вилла Зее-Адлер у моря" if case == "stronger_next_run"
                    else AGENT_NAME if case in {"accepted", "same_name_twice", "manual_cover"}
                    else "Вилла")
        result = service.story(sid)
        assert result["place_name"] == proposed
        assert result["visual_identity"]["candidate_name"] == "Вилла"
        assert result["card_notes"][0]["notes"][0] == (
            "Морская вилла" if case == "manual_cover" else proposed)
        assert result["card_notes"][0]["notes"][1:] == ["Вход в пансион", "листай →"]
        assert result["card_notes"][1]["notes"] == ["В доме были гости"]
        assert len(calls) == (2 if case in {"same_name_twice", "stronger_next_run"} else 1)
    finally:
        await reader.search_http.aclose()
