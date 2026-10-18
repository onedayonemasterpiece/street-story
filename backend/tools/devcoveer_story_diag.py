#!/usr/bin/env python3
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3


def load_installer():
    path = Path(__file__).resolve().parents[1] / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-id", required=True)
    args = parser.parse_args()
    installer = load_installer()
    database = installer.DATA_ROOT / "street-story.sqlite3"
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        story = db.execute(
            "SELECT state,error_code,error_message,visual_context_json FROM stories WHERE id=?",
            (args.story_id,),
        ).fetchone()
        job = db.execute(
            "SELECT id,state,attempts,last_error FROM jobs WHERE story_id=? AND kind='visual' ORDER BY created_at DESC LIMIT 1",
            (args.story_id,),
        ).fetchone()
    if not story:
        print(json.dumps({"status":"not_found","secrets_disclosed":False}))
        return 1
    try:
        visual = json.loads(story["visual_context_json"] or "{}")
    except ValueError:
        visual = {}
    safe_visual = {
        key: visual.get(key)
        for key in (
            "prompt_version",
            "prompt_sha256",
            "content_revision",
            "source_asset_ref",
            "operation_id",
            "visual_job_id",
            "candidate_id",
            "selected_asset_ref",
            "selected_sha256",
            "stale",
        )
        if visual.get(key) is not None
    }
    result = {
        "status":"ok",
        "story":{
            "state":story["state"],
            "error_code":story["error_code"],
            "error_message":story["error_message"],
            "visual":safe_visual,
        },
        "job":dict(job) if job else None,
        "secrets_disclosed":False,
    }
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
