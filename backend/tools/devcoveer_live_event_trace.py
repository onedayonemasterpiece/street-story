#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, importlib.util, json, struct, time, uuid, zlib
from pathlib import Path
import httpx

BASE_URL="https://street-story.kenigevents.ru"

def installer():
    path=Path(__file__).resolve().parents[1]/"deploy"/"devcoveer_install.py"
    spec=importlib.util.spec_from_file_location("ss_installer",path)
    assert spec and spec.loader
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def chunk(kind,data):
    return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)

def png():
    sig=b"\x89PNG\r\n\x1a\n"; ih=struct.pack(">IIBBBBB",1,1,8,2,0,0,0)
    return sig+chunk(b"IHDR",ih)+chunk(b"IDAT",zlib.compress(b"\x00\x20\x40\x60"))+chunk(b"IEND",b"")

def safe_event(e):
    item={"type":e.get("type")}
    for k in ("name","status","code","model"):
        if e.get(k) is not None: item[k]=e.get(k)
    if isinstance(e.get("calls"),list):
        item["calls"]=[{"name":c.get("name"),"has_id":bool(c.get("id"))} for c in e["calls"] if isinstance(c,dict)]
    if e.get("type")=="output_transcript":
        item["text_chars"]=len(str(e.get("text") or ""))
    return item

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--expected-sha",required=True); args=ap.parse_args()
    inst=installer(); inst.require_mode(inst.DEVICE_TOKEN_FILE,0o600)
    token,created=inst.device_token()
    if created: raise SystemExit("unexpected_token_creation")
    headers={"Authorization":"Bearer "+token,"Accept":"application/json"}
    events=[]; sid=""; story_id=""
    with httpx.Client(base_url=BASE_URL,headers=headers,timeout=40) as c:
        h=c.get("/healthz").json()
        if h.get("source_sha")!=args.expected_sha: raise SystemExit("sha_mismatch")
        photo=png(); sha=hashlib.sha256(photo).hexdigest(); tag=uuid.uuid4().hex[:12]
        r=c.post("/v1/stories",files={"photo":("probe.png",photo,"image/png")},
                 data={"client_story_id":"tool-probe-"+tag,"photo_sha256":sha,"voice_protocol":"voice-chunks-v2","lat":"54.7104","lon":"20.4522"},
                 headers={**headers,"Idempotency-Key":"tool-probe-"+tag,"X-Photo-SHA256":sha})
        story_id=r.json()["id"]
        try:
            started=c.post(f"/v1/stories/{story_id}/live-sessions").json(); sid=started["session_id"]
            first=c.get(f"/v1/stories/{story_id}/live-sessions/{sid}/events",params={"after":0}).json()
            events.extend(first.get("events",[])); cursor=int(first.get("cursor") or 0)
            c.post(f"/v1/stories/{story_id}/live-sessions/{sid}/input",json={"text":
                "Техническая проверка функций. Сначала ОБЯЗАТЕЛЬНО вызови read_topic. Затем ОБЯЗАТЕЛЬНО вызови search_web с запросом «Бранденбургские ворота Калининград официальный сайт история». После результатов ответь одним коротким предложением."})
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                p=c.get(f"/v1/stories/{story_id}/live-sessions/{sid}/events",params={"after":cursor}).json()
                batch=p.get("events",[]); events.extend(x for x in batch if isinstance(x,dict)); cursor=int(p.get("cursor") or cursor)
                names=[x.get("name") for x in events if x.get("type")=="tool_result"]
                if "search_web" in names and any(x.get("type")=="turn_complete" for x in events): break
                if any(x.get("type")=="error" for x in events): break
                time.sleep(.4)
        finally:
            if sid:
                try:c.post(f"/v1/stories/{story_id}/live-sessions/{sid}/stop",timeout=5)
                except Exception:pass
    print(json.dumps({"story_id":story_id,"session_id":sid,"events":[safe_event(e) for e in events],"secrets_disclosed":False},ensure_ascii=False,sort_keys=True))
if __name__=="__main__": main()
