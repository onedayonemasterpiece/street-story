#!/usr/bin/env python3
"""Direct owner operator; MCP run_tool executes this, not a model or new daemon."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from street_story.db import Store  # noqa: E402
from street_story.poi_intake import execute, instructions, readback  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instructions', action='store_true')
    parser.add_argument('--database', type=Path, help='Exact existing canonical product database; no implicit empty DB')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--request-file', type=Path)
    group.add_argument('--request-part', action='append', help='JSON text segments, joined without separators; no code evaluation')
    group.add_argument('--readback', help='JSON object with campaign, packet_id, poi_id or story_id')
    args = parser.parse_args()
    if args.instructions:
        print(json.dumps(instructions(), ensure_ascii=False))
        return 0
    if not args.database or not args.database.is_absolute():
        parser.error('--database must be an explicit absolute existing path')
    try:
        store = Store.open_existing(args.database)
        with store.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='poi_intake_packets'").fetchone():
                raise ValueError('intake_schema_not_installed_deploy_product_first')
        if args.readback is not None:
            result = readback(store, **json.loads(args.readback))
        else:
            if args.request_file:
                if args.request_file.stat().st_size > 256 * 1024:
                    raise ValueError('intake_packet_too_large')
                body = args.request_file.read_text(encoding='utf-8')
            else:
                body = ''.join(args.request_part or [])
            if not body or len(body.encode()) > 256 * 1024:
                raise ValueError('intake_request_missing_or_too_large')
            result = asyncio.run(execute(store, json.loads(body)))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, httpx.HTTPError) as exc:
        # Validation input may contain private text: do not echo payloads/tracebacks.
        message = str(exc)
        code = message if message.isascii() and len(message) <= 160 and '\n' not in message else type(exc).__name__
        print(json.dumps({'ok': False, 'error': code}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
