#!/usr/bin/env python3
"""Read aggregate Street Story fact-conflict statistics; never mutates product state."""
from __future__ import annotations

import argparse
import json
import sqlite3

from devcoveer_story_diag import load_installer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poi-key")
    args = parser.parse_args()
    installer = load_installer()
    database = installer.DATA_ROOT / "street-story.sqlite3"
    where = " WHERE poi_key=?" if args.poi_key else ""
    params = (args.poi_key,) if args.poi_key else ()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) FROM fact_conflicts" + where, params).fetchone()[0]
        relation = {
            row["relation"]: row["n"]
            for row in db.execute(
                "SELECT relation,COUNT(*) AS n FROM fact_conflicts" + where + " GROUP BY relation",
                params,
            )
        }
        resolution_where = where + (" AND " if where else " WHERE ") + "final_resolution IS NOT NULL"
        resolutions = {
            row["final_resolution"]: row["n"]
            for row in db.execute(
                "SELECT final_resolution,COUNT(*) AS n FROM fact_conflicts"
                + resolution_where
                + " GROUP BY final_resolution",
                params,
            )
        }
        mira = db.execute(
            "SELECT COUNT(*) FROM fact_conflicts"
            + where
            + (" AND " if where else " WHERE ")
            + "arbitrated_by='mira'",
            params,
        ).fetchone()[0]
        open_count = db.execute(
            "SELECT COUNT(*) FROM fact_conflicts"
            + where
            + (" AND " if where else " WHERE ")
            + "(final_resolution IS NULL OR final_resolution='unresolved')",
            params,
        ).fetchone()[0]
    print(json.dumps({
        "poi_key": args.poi_key,
        "total": total,
        "open": open_count,
        "mira_arbitrated": mira,
        "relations": relation,
        "resolutions": resolutions,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
