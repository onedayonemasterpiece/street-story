#!/usr/bin/env python3
"""Sanitized read-only Gemini router diagnostics for Street Story production."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time

DB = Path("/home/dev/.local/state/street-story/data/street-story.sqlite3")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-id")
    args = parser.parse_args()
    if not DB.is_file():
        print(json.dumps({"status":"missing_db","secrets_disclosed":False}))
        return 1

    now = time.time()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        credentials = {
            row["key_id"]: {
                "key_id_prefix": str(row["key_id"])[:12],
                "disabled": bool(row["disabled"]),
                "busy_remaining": max(0, round(float(row["busy_until"] or 0)-now, 3)),
                "disabled_reason": str(row["disabled_reason"] or "")[:80] or None,
            }
            for row in db.execute(
                "SELECT key_id,disabled,disabled_reason,busy_until FROM gemini_credentials ORDER BY key_id"
            )
        }
        health = []
        for row in db.execute(
            """
            SELECT key_id,model,operation,cooldown_until,consecutive_failures,last_success,
                   last_selected,quota_state,retry_after,last_failure,minute_bucket,minute_used,
                   advisory_until,advisory_load,advisory_observed_at
            FROM gemini_key_health
            ORDER BY operation,model,key_id
            """
        ):
            item = dict(row)
            cred = credentials.get(row["key_id"], {})
            health.append({
                "key_id_prefix": str(row["key_id"])[:12],
                "model": row["model"],
                "operation": row["operation"],
                "disabled": cred.get("disabled", False),
                "busy_remaining": cred.get("busy_remaining", 0),
                "cooldown_remaining": max(0, round(float(row["cooldown_until"] or 0)-now, 3)),
                "advisory_remaining": max(0, round(float(row["advisory_until"] or 0)-now, 3)),
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "quota_state": row["quota_state"],
                "last_failure": row["last_failure"],
                "retry_after_remaining": (
                    max(0, round(float(row["retry_after"])-now, 3))
                    if row["retry_after"] is not None else None
                ),
                "last_success_age": (
                    round(now-float(row["last_success"]), 3)
                    if float(row["last_success"] or 0) > 0 else None
                ),
                "last_selected_age": (
                    round(now-float(row["last_selected"]), 3)
                    if float(row["last_selected"] or 0) > 0 else None
                ),
                "minute_used": int(row["minute_used"] or 0),
            })

        blocks = [
            {
                "key_id_prefix": str(row["key_id"])[:12],
                "model": row["model"],
                "reason": str(row["reason"] or "")[:80],
            }
            for row in db.execute(
                "SELECT key_id,model,reason FROM gemini_model_blocks ORDER BY model,key_id"
            )
        ]

        jobs = []
        if args.story_id:
            for row in db.execute(
                """
                SELECT id,kind,state,attempts,available_at,last_error,created_at,updated_at
                FROM jobs WHERE story_id=? ORDER BY created_at DESC LIMIT 12
                """,
                (args.story_id,),
            ):
                jobs.append({
                    "id": row["id"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "attempts": row["attempts"],
                    "available_in": round(float(row["available_at"] or 0)-now, 3),
                    "last_error_present": bool(row["last_error"]),
                    "age": round(now-float(row["created_at"]), 3),
                    "updated_age": round(now-float(row["updated_at"]), 3),
                })

        provider_models = {}
        provider_env = Path("/home/dev/.local/state/street-story/providers.env")
        if provider_env.is_file():
            for raw in provider_env.read_text(encoding="utf-8").splitlines():
                if "=" not in raw or raw.lstrip().startswith("#"):
                    continue
                key, value = raw.split("=", 1)
                key = key.strip()
                if key in {
                    "GEMINI_MODEL",
                    "GEMINI_FALLBACK_MODEL",
                    "GEMINI_TRANSCRIPTION_MODEL",
                    "GEMINI_TRANSCRIPTION_FALLBACK_MODEL",
                }:
                    provider_models[key] = value.strip().strip("'\"")

        print(json.dumps({
            "status":"ok",
            "provider_models": provider_models,
            "models": sorted({row["model"] for row in health}),
            "credential_count": len(credentials),
            "health": health,
            "model_blocks": blocks,
            "jobs": jobs,
            "secrets_disclosed": False,
        }, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
