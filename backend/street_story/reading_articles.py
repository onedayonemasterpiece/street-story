"""Reading suggestions use current-object source versions; the model ranks their content."""
from __future__ import annotations

from .article_media import public_url


def candidates(db, story_id: str, research: dict) -> list[dict]:
    rows = db.execute(
        "SELECT rs.url,rs.title,sv.source_version_id,sv.normalized_text,sv.read_status "
        "FROM research_run_sources rs JOIN research_runs rr ON rr.run_id=rs.run_id "
        "JOIN source_versions sv ON sv.source_version_id=rs.source_version_id "
        "WHERE rr.story_id=? AND rr.identity_generation=? ORDER BY rs.updated_at DESC",
        (story_id, int(research.get("identity_generation") or 0)))
    result = {}
    for row in rows:
        url = public_url(row["url"])
        if url and row["normalized_text"] and not any(char in url for char in ' )[]\n'):
            result.setdefault(url, {"url": url, "title": str(row["title"] or url)[:140],
                "source_version_id": row["source_version_id"], "read_status": row["read_status"],
                "excerpt": row["normalized_text"]})
    return list(result.values())


def shortlist(db, story_id: str, research: dict) -> list[dict]:
    pool = {item["url"]: item for item in candidates(db, story_id, research)}
    selected = {item.get("url") for item in research.get("reading_selected") or []}
    ranked = [item for item in research.get("reading_rank") or []
        if isinstance(item, dict) and item.get("url") in pool and item.get("is_article") is True
        and item.get("source_version_id") == pool[item["url"]]["source_version_id"]]
    ranked.sort(key=lambda item: item["url"] not in selected)
    return [{"url": item["url"], "title": pool[item["url"]]["title"],
        "reason": str(item.get("reason") or "")[:180], "selected": item["url"] in selected}
        for item in ranked[:3]]
