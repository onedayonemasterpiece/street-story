from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def extraction_scope(goal: str) -> str:
    # Scope is supplied by the model, never inferred through semantic matching.
    return ' '.join(str(goal or '').split()).casefold()


def _run_scope(row) -> str:
    return extraction_scope(row['extraction_scope'] if 'extraction_scope' in row.keys() and row['extraction_scope'] else row['goal'])


def source_coverage(db, poi_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Only frozen, checked chunk receipts establish completed source coverage."""
    if not poi_keys:
        return {}
    placeholders = ','.join('?' for _ in poi_keys)
    rows = db.execute(
        f"SELECT s.*,r.goal,r.extraction_scope,r.state AS run_state,v.final_url,v.read_status FROM research_run_sources s "
        "JOIN research_runs r ON r.run_id=s.run_id JOIN source_versions v ON v.source_version_id=s.source_version_id "
        f"WHERE r.poi_key IN ({placeholders}) ORDER BY s.updated_at DESC", tuple(poi_keys))
    result: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        scope = _run_scope(row)
        urls = {str(row['url']), str(row['final_url'])}
        chunks = list(db.execute(
            'SELECT c.chunk_id,r.status,r.error_code,r.prompt_version FROM source_chunks c LEFT JOIN research_chunk_runs r '
            'ON r.chunk_id=c.chunk_id AND r.run_id=? WHERE c.source_version_id=? ORDER BY c.ordinal',
            (row['run_id'], row['source_version_id'])))
        complete = bool(chunks) and row['run_state'] not in {'cancelled', 'failed'} and row['status'] == 'fetched' and not row['error_code'] and row['read_status'] == 'complete' and all(
            c['status'] in {'extracted', 'no_claims'} and not c['error_code']
            and c['prompt_version'] == 'live-chunk-findings-v1'
            and _valid_checkpoint(db, row['run_id'], c['chunk_id']).get('reusable_terminal') for c in chunks)
        for url in urls:
            if (url, scope) in seen:
                continue
            seen.add((url, scope))
            result.setdefault(url, []).append({
                'scope': scope, 'source_version_id': row['source_version_id'], 'run_id': row['run_id'],
                'completed': complete, 'checked_at': row['updated_at'],
                'chunks_total': len(chunks), 'chunks_completed': sum(c['status'] in {'extracted', 'no_claims'} and not c['error_code'] for c in chunks),
            })
    return result


def _valid_checkpoint(db, run_id: str, chunk_id: str) -> dict[str, Any]:
    try:
        checkpoint = chunk_checkpoint(db, run_id, chunk_id)
        # Legacy empty/menu receipts did not distinguish readable article
        # content from a navigation dump. They cannot prove complete coverage.
        batches = list(db.execute('SELECT payload_json,batch_index,status,error_code FROM research_chunk_batches WHERE run_id=? AND chunk_id=? ORDER BY batch_index',
                                  (run_id, chunk_id)))
        prefix = [row for row in batches if row['batch_index'] < checkpoint['next_batch_index']]
        checked = [row['batch_index'] for row in prefix] == list(range(checkpoint['next_batch_index']))
        for row in prefix:
            payload = json.loads(row['payload_json'] or '{}')
            checked = checked and not row['error_code'] and (payload.get('source_content_valid') is True or bool(payload.get('facts')))
        checkpoint['article_content_checked'] = checked
        checkpoint['reusable_terminal'] = checkpoint['terminal'] and checked and all(
            row['status'] in {'completed', 'continuation'} and not row['error_code'] for row in batches)
        return checkpoint
    except ValueError:
        logger.warning('street_story_chunk_reuse_rejected run_id=%s chunk_id=%s reason=invalid_checkpoint', run_id, chunk_id)
        return {}


def reuse_chunk_checkpoint(db, run_id: str, chunk_id: str, now: float,
                          prompt_version: str = 'live-chunk-findings-v1') -> bool:
    """Attach an immutable checkpoint snapshot; never copy observations or review decisions."""
    run = db.execute('SELECT * FROM research_runs WHERE run_id=?', (run_id,)).fetchone()
    if not run or not run['poi_key']:
        return False
    from .poi_memory import memory_keys
    keys = memory_keys(db, {'candidate_id': run['poi_key']})
    placeholders = ','.join('?' for _ in keys)
    donors = db.execute(
        "SELECT c.*,r.goal,r.extraction_scope,r.story_id,v.access_scope FROM research_chunk_runs c "
        "JOIN research_runs r ON r.run_id=c.run_id JOIN source_chunks sc ON sc.chunk_id=c.chunk_id "
        "JOIN source_versions v ON v.source_version_id=sc.source_version_id "
        f"WHERE c.chunk_id=? AND c.run_id<>? AND r.poi_key IN ({placeholders}) "
        "AND c.prompt_version=? AND c.error_code IS NULL AND c.status IN ('extracted','no_claims','deferred','extracting') "
        "AND r.state NOT IN ('cancelled','failed') "
        "AND NOT EXISTS (SELECT 1 FROM research_run_sources s JOIN source_chunks sc "
        "ON sc.source_version_id=s.source_version_id WHERE s.run_id=c.run_id AND sc.chunk_id=c.chunk_id "
        "AND s.error_code='not_article_text') "
        "ORDER BY CASE WHEN c.status IN ('extracted','no_claims') THEN 0 ELSE 1 END,c.updated_at DESC",
        (chunk_id, run_id, *keys, prompt_version))
    for donor in donors:
        if donor['story_id'] != run['story_id'] and donor['access_scope'] != 'public':
            continue
        if donor['story_id'] != run['story_id']:
            donor_run = db.execute('SELECT * FROM research_runs WHERE run_id=?', (donor['run_id'],)).fetchone()
            if not _confirmed_run_keys(db, run).intersection(_confirmed_run_keys(db, donor_run)):
                continue
        if _run_scope(donor) != _run_scope(run):
            continue
        checkpoint = _valid_checkpoint(db, donor['run_id'], chunk_id)
        if not checkpoint:
            continue
        if not checkpoint['article_content_checked']:
            logger.info('street_story_chunk_reuse_rejected run_id=%s chunk_id=%s donor_run_id=%s reason=legacy_empty_content_unverified',
                        run_id, chunk_id, donor['run_id'])
            continue
        if checkpoint['terminal'] and not checkpoint['reusable_terminal']:
            logger.info('street_story_chunk_reuse_rejected run_id=%s chunk_id=%s donor_run_id=%s reason=incomplete_terminal_receipt',
                        run_id, chunk_id, donor['run_id'])
            continue
        if checkpoint['payload_missing'] or not checkpoint['next_batch_index']:
            continue
        rows = list(db.execute(
            'SELECT * FROM research_chunk_batches WHERE run_id=? AND chunk_id=? AND batch_index<? ORDER BY batch_index',
            (donor['run_id'], chunk_id, checkpoint['next_batch_index'])))
        if not rows or any(row['prompt_version'] != prompt_version for row in rows):
            continue
        for row in rows:
            record_chunk_batch(db, run_id=run_id, chunk_id=chunk_id, batch_index=row['batch_index'],
                status='continuation' if row['status'] == 'deferred' else row['status'],
                raw_fact_count=row['raw_fact_count'], accepted_fact_count=row['accepted_fact_count'],
                continuation_needed=bool(row['continuation_needed']), continuation_reason=row['continuation_reason'],
                model_name=row['model_name'], prompt_version=row['prompt_version'], now=now,
                payload=json.loads(row['payload_json']))
        kind = 'skipped_completed' if checkpoint['terminal'] else 'resumed_partial'
        db.execute('UPDATE research_chunk_runs SET status=?,observation_count=?,model_name=?,prompt_version=?,reuse_from_run_id=?,reuse_kind=? WHERE run_id=? AND chunk_id=?',
                   (donor['status'] if checkpoint['terminal'] else 'deferred', donor['observation_count'],
                    donor['model_name'], donor['prompt_version'], donor['run_id'], kind, run_id, chunk_id))
        logger.info('street_story_chunk_reuse run_id=%s chunk_id=%s donor_run_id=%s reason=%s next_batch=%s',
                    run_id, chunk_id, donor['run_id'], kind, checkpoint['next_batch_index'])
        return True
    return False


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


def acquire_chunk_lease(db, *, run_id, chunk_id, owner, now, ttl=180):
    """Fence one frozen core/scope shared by Live and background extractors."""
    row = db.execute('SELECT r.*,x.extraction_scope,x.poi_key FROM research_chunk_runs r '
                     'JOIN research_runs x ON x.run_id=r.run_id WHERE r.run_id=? AND r.chunk_id=?',
                     (run_id,chunk_id)).fetchone()
    if row is None:
        return None
    held = db.execute('SELECT r.run_id,r.lease_owner FROM research_chunk_runs r JOIN research_runs x ON x.run_id=r.run_id '
                      'WHERE r.chunk_id=? AND x.extraction_scope=? AND x.poi_key IS ? AND r.lease_until>? '
                      'AND r.lease_owner IS NOT NULL', (chunk_id,row['extraction_scope'],row['poi_key'],now)).fetchall()
    if any(item['lease_owner'] != owner or item['run_id'] != run_id for item in held):
        return None
    fence = int(row['lease_fence']) + (0 if row['lease_owner'] == owner and row['lease_until'] > now else 1)
    db.execute('UPDATE research_chunk_runs SET lease_owner=?,lease_until=?,lease_fence=? WHERE run_id=? AND chunk_id=?',
               (owner,now+ttl,fence,run_id,chunk_id))
    return fence


def chunk_lease_owned(db, *, run_id, chunk_id, owner, fence, now):
    return fence is not None and db.execute('SELECT 1 FROM research_chunk_runs WHERE run_id=? AND chunk_id=? '
        'AND lease_owner=? AND lease_fence=? AND lease_until>?', (run_id,chunk_id,owner,fence,now)).fetchone() is not None


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
    scope: str | None = None,
) -> str:
    value = str(run_id or "").strip() or "research_" + uuid.uuid4().hex[:24]
    db.execute(
        "INSERT OR IGNORE INTO research_runs("
        "run_id,story_id,poi_key,goal,extraction_scope,state,status_detail,expected_story_revision,"
        "identity_generation,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,'',?,?,?,?)",
        (
            value,
            story_id,
            poi_key,
            str(goal or "")[:2000],
            extraction_scope(scope if scope is not None else goal)[:2000],
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
    access_scope: str = 'story',
) -> dict[str, Any]:
    if access_scope not in {'story', 'public'}:
        raise ValueError('research_source_access_scope_invalid')
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
                "read_status": str(read_status or 'complete'),
                "access_scope": access_scope,
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
        "http_status,redirect_chain_json,normalized_text,read_status,char_count,created_at,access_scope"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            access_scope,
        ),
    )

    # Source bytes and the partition used for their receipts are immutable.
    # A different consumer's window size must not add overlapping chunks to a
    # snapshot that already has checked extraction coverage.
    chunks = [dict(row) for row in db.execute(
        'SELECT ordinal,core_start,core_end,context_start,context_end,chunk_text AS text '
        'FROM source_chunks WHERE source_version_id=? ORDER BY ordinal', (source_version_id,))]
    if not chunks:
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
        attached = db.execute(
            "INSERT OR IGNORE INTO research_chunk_runs("
            "run_id,chunk_id,status,observation_count,error_code,model_name,prompt_version,updated_at"
            ") VALUES(?,?,'planned',0,NULL,'','',?)",
            (run_id, chunk_id, now),
        ).rowcount
        if attached:
            reuse_chunk_checkpoint(db, run_id, chunk_id, now)

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


def record_chunk_batch(
    db,
    *,
    run_id: str,
    chunk_id: str,
    batch_index: int,
    status: str,
    raw_fact_count: int,
    accepted_fact_count: int,
    continuation_needed: bool,
    continuation_reason: str,
    model_name: str,
    prompt_version: str,
    now: float,
    error_code: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    if status not in {"completed", "continuation", "failed", "deferred"}:
        raise ValueError("research_chunk_batch_state_invalid")
    batch_id = _digest(
        "chunkbatch_",
        f"{run_id}:{chunk_id}:{int(batch_index)}",
        24,
    )
    payload_json = canonical(payload) if payload is not None else ""
    payload_sha256 = (
        hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if payload_json
        else ""
    )
    existing = db.execute("SELECT payload_sha256 FROM research_chunk_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if existing and existing["payload_sha256"] and payload_sha256 and existing["payload_sha256"] != payload_sha256:
        raise ValueError("research_chunk_batch_replay_payload_mismatch")
    db.execute(
        "INSERT INTO research_chunk_batches("
        "batch_id,run_id,chunk_id,batch_index,status,raw_fact_count,accepted_fact_count,"
        "continuation_needed,continuation_reason,error_code,model_name,prompt_version,"
        "payload_json,payload_sha256,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id,chunk_id,batch_index) DO UPDATE SET "
        "status=excluded.status,raw_fact_count=excluded.raw_fact_count,"
        "accepted_fact_count=excluded.accepted_fact_count,"
        "continuation_needed=excluded.continuation_needed,"
        "continuation_reason=excluded.continuation_reason,error_code=excluded.error_code,"
        "model_name=excluded.model_name,prompt_version=excluded.prompt_version,"
        "payload_json=CASE WHEN excluded.payload_json<>'' THEN excluded.payload_json "
        "ELSE research_chunk_batches.payload_json END,"
        "payload_sha256=CASE WHEN excluded.payload_sha256<>'' THEN excluded.payload_sha256 "
        "ELSE research_chunk_batches.payload_sha256 END",
        (
            batch_id,
            run_id,
            chunk_id,
            max(0, int(batch_index)),
            status,
            max(0, int(raw_fact_count)),
            max(0, int(accepted_fact_count)),
            int(bool(continuation_needed)),
            str(continuation_reason or "")[:1000],
            str(error_code or "")[:120] or None,
            str(model_name or "")[:120],
            str(prompt_version or "")[:120],
            payload_json,
            payload_sha256,
            now,
        ),
    )
    return batch_id



def _confirmed_run_keys(db, run) -> set[str]:
    story = db.execute('SELECT research_json FROM stories WHERE id=?', (run['story_id'],)).fetchone()
    if not story:
        return set()
    research = json.loads(story['research_json'] or '{}')
    identity = research.get('visual_identity') or {}
    if (identity.get('status') not in {'match', 'owner_confirmed'}
            or int(research.get('identity_generation') or 0) != int(run['identity_generation'])
            or not identity.get('candidate_id')):
        return set()
    from .poi_memory import memory_keys
    keys = set(memory_keys(db, identity))
    return keys if run['poi_key'] in keys else set()


def attach_source_version(db, *, run_id: str, source_version_id: str,
                          requested_url: str, now: float) -> dict[str, Any] | None:
    """Attach authorized frozen text; media enumeration/comparison stay separate.

    A URL's appearance in another story grants no access to that story. Only
    explicitly public article text for the same confirmed physical POI may
    cross story boundaries. No private image, voice, draft or verdict is copied.
    """
    target = db.execute('SELECT * FROM research_runs WHERE run_id=?', (run_id,)).fetchone()
    if not target or target['state'] in {'cancelled', 'failed', 'completed'}:
        return None
    story = db.execute('SELECT research_json FROM stories WHERE id=?', (target['story_id'],)).fetchone()
    if not story or int(json.loads(story['research_json'] or '{}').get('identity_generation') or 0) != int(target['identity_generation']):
        return None
    url = str(requested_url or '').rstrip('/')
    version = db.execute('SELECT v.*,d.title FROM source_versions v JOIN source_documents d '
                         'ON d.document_id=v.document_id WHERE v.source_version_id=?', (source_version_id,)).fetchone()
    if (not version or not url.startswith('https://') or not version['normalized_text'].strip()
            or int(version['http_status']) != 200
            or version['read_status'] not in {'complete', 'partial', 'partial_text_limit'}):
        return None
    if hashlib.sha256(version['normalized_text'].encode('utf-8')).hexdigest() != version['content_sha256']:
        logger.warning('street_story_source_snapshot_rejected run_id=%s source_version_id=%s reason=content_digest_mismatch',
                       run_id, source_version_id)
        return None
    existing = db.execute('SELECT source_version_id FROM research_run_sources WHERE run_id=? AND url=?', (run_id, url)).fetchone()
    if existing and existing['source_version_id'] and existing['source_version_id'] != source_version_id:
        return None  # A run's already acquired version is frozen.
    donors = list(db.execute('SELECT r.*,s.url AS source_url FROM research_run_sources s '
                            'JOIN research_runs r ON r.run_id=s.run_id WHERE s.source_version_id=? '
                            "AND s.error_code IS NULL AND s.status IN ('fetched','partial')", (source_version_id,)))
    target_keys = _confirmed_run_keys(db, target) if version['access_scope'] == 'public' else set()
    authorized = any(
        url in {donor['source_url'], version['requested_url'], version['final_url']}
        and (donor['story_id'] == target['story_id']
             or (target_keys and target_keys.intersection(_confirmed_run_keys(db, donor))))
        for donor in donors
    )
    if not authorized:
        return None
    register_discovered_source(db, run_id=run_id, url=url, title=version['title'],
                              status='fetched' if version['read_status'] == 'complete' else 'partial',
                              source_version_id=source_version_id, now=now)
    for chunk in db.execute('SELECT chunk_id FROM source_chunks WHERE source_version_id=? ORDER BY ordinal', (source_version_id,)):
        added = db.execute("INSERT OR IGNORE INTO research_chunk_runs(run_id,chunk_id,status,updated_at) VALUES(?,?,'planned',?)",
                           (run_id, chunk['chunk_id'], now)).rowcount
        if added:
            reuse_chunk_checkpoint(db, run_id, chunk['chunk_id'], now)
    logger.info('street_story_source_snapshot_reuse run_id=%s source_version_id=%s access_scope=%s read_status=%s',
                run_id, source_version_id, version['access_scope'], version['read_status'])
    return saved_run_document(db, run_id, url)


def reusable_source_document(db, *, run_id: str, url: str, now: float,
                             max_age_seconds: float = 86400) -> dict[str, Any] | None:
    """Reuse an authorized fresh snapshot, independently of extraction scope."""
    target = db.execute('SELECT r.*,s.research_json FROM research_runs r JOIN stories s ON s.id=r.story_id WHERE run_id=?',
                        (run_id,)).fetchone()
    if (not target or target['state'] in {'cancelled', 'failed', 'completed'}
            or int(json.loads(target['research_json'] or '{}').get('identity_generation') or 0) != int(target['identity_generation'])):
        return None
    saved = saved_run_document(db, run_id, url)
    if saved is not None:
        return saved
    clean_url = str(url or '').rstrip('/')
    rows = db.execute('SELECT DISTINCT v.source_version_id,v.created_at FROM source_versions v '
                      'LEFT JOIN research_run_sources s ON s.source_version_id=v.source_version_id '
                      'WHERE (v.requested_url=? OR v.final_url=? OR s.url=?) '
                      'AND v.created_at<=? AND v.created_at>=? ORDER BY v.created_at DESC',
                      (clean_url, clean_url, clean_url, now, now - max(0, float(max_age_seconds))))
    for row in rows:
        document = attach_source_version(db, run_id=run_id, source_version_id=row['source_version_id'],
                                         requested_url=clean_url, now=now)
        if document is not None:
            return document
    return None


def saved_run_document(db, run_id: str, url: str) -> dict[str, Any] | None:
    """Read the frozen version already attached to this run, without refetching."""
    row = db.execute(
        "SELECT v.*,s.title FROM research_run_sources s JOIN source_versions v "
        "ON v.source_version_id=s.source_version_id WHERE s.run_id=? AND s.url=?",
        (run_id, str(url).rstrip("/")),
    ).fetchone()
    if row is None:
        return None
    document = dict(row)
    document["requested_url"] = str(url).rstrip("/")
    document["redirect_chain"] = json.loads(document.pop("redirect_chain_json") or "[]")
    document["chunks"] = [dict(item) for item in db.execute(
        "SELECT *,chunk_text AS text FROM source_chunks WHERE source_version_id=? ORDER BY ordinal",
        (document["source_version_id"],),
    )]
    return document


def chunk_checkpoint(db, run_id: str, chunk_id: str) -> dict[str, Any]:
    chunk = db.execute(
        "SELECT status,observation_count,error_code FROM research_chunk_runs "
        "WHERE run_id=? AND chunk_id=?",
        (run_id, chunk_id),
    ).fetchone()
    if chunk is None:
        raise KeyError("research_chunk_not_found")

    rows = list(
        db.execute(
            "SELECT batch_index,status,raw_fact_count,accepted_fact_count,"
            "continuation_needed,continuation_reason,error_code,payload_json,payload_sha256 "
            "FROM research_chunk_batches WHERE run_id=? AND chunk_id=? "
            "ORDER BY batch_index",
            (run_id, chunk_id),
        )
    )
    facts: list[dict[str, Any]] = []
    official_urls: list[str] = []
    next_batch_index = 0
    continuation_batches = 0
    payload_missing = False
    raw_fact_count = 0
    accepted_fact_count = 0
    resumable_deferred_batches: list[int] = []
    passage_cursor = 0
    read_passage_ids: set[int] = set()

    for row in rows:
        raw_fact_count += int(row["raw_fact_count"] or 0)
        accepted_fact_count += int(row["accepted_fact_count"] or 0)
        status = str(row["status"])
        if status == "continuation":
            continuation_batches += 1
        payload_json = str(row["payload_json"] or "")
        payload: dict[str, Any] | None = None
        if payload_json:
            expected = str(row["payload_sha256"] or "")
            actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            if expected and expected != actual:
                raise ValueError("research_chunk_batch_payload_digest_mismatch")
            try:
                decoded = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise ValueError("research_chunk_batch_payload_invalid") from exc
            if not isinstance(decoded, dict):
                raise ValueError("research_chunk_batch_payload_invalid")
            payload = decoded
            if type(payload.get('next_passage_cursor')) is int:
                passage_cursor = max(0, payload['next_passage_cursor'])
            read_passage_ids.update(value for value in payload.get('read_passage_ids') or [] if type(value) is int and value >= 0)
            for fact in payload.get("facts") or []:
                if isinstance(fact, dict):
                    facts.append(dict(fact))
            for url in payload.get("official_source_urls") or []:
                clean = str(url or "").rstrip("/")
                if clean.startswith("https://") and clean not in official_urls:
                    official_urls.append(clean)

        batch_index = int(row["batch_index"])
        if status in {"completed", "continuation"}:
            if payload is None:
                payload_missing = True
                next_batch_index = batch_index
                break
            next_batch_index = batch_index + 1
        elif status == "deferred" and payload is not None:
            # A bounded invocation ended after accepting this payload. Resume
            # from the next batch and turn the old row into continuation.
            next_batch_index = batch_index + 1
            resumable_deferred_batches.append(batch_index)
        elif status in {"failed", "deferred"}:
            # Retry only a batch that produced no durable accepted payload.
            next_batch_index = batch_index
            break

    terminal = str(chunk["status"]) in {"extracted", "no_claims"}
    if terminal and rows:
        terminal_payload_ok = all(
            bool(str(row["payload_json"] or ""))
            for row in rows
            if str(row["status"]) in {"completed", "continuation"}
        )
        payload_missing = payload_missing or not terminal_payload_ok
    elif terminal and not rows:
        payload_missing = True

    return {
        "status": str(chunk["status"]),
        "terminal": terminal and not payload_missing,
        "facts": facts,
        "official_source_urls": official_urls,
        "next_batch_index": next_batch_index,
        "continuation_batches": continuation_batches,
        "payload_missing": payload_missing,
        "raw_fact_count": raw_fact_count,
        "accepted_fact_count": accepted_fact_count,
        "resumable_deferred_batches": resumable_deferred_batches,
        "passage_cursor": passage_cursor,
        "read_passage_ids": sorted(read_passage_ids),
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
    batches = [dict(row) for row in db.execute(
        "SELECT batch_id,run_id,chunk_id,batch_index,status,raw_fact_count,"
        "accepted_fact_count,continuation_needed,continuation_reason,error_code,"
        "model_name,prompt_version,payload_sha256,"
        "CASE WHEN payload_json<>'' THEN 1 ELSE 0 END AS payload_saved,created_at "
        "FROM research_chunk_batches WHERE run_id=? ORDER BY chunk_id,batch_index",
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
        "chunks_skipped_completed": sum(item.get('reuse_kind') == 'skipped_completed' for item in chunks),
        "chunks_resumed_partial": sum(item.get('reuse_kind') == 'resumed_partial' for item in chunks),
        "chunk_batches_total": len(batches),
        "chunk_batches_continuation": sum(item["status"] == "continuation" for item in batches),
        "chunk_batches_failed": sum(item["status"] == "failed" for item in batches),
        "chunk_batches_deferred": sum(item["status"] == "deferred" for item in batches),
        "chunk_batches_payload_missing": sum(
            item["status"] in {"completed", "continuation"}
            and not bool(item["payload_saved"])
            for item in batches
        ),
        "terminal_chunks_payload_missing": sum(
            item["status"] in {"extracted", "no_claims"}
            and not chunk_checkpoint(db, run_id, item["chunk_id"])["terminal"]
            for item in chunks
        ),
    }
    return {
        "run": dict(run),
        "sources": sources,
        "chunks": chunks,
        "chunk_batches": batches,
        "counts": counts,
    }


def manifest_complete(manifest: dict[str, Any]) -> bool:
    counts = manifest.get("counts") or {}
    return (
        int(counts.get("sources_discovered") or 0) > 0
        and int(counts.get("sources_partial") or 0) == 0
        and int(counts.get("sources_failed") or 0) == 0
        and int(counts.get("sources_pending") or 0) == 0
        and int(counts.get("chunks_failed") or 0) == 0
        and int(counts.get("chunks_deferred") or 0) == 0
        and int(counts.get("chunks_needs_context") or 0) == 0
        and int(counts.get("chunk_batches_failed") or 0) == 0
        and int(counts.get("chunk_batches_deferred") or 0) == 0
        and int(counts.get("chunk_batches_payload_missing") or 0) == 0
        and int(counts.get("terminal_chunks_payload_missing") or 0) == 0
        and int(counts.get("chunks_completed") or 0) == int(counts.get("chunks_planned") or 0)
    )
