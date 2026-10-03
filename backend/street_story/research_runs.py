from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


RUN_STATES = {
    "planned",
    "discovering",
    "fetching",
    "extracting",
    "reconciling",
    "verifying",
    "completed",
    "partial",
    "failed",
    "cancelled",
}
CHUNK_STATES = {
    "planned",
    "extracting",
    "extracted",
    "no_claims",
    "needs_context",
    "failed",
    "deferred",
    "cancelled",
}
SOURCE_STATES = {
    "discovered",
    "snippet_only",
    "fetching",
    "fetched",
    "partial",
    "failed",
    "deferred",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, value: str, length: int = 24) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def plan_text_chunks(text: str, *, target_chars: int = 6000, overlap_chars: int = 400) -> list[dict[str, Any]]:
    """Plan non-overlapping core spans plus bounded context overlap.

    Every character in the accepted normalized text belongs to exactly one core
    span. Context may overlap; core never does. No tail is silently dropped.
    """
    text = str(text or "")
    if not text:
        return []
    target = max(1200, int(target_chars))
    overlap = max(0, min(int(overlap_chars), target // 3))
    minimum = max(600, int(target * 0.55))
    chunks: list[dict[str, Any]] = []
    core_start = 0
    ordinal = 0
    length = len(text)

    while core_start < length:
        desired = min(length, core_start + target)
        core_end = desired
        if desired < length:
            lower = min(length, core_start + minimum)
            window = text[lower:desired]
            candidates = [
                window.rfind("\n\n"),
                window.rfind("\n"),
                window.rfind(". "),
                window.rfind("; "),
            ]
            best = max(candidates)
            if best >= 0:
                delimiter = 2 if window[best:best + 2] in {"\n\n", ". ", "; "} else 1
                core_end = lower + best + delimiter
            if core_end <= core_start:
                core_end = desired

        context_start = max(0, core_start - overlap)
        context_end = min(length, core_end + overlap)
        before = text[context_start:core_start]
        core = text[core_start:core_end]
        after = text[core_end:context_end]
        chunk_text = (
            ("[context_before]\n" + before + "\n" if before else "")
            + "[core]\n"
            + core
            + ("\n[context_after]\n" + after if after else "")
        )
        chunks.append(
            {
                "ordinal": ordinal,
                "core_start": core_start,
                "core_end": core_end,
                "context_start": context_start,
                "context_end": context_end,
                "text": chunk_text,
            }
        )
        ordinal += 1
        core_start = core_end

    return chunks


def begin_research_run(
    db,
    *,
    story_id: str,
    poi_key: str | None,
    goal: str,
    expected_story_revision: int,
    identity_generation: int,
    run_id: str | None,
    now: float,
) -> str:
    value = str(run_id or "").strip() or "research_" + uuid.uuid4().hex[:24]
    db.execute(
        "INSERT OR IGNORE INTO research_runs("
        "run_id,story_id,poi_key,goal,state,status_detail,expected_story_revision,"
        "identity_generation,created_at,updated_at"
        ") VALUES(?,?,?,?,?,'',?,?,?,?)",
        (
            value,
            story_id,
            poi_key,
            str(goal or "")[:2000],
            "planned",
            int(expected_story_revision),
            int(identity_generation),
            now,
            now,
        ),
    )
    return value


def set_run_state(
    db,
    run_id: str,
    state: str,
    *,
    detail: str = "",
    now: float,
    completed: bool = False,
) -> None:
    if state not in RUN_STATES:
        raise ValueError("research_run_state_invalid")
    db.execute(
        "UPDATE research_runs SET state=?,status_detail=?,updated_at=?,"
        "completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE run_id=?",
        (state, str(detail or "")[:1000], now, int(completed), now, run_id),
    )


def register_discovered_source(
    db,
    *,
    run_id: str,
    url: str,
    title: str,
    status: str,
    now: float,
    source_version_id: str | None = None,
    error_code: str | None = None,
) -> None:
    if status not in SOURCE_STATES:
        raise ValueError("research_source_state_invalid")
    clean_url = str(url or "").rstrip("/")
    if not clean_url.startswith("https://"):
        raise ValueError("research_source_url_invalid")
    db.execute(
        "INSERT INTO research_run_sources("
        "run_id,url,title,status,source_version_id,error_code,discovered_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id,url) DO UPDATE SET "
        "title=excluded.title,status=excluded.status,"
        "source_version_id=COALESCE(excluded.source_version_id,research_run_sources.source_version_id),"
        "error_code=excluded.error_code,updated_at=excluded.updated_at",
        (
            run_id,
            clean_url,
            str(title or clean_url)[:500],
            status,
            source_version_id,
            str(error_code or "")[:120] or None,
            now,
            now,
        ),
    )


def persist_source_version(
    db,
    *,
    run_id: str,
    requested_url: str,
    final_url: str,
    title: str,
    content_type: str,
    http_status: int,
    redirect_chain: list[str],
    normalized_text: str,
    read_status: str,
    now: float,
    target_chars: int = 6000,
    overlap_chars: int = 400,
) -> dict[str, Any]:
    clean_final = str(final_url or "").rstrip("/")
    document_id = _digest("doc_", clean_final, 24)
    content_sha = hashlib.sha256(str(normalized_text or "").encode("utf-8")).hexdigest()
    source_version_id = _digest(
        "srcv_",
        canonical(
            {
                "final_url": clean_final,
                "content_sha256": content_sha,
                "content_type": str(content_type or ""),
            }
        ),
        24,
    )
    db.execute(
        "INSERT INTO source_documents(document_id,canonical_url,title,created_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET "
        "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE source_documents.title END,"
        "updated_at=excluded.updated_at",
        (document_id, clean_final, str(title or "")[:500], now, now),
    )
    row = db.execute(
        "SELECT document_id FROM source_documents WHERE canonical_url=?",
        (clean_final,),
    ).fetchone()
    document_id = str(row["document_id"])
    db.execute(
        "INSERT OR IGNORE INTO source_versions("
        "source_version_id,document_id,requested_url,final_url,content_sha256,content_type,"
        "http_status,redirect_chain_json,normalized_text,read_status,char_count,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_version_id,
            document_id,
            str(requested_url or "")[:1000],
            clean_final,
            content_sha,
            str(content_type or "")[:160],
            int(http_status),
            canonical(list(redirect_chain or [])),
            str(normalized_text or ""),
            str(read_status or "complete")[:80],
            len(str(normalized_text or "")),
            now,
        ),
    )

    chunks = plan_text_chunks(
        str(normalized_text or ""),
        target_chars=target_chars,
        overlap_chars=overlap_chars,
    )
    for item in chunks:
        chunk_id = _digest(
            "chunk_",
            f"{source_version_id}:{item['core_start']}:{item['core_end']}",
            24,
        )
        db.execute(
            "INSERT OR IGNORE INTO source_chunks("
            "chunk_id,source_version_id,ordinal,core_start,core_end,context_start,context_end,"
            "chunk_text,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                chunk_id,
                source_version_id,
                int(item["ordinal"]),
                int(item["core_start"]),
                int(item["core_end"]),
                int(item["context_start"]),
                int(item["context_end"]),
                item["text"],
                now,
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO research_chunk_runs("
            "run_id,chunk_id,status,observation_count,error_code,model_name,prompt_version,updated_at"
            ") VALUES(?,?,'planned',0,NULL,'','',?)",
            (run_id, chunk_id, now),
        )

    register_discovered_source(
        db,
        run_id=run_id,
        url=str(requested_url or clean_final).rstrip("/"),
        title=title,
        status="fetched" if read_status == "complete" else "partial",
        source_version_id=source_version_id,
        error_code=None,
        now=now,
    )
    return {
        "document_id": document_id,
        "source_version_id": source_version_id,
        "content_sha256": content_sha,
        "read_status": read_status,
        "char_count": len(str(normalized_text or "")),
        "chunks": [
            {
                **item,
                "chunk_id": _digest(
                    "chunk_",
                    f"{source_version_id}:{item['core_start']}:{item['core_end']}",
                    24,
                ),
            }
            for item in chunks
        ],
    }


def mark_chunk(
    db,
    *,
    run_id: str,
    chunk_id: str,
    status: str,
    observation_count: int,
    model_name: str,
    prompt_version: str,
    now: float,
    error_code: str | None = None,
) -> None:
    if status not in CHUNK_STATES:
        raise ValueError("research_chunk_state_invalid")
    db.execute(
        "UPDATE research_chunk_runs SET status=?,observation_count=?,error_code=?,"
        "model_name=?,prompt_version=?,updated_at=? WHERE run_id=? AND chunk_id=?",
        (
            status,
            max(0, int(observation_count)),
            str(error_code or "")[:120] or None,
            str(model_name or "")[:120],
            str(prompt_version or "")[:120],
            now,
            run_id,
            chunk_id,
        ),
    )


def run_manifest(db, run_id: str) -> dict[str, Any]:
    run = db.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError("research_run_not_found")
    sources = [dict(row) for row in db.execute(
        "SELECT * FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url",
        (run_id,),
    )]
    chunks = [dict(row) for row in db.execute(
        "SELECT r.*,c.source_version_id,c.ordinal,c.core_start,c.core_end,c.context_start,c.context_end "
        "FROM research_chunk_runs r JOIN source_chunks c ON c.chunk_id=r.chunk_id "
        "WHERE r.run_id=? ORDER BY c.source_version_id,c.ordinal",
        (run_id,),
    )]
    counts = {
        "sources_discovered": len(sources),
        "sources_fetched": sum(item["status"] == "fetched" for item in sources),
        "sources_snippet_only": sum(item["status"] == "snippet_only" for item in sources),
        "sources_partial": sum(item["status"] == "partial" for item in sources),
        "sources_failed": sum(item["status"] == "failed" for item in sources),
        "sources_pending": sum(
            item["status"] in {"discovered", "fetching", "deferred"}
            for item in sources
        ),
        "chunks_planned": len(chunks),
        "chunks_completed": sum(item["status"] in {"extracted", "no_claims"} for item in chunks),
        "chunks_needs_context": sum(item["status"] == "needs_context" for item in chunks),
        "chunks_failed": sum(item["status"] == "failed" for item in chunks),
        "chunks_deferred": sum(item["status"] == "deferred" for item in chunks),
    }
    return {"run": dict(run), "sources": sources, "chunks": chunks, "counts": counts}


def manifest_complete(manifest: dict[str, Any]) -> bool:
    counts = manifest.get("counts") or {}
    return (
        int(counts.get("sources_partial") or 0) == 0
        and int(counts.get("sources_failed") or 0) == 0
        and int(counts.get("sources_pending") or 0) == 0
        and int(counts.get("chunks_failed") or 0) == 0
        and int(counts.get("chunks_deferred") or 0) == 0
        and int(counts.get("chunks_needs_context") or 0) == 0
        and int(counts.get("chunks_completed") or 0) == int(counts.get("chunks_planned") or 0)
    )
