#!/usr/bin/env python3
"""Bounded cleanup/retry for Street Story E2E acceptance jobs only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import time

DB=Path("/home/dev/.local/state/street-story/data/street-story.sqlite3")
PREFIX="live-e2e-"

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--current-story-id",required=True)
    args=parser.parse_args()
    if not DB.is_file():
        print(json.dumps({"status":"missing_db","changed":False}))
        return 1
    now=time.time()
    db=sqlite3.connect(DB)
    db.row_factory=sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        current=db.execute(
            "SELECT id,client_story_id FROM stories WHERE id=?",
            (args.current_story_id,),
        ).fetchone()
        if current is None or not str(current["client_story_id"]).startswith(PREFIX):
            print(json.dumps({"status":"current_story_not_e2e","changed":False}))
            return 2
        old_ids=[
            row["id"] for row in db.execute(
                "SELECT id FROM stories WHERE client_story_id LIKE ? AND id<>?",
                (PREFIX+"%",args.current_story_id),
            )
        ]
        stopped=0
        if old_ids:
            placeholders=",".join("?" for _ in old_ids)
            params=[now,*old_ids]
            cur=db.execute(
                f"UPDATE jobs SET state='failed',lease_until=0,last_error='e2e_superseded',updated_at=? "
                f"WHERE story_id IN ({placeholders}) AND state IN ('ready','retry','running')",
                params,
            )
            stopped=cur.rowcount
        rows=list(db.execute(
            "SELECT id,state,kind FROM jobs WHERE story_id=? AND kind IN ('research','refinement') "
            "AND state IN ('ready','retry') ORDER BY created_at",
            (args.current_story_id,),
        ))
        retried=0
        if rows:
            cur=db.execute(
                "UPDATE jobs SET state='retry',available_at=?,lease_until=0,updated_at=? "
                "WHERE id=? AND state IN ('ready','retry')",
                (now,now,rows[0]["id"]),
            )
            retried=cur.rowcount
        db.commit()
        print(json.dumps({
            "status":"ok",
            "old_e2e_story_count":len(old_ids),
            "stopped_old_jobs":stopped,
            "current_retry_released":retried,
            "current_story_id":args.current_story_id,
            "secrets_disclosed":False,
        },sort_keys=True))
        return 0
    finally:
        db.close()

if __name__=="__main__":
    raise SystemExit(main())
