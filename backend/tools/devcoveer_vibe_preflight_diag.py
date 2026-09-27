#!/usr/bin/env python3
"""Sanitized read-only diagnosis of Street Story VibePublish preview evidence."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import time


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def installer():
    path = root() / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = installer()
    module.require_mode(module.VIBE_PRINCIPAL_FILE, 0o600)
    principal = module.VIBE_PRINCIPAL_FILE.read_text(encoding="utf-8").strip()
    if not principal:
        raise SystemExit("principal_missing")

    db = sqlite3.connect(f"file:{module.VIBE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        actor = db.execute(
            "SELECT id,tenant_id,epoch,owner FROM principals WHERE id=?",
            (principal,),
        ).fetchone()
        if actor is None:
            raise SystemExit("principal_row_missing")
        binding = db.execute(
            """
            SELECT b.id AS binding_id,b.epoch AS binding_epoch,b.active,
                   d.label,c.provider,c.account_type
            FROM bindings b
            JOIN destinations d ON d.id=b.destination_id
            JOIN connections c ON c.id=d.connection_id
            WHERE b.principal_id=? AND b.alias=?
            ORDER BY b.epoch DESC LIMIT 1
            """,
            (principal, module.VIBE_ALIAS),
        ).fetchone()
        if binding is None:
            raise SystemExit("binding_row_missing")

        rows = db.execute(
            """
            SELECT o.id AS operation_id,o.actor_epoch,o.action,o.state AS operation_state,
                   o.complete,o.work_state,o.error,o.created,
                   a.binding_id,a.binding_epoch,a.dispatched,a.state AS attempt_state,
                   a.stage,a.plan
            FROM operations o
            LEFT JOIN attempts a ON a.operation_id=o.id
            WHERE o.principal_id=? AND o.action='publish'
            ORDER BY o.created DESC LIMIT 12
            """,
            (principal,),
        ).fetchall()

        attempts = []
        qualifying = 0
        now = time.time()
        for row in rows:
            plan = {}
            try:
                raw = json.loads(row["plan"] or "{}")
                if isinstance(raw, dict):
                    plan = raw
            except (TypeError, ValueError):
                pass
            summary = {
                "age_seconds": max(0, int(now - float(row["created"] or now))),
                "actor_epoch_match": row["actor_epoch"] == actor["epoch"],
                "binding_id_match": row["binding_id"] == binding["binding_id"],
                "binding_epoch_match": row["binding_epoch"] == binding["binding_epoch"],
                "dispatched": row["dispatched"],
                "attempt_state": row["attempt_state"],
                "stage": row["stage"],
                "operation_state": row["operation_state"],
                "complete": bool(row["complete"]),
                "work_state": row["work_state"],
                "error_present": row["error"] is not None,
                "plan_provider": plan.get("provider"),
                "plan_mode": plan.get("mode"),
                "plan_action": plan.get("action"),
                "plan_surface": plan.get("surface", "post"),
            }
            if (
                summary["binding_id_match"]
                and summary["binding_epoch_match"]
                and summary["dispatched"] == 0
                and summary["attempt_state"] == "needs_approval"
                and summary["stage"] == "awaiting_approval"
                and summary["actor_epoch_match"]
                and summary["operation_state"] == "needs_approval"
                and summary["complete"]
                and summary["work_state"] == "done"
                and not summary["error_present"]
                and summary["age_seconds"] <= 3600
                and summary["plan_provider"] == "telegram"
                and summary["plan_mode"] == "preview"
                and summary["plan_action"] == "publish"
                and summary["plan_surface"] == "post"
            ):
                qualifying += 1
            attempts.append(summary)

        print(json.dumps({
            "principal_prefix": principal[:24],
            "actor_epoch": actor["epoch"],
            "binding_epoch": binding["binding_epoch"],
            "binding_active": bool(binding["active"]),
            "provider": binding["provider"],
            "account_type": binding["account_type"],
            "recent_publish_rows": len(attempts),
            "qualifying_preview_rows": qualifying,
            "attempts": attempts,
            "secrets_disclosed": False,
        }, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
