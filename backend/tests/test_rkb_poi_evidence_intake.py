"""RKB adds book facts to Street Story's POI registry (owner, 18.10.2026: nothing had ever arrived)."""
from dataclasses import replace
import copy

from fastapi.testclient import TestClient

from street_story.app import create_app
from street_story.poi_memory import _public_regional_knowledge_facts
from test_backend import config, service
from test_poi_external import event

TOKEN = "rkb-test-specific-credential-0123456789"


def test_rkb_delivers_book_evidence_and_reads_back_the_three_confirmations(tmp_path):
    svc, _, _ = service(tmp_path)
    settings = replace(config(tmp_path), rkb_poi_service_token=TOKEN)
    auth = {"Authorization": "Bearer " + TOKEN}
    first = event(visibility="public", owner=None, semantic_key="built-1843", text="Ворота построены в 1843 году.")
    with TestClient(create_app(settings=settings, service=svc)) as client:
        assert client.post("/v1/internal/rkb-poi-evidence", json=first).status_code == 401
        device = {"Authorization": "Bearer " + settings.device_token.get_secret_value()}
        assert client.post("/v1/internal/rkb-poi-evidence", json=first, headers=device).status_code == 401
        delivered = client.post("/v1/internal/rkb-poi-evidence", json=first, headers=auth)
        assert delivered.status_code == 200 and delivered.json()["poi_id"]  # an unknown place becomes a candidate POI
        assert client.post("/v1/internal/rkb-poi-evidence", json=first, headers=auth).json()["replayed"] is True
        receipt = client.get(f"/v1/internal/rkb-poi-evidence/{first['event_id']}", headers=auth).json()
        assert receipt["delivered"] and receipt["poi_resolved"] and receipt["claim_status"] == "candidate"
        assert receipt["superseded"] is False
        bad = dict(first, contract_version="other")
        assert client.post("/v1/internal/rkb-poi-evidence", json=bad, headers=auth).status_code == 422

        identity = {"wikidata": "Q12345"}
        with svc.store.tx() as db:
            assert _public_regional_knowledge_facts(db, identity, None, statuses=("accepted",)) == []  # not reviewed yet
            db.execute("UPDATE poi_claims SET status='accepted' WHERE id=?", (receipt["claim_id"],))
            assert [f["text"] for f in _public_regional_knowledge_facts(db, identity, None, statuses=("accepted",))] == [
                "Ворота построены в 1843 году."]

        corrected = copy.deepcopy(event(visibility="public", owner=None, semantic_key="built-1844",
                                        text="Ворота построены в 1844 году."))
        corrected["source"]["revision"] = 2
        corrected["idempotency_key"] = corrected["idempotency_key"].replace(":1:", ":2:")
        assert client.post("/v1/internal/rkb-poi-evidence", json=corrected, headers=auth).status_code == 200
        assert client.get(f"/v1/internal/rkb-poi-evidence/{first['event_id']}", headers=auth).json()["superseded"] is True
        with svc.store.tx() as db:  # the corrected book revision supersedes the old evidence
            texts = [f["text"] for f in _public_regional_knowledge_facts(db, identity, None)]
        assert texts == ["Ворота построены в 1844 году."]


def test_the_owners_private_book_facts_reach_stories_and_others_stay_out(tmp_path, monkeypatch):
    from street_story.poi_external import ingest_poi_evidence
    svc, _, _ = service(tmp_path)
    owner, stranger = "6f7f732e-6ff3-5425-a3f1-b2062f4d9e50", "11111111-2222-3333-4444-555555555555"
    mine = ingest_poi_evidence(svc.store, event(visibility="private", owner=owner, semantic_key="own", text="Из моей книги."))
    theirs = ingest_poi_evidence(svc.store, event(visibility="private", owner=stranger, semantic_key="other", text="Из чужой книги."))
    identity = {"wikidata": "Q12345"}
    with svc.store.tx() as db:
        db.execute("UPDATE poi_claims SET status='accepted' WHERE id IN (?,?)", (mine["claim_id"], theirs["claim_id"]))
        assert _public_regional_knowledge_facts(db, identity, None, statuses=("accepted",)) == []
        monkeypatch.setenv("STREET_STORY_RKB_OWNER_SUBS", owner)
        assert [f["text"] for f in _public_regional_knowledge_facts(db, identity, None, statuses=("accepted",))] == ["Из моей книги."]
