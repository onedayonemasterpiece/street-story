from __future__ import annotations
from uuid import uuid4

import pytest
from street_story.db import Store
from street_story.poi_model_bridge import PoiBridgeConflict, execute_poi_command

ACTOR = "11111111-1111-1111-1111-111111111111"


def model_request(action, entity=None, **kw):
    entity=entity or str(uuid4())
    return {
        "action": action, "actor_sub": ACTOR, "entity_id": entity,
        "canonical_name": "Дом старого архива",
        "site_state": "lost_site",
        "site_context": "Место утраченного сооружения возле бывшего рынка",
        "model_reason": "Модель прочла источник и квалифицировала конкретное физическое место.",
        "source_ref": "knowledge://chunks/" + str(uuid4()),
        "idempotency_key": "rkb-link:"+entity+":"+action, **kw,
    }


def test_model_creates_owner_poi_only_by_explicit_decision(tmp_path):
    db=Store(tmp_path/"owner.sqlite3")
    eid=str(uuid4())
    raw=model_request("create",eid)
    first=execute_poi_command(db,raw)
    assert first["state"]=="linked" and not first["replayed"]
    poi=first["poi"]
    assert poi["poi_id"].startswith("poi_ss_")
    assert poi["status"]=="candidate"
    assert poi["representative_position"] is None
    assert poi["site_descriptions"][0]["rkb_entity_id"]==eid
    assert poi["site_descriptions"][0]["site_state"]=="lost_site"
    assert execute_poi_command(db,raw)["replayed"] is True
    assert execute_poi_command(db,{"action":"search","actor_sub":ACTOR,"query":"Дом старого архива"})["candidates"][0]["poi_id"]==poi["poi_id"]
    with db.connection() as cx:
        assert cx.execute("SELECT count(*) FROM pois").fetchone()[0]==1
        assert cx.execute("SELECT count(*) FROM rkb_poi_links").fetchone()[0]==1
        assert "Модель" not in str(cx.execute("SELECT * FROM pois").fetchone())


def test_existing_poi_selected_after_shortlist_without_automerging(tmp_path):
    db=Store(tmp_path/"owner.sqlite3")
    one=execute_poi_command(db,model_request("create",str(uuid4())))
    poi_id=one["poi"]["poi_id"]
    other=str(uuid4())
    matching=execute_poi_command(db,{"action":"search","actor_sub":ACTOR,"query":"Дом старого архива"})
    assert matching["identity_selected"] is False and len(matching["candidates"])==1
    with pytest.raises(PoiBridgeConflict,match="existing_poi_candidates_require_selection"):
        execute_poi_command(db,model_request("create",other))
    selection=model_request("select",other,poi_id=poi_id)
    selected=execute_poi_command(db,selection)
    assert selected["poi"]["poi_id"]==poi_id
    assert len(selected["poi"]["site_descriptions"])==2
    assert execute_poi_command(db,selection)["replayed"]
    with pytest.raises(PoiBridgeConflict):
        execute_poi_command(db,model_request("select",other,poi_id="another-existing-id"))
    with db.connection() as cx:
        assert cx.execute("SELECT count(*) FROM pois").fetchone()[0]==1


def test_no_implicit_creation_untrusted_fields_or_owner_leak(tmp_path):
    db=Store(tmp_path/"owner.sqlite3")
    with pytest.raises(ValueError):
        execute_poi_command(db,{"action":"create","actor_sub":ACTOR,"canonical_name":"Unverified site"})
    with pytest.raises(ValueError):
        execute_poi_command(db,{"action":"create","actor_sub":ACTOR,"foo":"bar"})
    eid=str(uuid4())
    result=execute_poi_command(db,model_request("create",eid))
    found=execute_poi_command(db,{"action":"get","actor_sub":str(uuid4()),"poi_id":result["poi"]["poi_id"]})
    assert found["state"]=="found" and found["poi"]["site_descriptions"]==[]
    assert execute_poi_command(db,{"action":"search","actor_sub":str(uuid4()),"query":"Дом старого архива"})["candidates"]


def test_scoped_service_http_route_rejects_device_token(tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from test_backend import config, service
    from street_story.app import create_app
    s,_,_=service(tmp_path)
    settings=replace(config(tmp_path),
        rkb_poi_service_token="rkb-test-specific-credential-0123456789")
    with TestClient(create_app(settings=settings,service=s)) as client:
        body={"action":"search","actor_sub":ACTOR,"query":"Дом старого архива"}
        denied=client.post("/v1/internal/rkb-poi",json=body)
        assert denied.status_code==401
        device=settings.device_token.get_secret_value()
        denied=client.post("/v1/internal/rkb-poi",json=body,
                           headers={"Authorization":"Bearer "+device})
        assert denied.status_code==401
        allowed=client.post("/v1/internal/rkb-poi",json=body,headers={
            "Authorization":"Bearer rkb-test-specific-credential-0123456789"})
        assert allowed.status_code==200 and allowed.json()["identity_selected"] is False


if __name__ == "__main__":
    from pathlib import Path
    from tempfile import TemporaryDirectory
    for test in (test_model_creates_owner_poi_only_by_explicit_decision,
                 test_existing_poi_selected_after_shortlist_without_automerging,
                 test_no_implicit_creation_untrusted_fields_or_owner_leak):
        with TemporaryDirectory() as folder:
            test(Path(folder))
    print("3 isolated Street Story POI tests passed")
