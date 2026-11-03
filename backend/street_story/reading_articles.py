"""Reading suggestions come from retained research; selection remains the author's."""
from __future__ import annotations

from urllib.parse import urlsplit
from .article_media import public_url


def shortlist(db, story_id: str, research: dict) -> list[dict]:
    candidates = {}
    rows = db.execute(
        "SELECT rs.url,rs.title,sv.normalized_text FROM research_run_sources rs "
        "JOIN research_runs rr ON rr.run_id=rs.run_id "
        "LEFT JOIN source_versions sv ON sv.source_version_id=rs.source_version_id "
        "WHERE rr.story_id=? ORDER BY rs.updated_at DESC LIMIT 80", (story_id,))
    for row in rows:
        url = public_url(row["url"])
        body = str(row["normalized_text"] or "")
        path = urlsplit(url).path.lower() if url else ""
        if not url or len(body) < 700 or not any(segment in path
                for segment in ("/tpost/", "/articles/", "/article/", "/history/", "/blog/")):
            continue
        candidates.setdefault(url, {"url": url, "title": str(row["title"] or url)[:140],
            "excerpt": body[:550], "reason": "Исторический материал из исследования"})
    suggested = []
    for item in research.get("reading_suggestions") or []:
        url = public_url(item.get("url")) if isinstance(item, dict) else None
        if url and url not in suggested:
            suggested.append(url)
            candidates[url] = {"url": url, "title": str(item.get("title") or urlsplit(url).hostname)[:140],
                "excerpt": candidates.get(url, {}).get("excerpt", ""),
                "reason": "Ссылка предложена автором; содержание ещё не оценено" }
    selected = {item.get("url") for item in research.get("reading_selected") or [] if isinstance(item, dict)}
    ranks = {item.get("url"): (i, item) for i, item in enumerate(research.get("reading_rank") or [])
             if isinstance(item, dict) and item.get("url") in candidates}
    ordered = sorted(candidates.values(), key=lambda item: (
        item["url"] not in selected, item["url"] not in suggested,
        ranks.get(item["url"], (99, {}))[0], -len(item["excerpt"])))
    result = []
    for item in ordered:
        review = ranks.get(item["url"], (99, {}))[1]
        if review.get("is_article") is False and item["url"] not in selected and item["url"] not in suggested:
            continue
        result.append({**item, "selected": item["url"] in selected,
            "reason": str(review.get("reason") or item["reason"])[:180]})
        if len(result) == 3:
            break
    return result
