#!/usr/bin/env python3
"""Bounded public Prussia39 reader used in the 2026-10-08 research.

This is a reproducibility helper, not a Street Story production adapter.
One command makes at most one request; repeats reuse a local content cache.
No reference photographs, authentication, or inference are requested.
The private cache contains copyrighted source pages: do not commit it.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.prussia39.ru"
DEFAULTS = {
    "text_n": "Название достопримечательности",
    "text_np": "Населенный пункт",
    "text_adr": "Адрес",
    "text_link": "Официальный сайт",
    "text_cph": "Код",
    "text_nph": "Номер телефона",
    "w_id_wout": "0", "w_id_photo": "0", "w_st_okn": "0",
}


def clean(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", fragment,
                      flags=re.S | re.I)
    fragment = re.sub(r"<(?:br\b[^>]*|/p|/tr|/h\d)>", "\n", fragment,
                      flags=re.I)
    fragment = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return "\n".join(re.sub(r"\s+", " ", line).strip()
                     for line in fragment.splitlines() if line.strip())


def decode(raw: bytes, content_type: str = "") -> str:
    # The tested site sends cp1251 / windows-1251, not UTF-8.
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if not m:
        m = re.search(r"charset=([\w-]+)", raw[:4096].decode("ascii", "ignore"), re.I)
    return raw.decode(m.group(1) if m else "cp1251", errors="strict")


def address_url(city: str, street: str, name: str | None = None, page: int = 1) -> str:
    params = dict(DEFAULTS)
    params.update(text_np=city, text_adr=street)
    if name:
        params["text_n"] = name
    if page > 1:
        params["p"] = str(page)
    # Full form defaults are necessary on the version observed on 2026-10-08.
    return BASE + "/sight/database.php?" + urllib.parse.urlencode(params, encoding="cp1251")


def nearby_payload(lat: float, lon: float) -> bytes:
    return urllib.parse.urlencode({"new_coords": f"{lat},{lon}"}).encode("ascii")


def fetch(url: str, cache: Path, body: bytes | None = None, timeout: float = 12) -> tuple[str, dict]:
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode() + b"\0" + (body or b"")).hexdigest()[:24]
    page, meta_path = cache / (key + ".html"), cache / (key + ".json")
    if page.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return decode(page.read_bytes(), meta.get("content_type", "")), {**meta, "cache_hit": True}
    started = time.monotonic()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "StreetStoryResearch/1.0"})
    meta = {"url": url, "method": "POST" if body else "GET",
            "form": body.decode() if body else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(1_000_001)
            meta.update(status=response.status, content_type=response.headers.get("Content-Type", ""))
        meta.update(bytes=len(raw), elapsed_seconds=round(time.monotonic() - started, 3),
                    sha256=hashlib.sha256(raw).hexdigest())
        if not raw:
            raise ValueError("empty_body: HTTP 200 is not evidence that no article exists")
        if len(raw) > 1_000_000:
            raise ValueError("body_limit: page exceeds the research limit")
        text = decode(raw, meta["content_type"])
        if "<html" not in text.lower():
            raise ValueError("unexpected_body: expected HTML")
        page.write_bytes(raw)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return text, meta
    except Exception as exc:
        meta.update(error=str(exc), elapsed_seconds=round(time.monotonic() - started, 3))
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        raise


def nearby_cards(page: str) -> list[dict]:
    names = re.search(r"var name_fgr\s*=\s*(\[.*?\]);", page, re.S)
    coords = re.search(r"var coords\s*=\s*(\[.*?\]);", page, re.S)
    if not names or not coords:
        raise ValueError("nearby_parse_unavailable: response is not a verified empty result")
    labels, points = json.loads(names.group(1)), json.loads(coords.group(1))
    if len(labels) != len(points):
        raise ValueError("nearby_parse_mismatch")
    result = []
    for label, point in zip(labels, points):
        sid = re.search(r"sid=(\d+)", label)
        if sid:
            result.append({"sid": int(sid.group(1)), "title": clean(label),
                           "coordinate": point,
                           "url": BASE + "/sight/index.php?sid=" + sid.group(1)})
    return result


def address_cards(page: str) -> dict:
    start = page.find("Результаты поиска")
    if start < 0:
        raise ValueError("address_parse_unavailable: returned form is not an empty search result")
    result_html = page[start:]
    result_html = result_html.split("ymaps.ready", 1)[0]
    sids = list(dict.fromkeys(int(x) for x in re.findall(r"index\.php\?sid=(\d+)", result_html)))
    readable = clean(result_html)
    total = re.search(r"всего совпадений:\s*(\d+)", readable)
    page_links = []
    for href in re.findall(r'''href=["']([^"']+)["']''', page, re.I):
        href = html.unescape(href)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if "p" in query and "text_adr" in query:
            page_links.append(urllib.parse.urljoin(BASE + "/sight/database.php", href))
    return {"sids": sids, "total_count": int(total.group(1)) if total else None,
            "pagination_urls": list(dict.fromkeys(page_links)), "result_text": readable}


def article_body(page: str) -> dict:
    title = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    # Body td observed on the source. A changed layout must be reported,
    # never silently replaced with navigation, comments, or photo captions.
    blocks = re.findall(r'<td\b[^>]*style="[^"]*text-align:\s*justify;?[^"]*"[^>]*>(.*?)</td>',
                        page, re.S | re.I)
    if not blocks:
        raise ValueError("article_body_unavailable")
    substantive = [clean(x) for x in blocks if not clean(x).startswith("Все фотографии")]
    if not substantive:
        raise ValueError("article_body_unavailable")
    body = max(substantive, key=len).split("Интерактивный путеводитель", 1)[0].strip()
    return {"title": clean(title.group(1)) if title else None, "body": body}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".prussia-private-cache"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("address"); p.add_argument("city"); p.add_argument("street"); p.add_argument("--name"); p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("nearby"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p = sub.add_parser("article"); p.add_argument("sid", type=int)
    args = parser.parse_args()
    body = None
    if args.command == "address":
        url = address_url(args.city, args.street, args.name, args.page); parse = address_cards
    elif args.command == "nearby":
        url = BASE + "/sight/map_coord.php"; body = nearby_payload(args.lat, args.lon); parse = nearby_cards
    else:
        url = BASE + f"/sight/index.php?sid={args.sid}"; parse = article_body
    page, meta = fetch(url, args.cache, body)
    print(json.dumps({"fetch": meta, "result": parse(page)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
