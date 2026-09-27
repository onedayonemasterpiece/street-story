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
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def urls(value):
    out=[]
    def visit(item):
        if isinstance(item,str):
            if item.startswith("https://") and item not in out: out.append(item)
        elif isinstance(item,dict):
            for nested in item.values(): visit(nested)
        elif isinstance(item,list):
            for nested in item: visit(nested)
    visit(value)
    return out

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
        response=client.get(f"/v1/stories/{args.story_id}/live-sessions/{args.session_id}/events",params={"after":0})
    if response.status_code!=200:
        print(json.dumps({"status":"unavailable","http_status":response.status_code,"secrets_disclosed":False}))
        return 1
    payload=response.json()
    output=[]
    for event in payload.get("events",[]):
        if not isinstance(event,dict): continue
        item={"type":event.get("type")}
        for key in ("name","status","code","capability"):
            if event.get(key) is not None:item[key]=event.get(key)
        if event.get("type")=="grounding":
            item["https_url_count"]=len(urls(event.get("metadata")))
        if isinstance(event.get("calls"),list):
            item["calls"]=[{"name":call.get("name"),"has_id":bool(call.get("id"))} for call in event["calls"] if isinstance(call,dict)]
        if event.get("type")=="output_transcript":
            item["text_chars"]=len(str(event.get("text") or ""))
        output.append(item)
    print(json.dumps({"status":"ok","cursor":payload.get("cursor"),"closed":payload.get("closed"),"events":output,"secrets_disclosed":False},sort_keys=True))
    return 0
if __name__=="__main__":
    raise SystemExit(main())
