"""The source-backed researcher nominates a cover name without renaming the selected POI."""
import json

import pytest

from street_story.fact_agents import _finish
from street_story.fact_ledger import accept_fact_basis, refresh_review_status
from street_story.headless_facts import HeadlessFacts
from test_fact_ledger import fact, persist
from test_headless_facts import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("supported,manual", [(True, False), (False, False), (True, True)])
async def test_confirmed_name_handoff_requires_accepted_current_fact_and_preserves_owner(tmp_path, supported, manual):
    service, job, _, reader, _ = await fixture(tmp_path)
    try:
        sid = job["story_id"]
        with service.store.tx() as db:
            row = service._story_row(db, sid)
            research = json.loads(row["research_json"])
            research["visual_identity"]["candidate_name"] = "Вилла"
            research["visual_identity"]["candidate_name_provenance"] = "owner_voice" if manual else "osm.tags.name"
            db.execute("UPDATE stories SET place_name=?,research_json=? WHERE id=?",
                       ("Вилла", json.dumps(research, ensure_ascii=False), sid))
        text = "Вилла Зее-Адлер: в 1912 году здесь действовал пансион."
        item = fact("cover-name:Вилла Зее-Адлер", text,
                    "https://example.org/villa-history", text)
        fid = persist(service.store, sid, [item], "headless-run", "name-fact")[0]
        if supported:
            with service.store.tx() as db:
                ids = [r[0] for r in db.execute(
                    "SELECT evidence_id FROM fact_evidence_spans WHERE observation_id IN "
                    "(SELECT observation_id FROM fact_observations WHERE story_id=? AND assertion_id=?)",
                    (sid, fid))]
                accept_fact_basis(db, sid, fid, ids, operation={"confirmation_model": "test"},
                                  now=service.store.now())
                refresh_review_status(db, sid, service.store.now())
        outcome = _finish(type("Adapter", (), {"service": service})(), job, "headless-run",
                          HeadlessFacts(service), 0, "research_agent_completed", pending=False)
        assert outcome is not None
        expected = "Вилла Зее-Адлер" if supported and not manual else "Вилла"
        assert service.story(sid)["place_name"] == expected
        assert service.story(sid)["visual_identity"]["candidate_name"] == "Вилла"
    finally:
        await reader.search_http.aclose()
