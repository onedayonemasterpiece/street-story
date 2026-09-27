#!/usr/bin/env python3
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import httpx


def load_installer():
    path=Path(__file__).resolve().parents[1]/"deploy"/"devcoveer_install.py"
    spec=importlib.util.spec_from_file_location("street_story_devcoveer_install",path)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sanitize(value):
    if isinstance(value, dict):
        allowed={
            "operation_id","resource_id","state","message","operation_complete","progress",
            "visual_job_id","visual_revision","selected_asset_ref","selected_sha256",
            "executor","next_action","retry_safe","receipt_ref","poll_after_seconds",
            "error","candidates","worker_seen_at","revision","action"
        }
        return {k:sanitize(v) for k,v in value.items() if k in allowed}
    if isinstance(value,list):
        return [sanitize(v) for v in value[:20]]
    if isinstance(value,(str,int,float,bool)) or value is None:
        text=value
        if isinstance(text,str) and len(text)>1000:
            return text[:1000]
        return text
    return str(value)[:500]


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--operation-id",required=True)
    args=ap.parse_args()
    inst=load_installer()
    inst.require_mode(inst.VIBE_TOKEN_FILE,0o600)
    token=inst.VIBE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("vibe_token_missing")
    headers={
        "Authorization":"Bearer "+token,
        "Accept":"application/json",
        "Host":inst.VIBE_HTTP_HOST,
    }
    with httpx.Client(timeout=20) as client:
        response=client.get(
            inst.VIBE_BASE_URL+f"/v1/operations/{args.operation_id}",
            headers=headers,
        )
    try:
        payload=response.json()
    except ValueError:
        payload={"message":"non_json"}
    print(json.dumps({
        "http_status":response.status_code,
        "receipt":sanitize(payload),
        "secrets_disclosed":False,
    },ensure_ascii=False,sort_keys=True))
    return 0 if response.status_code==200 else 1


if __name__=="__main__":
    raise SystemExit(main())
