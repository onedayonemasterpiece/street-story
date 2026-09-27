#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json
from pathlib import Path
import httpx

BASE_URL="https://street-story.kenigevents.ru"

def installer():
    path=Path(__file__).resolve().parents[1]/"deploy"/"devcoveer_install.py"
    spec=importlib.util.spec_from_file_location("ss_installer",path)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--story-id",required=True)
    ap.add_argument("--session-id",required=True)
    args=ap.parse_args()
    inst=installer()
    inst.require_mode(inst.DEVICE_TOKEN_FILE,0o600)
    token,created=inst.device_token()
    if created: raise SystemExit("unexpected_token_creation")
    with httpx.Client(base_url=BASE_URL,headers={"Authorization":"Bearer "+token},timeout=20) as client:
        r=client.get(f"/v1/stories/{args.story_id}/live-sessions/{args.session_id}/events",params={"after":0})
        if r.status_code!=200:
            print(json.dumps({"status":"unavailable","http_status":r.status_code,"secrets_disclosed":False}))
            return 1
        payload=r.json()
    events=[]
    for event in payload.get("events",[]) if isinstance(payload,dict) else []:
        if not isinstance(event,dict): continue
        item={"type":event.get("type")}
        if event.get("name"): item["name"]=event.get("name")
        if event.get("status"): item["status"]=event.get("status")
        if event.get("code"): item["code"]=event.get("code")
        calls=event.get("calls")
        if isinstance(calls,list):
            item["calls"]=[{"name":c.get("name"),"has_id":bool(c.get("id"))} for c in calls if isinstance(c,dict)]
        events.append(item)
    print(json.dumps({"status":"ok","cursor":payload.get("cursor"),"events":events,"secrets_disclosed":False},sort_keys=True))
    return 0
if __name__=="__main__": raise SystemExit(main())
