#!/usr/bin/env python3
"""Normalize one non-protected story's fact inventory.

Dry-run by default. --apply mutates only the requested story after revision/state
preconditions and refuses stories with a frozen visual or publication state.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from devcoveer_story_diag import load_installer
from street_story.fact_quality import merge_fact_inventory


PROTECTED = {"scheduled", "published"}


def load_rows(db, story_id):
    return list(db.execute(
        "SELECT fact_id,text,confidence,evidence_supported,selected,sources_json "
        "FROM facts WHERE story_id=? ORDER BY rowid",
        (story_id,),
    ))


def normalize(rows):
    return merge_fact_inventory([
        {
            "fact_id": row["fact_id"],
            "claim_key": "",
            "text": row["text"],
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "sources": json.loads(row["sources_json"] or "[]"),
        }
        for row in rows
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    installer = load_installer()
    database = installer.DATA_ROOT / "street-story.sqlite3"
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        story = db.execute(
            "SELECT id,state,revision,research_json,visual_context_json FROM stories WHERE id=?",
            (args.story_id,),
        ).fetchone()
        if story is None:
            raise SystemExit("story_not_found")
        rows = load_rows(db, args.story_id)
        merged = normalize(rows)
        before = [
            {"fact_id": row["fact_id"], "text": row["text"], "selected": bool(row["selected"])}
            for row in rows
        ]
        after = [
            {
                "fact_id": item["fact_id"],
                "text": item["text"],
                "selected": bool(item["selected"] and item["evidence_supported"]),
                "source_count": len(item.get("sources") or []),
            }
            for item in merged
        ]
        report = {
            "story_id": args.story_id,
            "state": story["state"],
            "revision": story["revision"],
            "before_count": len(before),
            "after_count": len(after),
            "before": before,
            "after": after,
            "applied": False,
        }
        if not args.apply:
            print(json.dumps(report, ensure_ascii=False))
            return
        if story["state"] in PROTECTED:
            raise SystemExit("protected_story_state")
        visual = json.loads(story["visual_context_json"] or "{}")
        if visual and any(visual.get(key) for key in ("selected_asset_ref", "source_asset_ref", "operation_id")):
            raise SystemExit("visual_context_present_review_required")

        research = json.loads(story["research_json"] or "{}")
        research["claim_decisions"] = {
            item["fact_id"]: bool(item["selected"] and item["evidence_supported"])
            for item in merged
        }
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT state,revision FROM stories WHERE id=?", (args.story_id,)
        ).fetchone()
        if current["state"] in PROTECTED or current["revision"] != story["revision"]:
            db.rollback()
            raise SystemExit("story_changed_during_normalization")
        db.execute("DELETE FROM facts WHERE story_id=?", (args.story_id,))
        for item in merged:
            db.execute(
                "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    args.story_id,
                    item["fact_id"],
                    item["text"],
                    float(item["confidence"]),
                    int(bool(item["evidence_supported"])),
                    int(bool(item["selected"] and item["evidence_supported"])),
                    json.dumps(item.get("sources") or [], ensure_ascii=False, separators=(",", ":")),
                ),
            )
        db.execute(
            "UPDATE stories SET research_json=?,revision=revision+1,updated_at=strftime('%s','now') WHERE id=?",
            (json.dumps(research, ensure_ascii=False, separators=(",", ":")), args.story_id),
        )
        db.commit()
        report["applied"] = True
        report["new_revision"] = story["revision"] + 1
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
