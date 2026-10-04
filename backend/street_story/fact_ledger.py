from __future__ import annotations

import hashlib
import json
from typing import Any

from .model_facts import normalized_claim_key, validated_model_fact_text

REVIEW_STATUSES = {"unreviewed", "eligible", "disputed", "withheld", "quarantined"}
ELIGIBILITY = {"unreviewed", "eligible", "withheld"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(prefix: str, value: str, size: int = 24) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]


def candidate_assertion_id(claim_key: Any, text: Any) -> str:
    """Server identity for a new assertion candidate.

    A model semantic key is an indexing hint, not authority to overwrite a
    different value. Therefore both the key and the exact bounded statement are
    part of the opaque ID. Explicit existing_fact_id is handled separately.
    """
    value = validated_model_fact_text(text)
    if value is None:
        raise ValueError("fact_text_invalid")
    key = normalized_claim_key(claim_key)
    if key is None or key.startswith("exact-text:"):
        # Preserve the pre-ledger exact-text identity contract so existing
        # selections and clients keep stable references during migration.
        return _sha("fact_", value.casefold(), 20)
    return _sha("claim_", key + "\n" + value.casefold(), 20)


def _source_key(source: dict[str, Any]) -> str:
    url = str(source.get("url") or "").rstrip("/")
    if url:
        return "url:" + url
    ref = str(source.get("ref") or source.get("evidence_ref") or "").strip()
    return "ref:" + ref if ref else ""


def _support_key(source_url: str, support: dict[str, Any]) -> str:
    return _canonical(
        {
            "kind": str(support.get("kind") or "support"),
            "source_url": str(support.get("source_url") or source_url).rstrip("/"),
            "text": str(support.get("text") or "").strip(),
        }
    )


def merge_source_payloads(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mechanical union that never replaces passages merely because URL matches."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            key = _source_key(raw)
            if not key:
                continue
            current = merged.get(key)
            if current is None:
                current = {**raw}
                current["supports"] = []
                merged[key] = current
                order.append(key)
            else:
                for field in ("title", "type", "ref", "evidence_ref"):
                    if not current.get(field) and raw.get(field):
                        current[field] = raw.get(field)
            source_url = str(raw.get("url") or "").rstrip("/")
            supports: dict[str, dict[str, Any]] = {
                _support_key(source_url, item): item
                for item in current.get("supports") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            }
            for support in raw.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                text = str(support.get("text") or "").strip()
                if not text:
                    continue
                supports[_support_key(source_url, support)] = {
                    **support,
                    "source_url": str(support.get("source_url") or source_url).rstrip("/"),
                    "text": text,
                }
            current["supports"] = list(supports.values())
    return [merged[key] for key in order]


def _source_version_id(source: dict[str, Any]) -> str:
    """Return the durable fetched document version when available."""
    durable = str(source.get("source_version_id") or "").strip()
    if durable:
        return durable
    payload = {
        "url": str(source.get("url") or "").rstrip("/"),
        "type": str(source.get("type") or ""),
        "title": str(source.get("title") or ""),
        "supports": [
            {
                "kind": str(item.get("kind") or "support"),
                "source_url": str(item.get("source_url") or source.get("url") or "").rstrip("/"),
                "text": str(item.get("text") or "").strip(),
            }
            for item in source.get("supports") or []
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ],
    }
    return _sha("srcv_", _canonical(payload), 24)


def _evidence_rows(observation_id: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").rstrip("/")
        fallback_source_version_id = _source_version_id(source)
        for support in source.get("supports") or []:
            if not isinstance(support, dict):
                continue
            text = str(support.get("text") or "").strip()
            if not text:
                continue
            kind = str(support.get("kind") or "support")[:80]
            support_url = str(support.get("source_url") or url).rstrip("/")
            source_version_id = str(
                support.get("source_version_id")
                or source.get("source_version_id")
                or fallback_source_version_id
            ).strip()
            passage_digest = hashlib.sha256(
                (
                    source_version_id
                    + "\n" + kind
                    + "\n" + support_url
                    + "\n" + str(support.get("chunk_id") or "")
                    + "\n" + str(
                        support.get("span_start")
                        if support.get("span_start") is not None
                        else ""
                    )
                    + "\n" + str(
                        support.get("span_end")
                        if support.get("span_end") is not None
                        else ""
                    )
                    + "\n" + text
                ).encode("utf-8")
            ).hexdigest()
            edge_digest = hashlib.sha256(
                (observation_id + "\n" + passage_digest).encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "evidence_id": "evidence_" + edge_digest[:24],
                    "observation_id": observation_id,
                    "source_url": url,
                    "source_version_id": source_version_id,
                    "support_kind": kind,
                    "span_text": text,
                    "span_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "chunk_id": str(support.get("chunk_id") or "") or None,
                    "span_start": support.get("span_start"),
                    "span_end": support.get("span_end"),
                    "relation": "supports",
                }
            )
    return rows


def _observation_id(
    story_id: str,
    run_id: str,
    batch_id: str,
    assertion_id: str,
    text: str,
    sources: list[dict[str, Any]],
) -> str:
    payload = {
        "story_id": story_id,
        "run_id": run_id,
        "batch_id": batch_id,
        "assertion_id": assertion_id,
        "text": text,
        "sources": merge_source_payloads(sources),
    }
    return _sha("observation_", _canonical(payload), 24)


def _assertion_exists(db, story_id: str, assertion_id: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM fact_assertions WHERE story_id=? AND assertion_id=?",
            (story_id, assertion_id),
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM facts WHERE story_id=? AND fact_id=?",
            (story_id, assertion_id),
        ).fetchone()
    )


def _recompute_assertion_digest(db, story_id: str, assertion_id: str) -> str:
    observations = {
        (
            str(row["text"]),
            str(row["status"]),
            str(row["structural_error"] or ""),
        )
        for row in db.execute(
            "SELECT text,status,structural_error FROM fact_observations "
            "WHERE story_id=? AND assertion_id=?",
            (story_id, assertion_id),
        )
    }
    evidence = {
        (
            str(row["source_version_id"]),
            str(row["chunk_id"] or ""),
            row["span_start"],
            row["span_end"],
            str(row["span_sha256"]),
            str(row["relation"]),
        )
        for row in db.execute(
            "SELECT e.source_version_id,e.chunk_id,e.span_start,e.span_end,e.span_sha256,e.relation "
            "FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id "
            "WHERE o.story_id=? AND o.assertion_id=?",
            (story_id, assertion_id),
        )
    }
    digest = "v2:" + hashlib.sha256(
        _canonical(
            {
                "observations": [list(item) for item in sorted(observations)],
                "evidence": [
                    list(item)
                    for item in sorted(
                        evidence,
                        key=lambda value: tuple(
                            "" if part is None else str(part)
                            for part in value
                        ),
                    )
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    db.execute(
        "UPDATE fact_assertions SET revision_digest=?,updated_at=? "
        "WHERE story_id=? AND assertion_id=?",
        (digest, db.execute("SELECT unixepoch('subsec')").fetchone()[0], story_id, assertion_id),
    )
    return digest


def _invalidate_story_outputs_for_fact_revision(
    db,
    story_id: str,
    assertion_id: str,
    now: float,
) -> None:
    row = db.execute(
        "SELECT research_json,visual_context_json,state FROM stories WHERE id=?",
        (story_id,),
    ).fetchone()
    if row is None:
        return

    research = json.loads(row["research_json"] or "{}")
    draft_bundle = (
        research.get("draft_fact_revisions")
        if isinstance(research.get("draft_fact_revisions"), dict)
        else {}
    )
    research_changed = assertion_id in draft_bundle
    if research_changed:
        research["draft_needs_refresh"] = True
        research["draft_stale_reason"] = "fact_revision_changed"

    visual = json.loads(row["visual_context_json"] or "{}")
    visual_bundle = (
        visual.get("fact_revision_bundle")
        if isinstance(visual.get("fact_revision_bundle"), dict)
        else {}
    )
    selected_visual_ids = {
        str(item.get("fact_id") or "")
        for item in (visual.get("selected_facts") or [])
        if isinstance(item, dict) and str(item.get("fact_id") or "")
    }
    visual_changed = assertion_id in visual_bundle or assertion_id in selected_visual_ids
    clear_visual = visual_changed and str(row["state"] or "") not in {"scheduled", "published"}
    if clear_visual:
        visual["stale"] = True
        visual["stale_reason"] = "fact_revision_changed"

    if not research_changed and not clear_visual:
        return

    db.execute(
        "UPDATE stories SET research_json=?,visual_context_json=?,"
        "vibepublish_asset_ref=CASE WHEN ? THEN NULL ELSE vibepublish_asset_ref END,"
        "processed_image_url=CASE WHEN ? THEN NULL ELSE processed_image_url END,"
        "state=CASE WHEN ? THEN 'needs_review' ELSE state END,"
        "error_code=CASE WHEN ? THEN 'visual_stale' ELSE error_code END,"
        "error_message=CASE WHEN ? THEN "
        "'Evidence for a used fact changed; refresh the draft/visual before publication.' "
        "ELSE error_message END,updated_at=? WHERE id=?",
        (
            _canonical(research),
            _canonical(visual),
            int(clear_visual),
            int(clear_visual),
            int(clear_visual),
            int(clear_visual),
            int(clear_visual),
            now,
            story_id,
        ),
    )


def persist_fact_candidates(
    db,
    *,
    story_id: str,
    poi_key: str | None,
    facts: list[dict[str, Any]],
    run_id: str,
    batch_id: str,
    model_name: str,
    prompt_version: str,
    now: float,
) -> list[str]:
    """Append model observations/evidence and refresh only touched compatibility facts."""
    persisted: list[str] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        text = validated_model_fact_text(item.get("text"))
        if text is None:
            continue
        claim_key = normalized_claim_key(item.get("claim_key"))
        existing_fact_id = str(item.get("existing_fact_id") or "").strip()
        if existing_fact_id and _assertion_exists(db, story_id, existing_fact_id):
            assertion_id = existing_fact_id
        else:
            assertion_id = candidate_assertion_id(claim_key, text)

        sources = merge_source_payloads(
            [source for source in (item.get("sources") or []) if isinstance(source, dict)]
        )
        observation_id = _observation_id(story_id, run_id, batch_id, assertion_id, text, sources)
        evidence_rows = _evidence_rows(observation_id, sources)
        status = "accepted" if evidence_rows else "candidate"
        structural_error = None if evidence_rows else "evidence_span_missing"

        current_assertion = db.execute(
            "SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id=?",
            (story_id, assertion_id),
        ).fetchone()
        legacy_fact = db.execute(
            "SELECT * FROM facts WHERE story_id=? AND fact_id=?",
            (story_id, assertion_id),
        ).fetchone()

        if current_assertion is None:
            owner_selected = int(bool(item.get("selected")) and bool(evidence_rows))
            display_text = text
            if legacy_fact is not None:
                owner_selected = int(bool(legacy_fact["selected"]))
                display_text = str(legacy_fact["text"])
            db.execute(
                "INSERT INTO fact_assertions("
                "story_id,assertion_id,semantic_key,display_text,owner_selected,"
                "review_status,eligibility,revision_digest,created_at,updated_at"
                ") VALUES(?,?,?,?,?,'unreviewed','unreviewed','',?,?)",
                (
                    story_id,
                    assertion_id,
                    claim_key,
                    display_text,
                    owner_selected,
                    now,
                    now,
                ),
            )

        db.execute(
            "INSERT OR IGNORE INTO fact_observations("
            "observation_id,story_id,poi_key,run_id,batch_id,assertion_id,model_claim_key,"
            "text,confidence,status,structural_error,model_name,prompt_version,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                story_id,
                poi_key,
                run_id,
                batch_id,
                assertion_id,
                claim_key,
                text,
                max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                status,
                structural_error,
                str(model_name or "")[:120],
                str(prompt_version or "")[:120],
                now,
            ),
        )
        for evidence in evidence_rows:
            db.execute(
                "INSERT OR IGNORE INTO fact_evidence_spans("
                "evidence_id,observation_id,source_url,source_version_id,support_kind,"
                "span_text,span_sha256,chunk_id,span_start,span_end,relation,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence["evidence_id"],
                    evidence["observation_id"],
                    evidence["source_url"],
                    evidence["source_version_id"],
                    evidence["support_kind"],
                    evidence["span_text"],
                    evidence["span_sha256"],
                    evidence["chunk_id"],
                    evidence["span_start"],
                    evidence["span_end"],
                    evidence["relation"],
                    now,
                ),
            )

        if legacy_fact is None:
            db.execute(
                "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    story_id,
                    assertion_id,
                    text,
                    max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                    int(bool(evidence_rows)),
                    int(bool(item.get("selected")) and bool(evidence_rows)),
                    _canonical(sources),
                ),
            )
        else:
            prior_sources = json.loads(legacy_fact["sources_json"] or "[]")
            merged_sources = merge_source_payloads(prior_sources, sources)
            db.execute(
                "UPDATE facts SET confidence=?,evidence_supported=?,sources_json=? "
                "WHERE story_id=? AND fact_id=?",
                (
                    max(float(legacy_fact["confidence"]), max(0.0, min(1.0, float(item.get("confidence") or 0.0)))),
                    int(bool(legacy_fact["evidence_supported"]) or bool(evidence_rows)),
                    _canonical(merged_sources),
                    story_id,
                    assertion_id,
                ),
            )

        previous_digest = ""
        row = db.execute(
            "SELECT revision_digest FROM fact_assertions WHERE story_id=? AND assertion_id=?",
            (story_id, assertion_id),
        ).fetchone()
        if row:
            previous_digest = str(row["revision_digest"] or "")
        new_digest = _recompute_assertion_digest(db, story_id, assertion_id)
        if previous_digest and previous_digest != new_digest:
            db.execute(
                "UPDATE fact_assertions SET review_status='unreviewed',eligibility='unreviewed',updated_at=? "
                "WHERE story_id=? AND assertion_id=? AND review_status<>'quarantined'",
                (now, story_id, assertion_id),
            )
            db.execute(
                "UPDATE fact_arbitration_events SET state='stale',"
                "stale_reason='fact_revision_changed',stale_at=? "
                "WHERE story_id=? AND state='active' AND conflict_id IN ("
                "SELECT conflict_id FROM fact_conflicts "
                "WHERE story_id=? AND (left_fact_id=? OR right_fact_id=?)"
                ")",
                (now, story_id, story_id, assertion_id, assertion_id),
            )
            db.execute(
                "UPDATE fact_conflicts SET final_resolution=NULL,final_fact_id=NULL,"
                "arbitration_reason=NULL,arbitration_confidence=NULL,arbitrated_by=NULL,last_seen_at=? "
                "WHERE story_id=? AND (left_fact_id=? OR right_fact_id=?)",
                (now, story_id, assertion_id, assertion_id),
            )
            _invalidate_story_outputs_for_fact_revision(
                db,
                story_id,
                assertion_id,
                now,
            )
        persisted.append(assertion_id)
    return persisted


def backfill_legacy_fact_ledger(db, now: float) -> int:
    rows = list(
        db.execute(
            "SELECT f.* FROM facts f "
            "WHERE NOT EXISTS(SELECT 1 FROM fact_assertions a "
            "WHERE a.story_id=f.story_id AND a.assertion_id=f.fact_id) "
            "ORDER BY f.story_id,f.rowid"
        )
    )
    count = 0
    for row in rows:
        sources = json.loads(row["sources_json"] or "[]")
        db.execute(
            "INSERT OR IGNORE INTO fact_assertions("
            "story_id,assertion_id,semantic_key,display_text,owner_selected,"
            "review_status,eligibility,revision_digest,created_at,updated_at"
            ") VALUES(?,?,?,?,?,'unreviewed','unreviewed','',?,?)",
            (
                row["story_id"],
                row["fact_id"],
                None,
                row["text"],
                int(bool(row["selected"])),
                now,
                now,
            ),
        )
        observation_id = _observation_id(
            row["story_id"], "legacy-migration", "legacy-facts", row["fact_id"], row["text"], sources
        )
        evidence_rows = _evidence_rows(observation_id, sources)
        db.execute(
            "INSERT OR IGNORE INTO fact_observations("
            "observation_id,story_id,poi_key,run_id,batch_id,assertion_id,model_claim_key,"
            "text,confidence,status,structural_error,model_name,prompt_version,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                row["story_id"],
                None,
                "legacy-migration",
                "legacy-facts",
                row["fact_id"],
                None,
                row["text"],
                row["confidence"],
                "accepted" if evidence_rows else "candidate",
                None if evidence_rows else "legacy_evidence_span_missing",
                "legacy",
                "legacy",
                now,
            ),
        )
        for evidence in evidence_rows:
            db.execute(
                "INSERT OR IGNORE INTO fact_evidence_spans("
                "evidence_id,observation_id,source_url,source_version_id,support_kind,"
                "span_text,span_sha256,chunk_id,span_start,span_end,relation,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence["evidence_id"],
                    evidence["observation_id"],
                    evidence["source_url"],
                    evidence["source_version_id"],
                    evidence["support_kind"],
                    evidence["span_text"],
                    evidence["span_sha256"],
                    evidence["chunk_id"],
                    evidence["span_start"],
                    evidence["span_end"],
                    evidence["relation"],
                    now,
                ),
            )
        _recompute_assertion_digest(db, row["story_id"], row["fact_id"])
        count += 1
    return count


def repair_missing_evidence_edges(db, now: float) -> dict[str, int]:
    """Repair legacy passage-PK collisions without inventing evidence."""
    repaired = 0
    downgraded = 0
    affected: set[tuple[str, str]] = set()
    rows = list(db.execute(
        "SELECT o.observation_id,o.story_id,o.assertion_id,f.sources_json "
        "FROM fact_observations o "
        "LEFT JOIN facts f ON f.story_id=o.story_id AND f.fact_id=o.assertion_id "
        "WHERE o.status='accepted' "
        "AND NOT EXISTS("
        "SELECT 1 FROM fact_evidence_spans e WHERE e.observation_id=o.observation_id"
        ")"
    ))
    for row in rows:
        observation_id = str(row["observation_id"])
        story_id = str(row["story_id"])
        assertion_id = str(row["assertion_id"])
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except (TypeError, ValueError):
            sources = []

        inserted = 0
        for evidence in _evidence_rows(observation_id, sources):
            exact = db.execute(
                "SELECT 1 FROM fact_evidence_spans "
                "WHERE source_url=? AND source_version_id=? AND support_kind=? "
                "AND span_sha256=? "
                "AND COALESCE(chunk_id,'')=COALESCE(?,'') "
                "AND COALESCE(span_start,-1)=COALESCE(?,-1) "
                "AND COALESCE(span_end,-1)=COALESCE(?,-1) LIMIT 1",
                (
                    evidence["source_url"],
                    evidence["source_version_id"],
                    evidence["support_kind"],
                    evidence["span_sha256"],
                    evidence["chunk_id"],
                    evidence["span_start"],
                    evidence["span_end"],
                ),
            ).fetchone()
            if not exact:
                continue
            before = db.total_changes
            db.execute(
                "INSERT OR IGNORE INTO fact_evidence_spans("
                "evidence_id,observation_id,source_url,source_version_id,support_kind,"
                "span_text,span_sha256,chunk_id,span_start,span_end,relation,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence["evidence_id"],
                    evidence["observation_id"],
                    evidence["source_url"],
                    evidence["source_version_id"],
                    evidence["support_kind"],
                    evidence["span_text"],
                    evidence["span_sha256"],
                    evidence["chunk_id"],
                    evidence["span_start"],
                    evidence["span_end"],
                    evidence["relation"],
                    now,
                ),
            )
            if db.total_changes > before:
                inserted += 1

        affected.add((story_id, assertion_id))
        if inserted:
            repaired += inserted
        else:
            db.execute(
                "UPDATE fact_observations SET status='candidate',"
                "structural_error='evidence_edge_missing' WHERE observation_id=?",
                (observation_id,),
            )
            downgraded += 1

    for story_id, assertion_id in affected:
        has_evidence = bool(db.execute(
            "SELECT 1 FROM fact_evidence_spans e "
            "JOIN fact_observations o ON o.observation_id=e.observation_id "
            "WHERE o.story_id=? AND o.assertion_id=? LIMIT 1",
            (story_id, assertion_id),
        ).fetchone())
        db.execute(
            "UPDATE facts SET evidence_supported=? WHERE story_id=? AND fact_id=?",
            (int(has_evidence), story_id, assertion_id),
        )
        if not has_evidence:
            db.execute(
                "UPDATE fact_assertions "
                "SET review_status='withheld',eligibility='withheld',updated_at=? "
                "WHERE story_id=? AND assertion_id=?",
                (now, story_id, assertion_id),
            )
        _recompute_assertion_digest(db, story_id, assertion_id)

    return {
        "repaired_edges": repaired,
        "downgraded_observations": downgraded,
    }


def set_owner_selection(db, story_id: str, selected_ids: list[str], now: float) -> None:
    backfill_legacy_fact_ledger(db, now)
    selected = set(selected_ids)
    for row in db.execute(
        "SELECT assertion_id FROM fact_assertions WHERE story_id=?",
        (story_id,),
    ):
        value = int(str(row["assertion_id"]) in selected)
        db.execute(
            "UPDATE fact_assertions SET owner_selected=?,updated_at=? "
            "WHERE story_id=? AND assertion_id=?",
            (value, now, story_id, row["assertion_id"]),
        )
        db.execute(
            "UPDATE facts SET selected=? WHERE story_id=? AND fact_id=?",
            (value, story_id, row["assertion_id"]),
        )


def refresh_review_status(db, story_id: str, now: float) -> None:
    """Recompute eligibility from exact review revisions and all active blockers.

    A successful review only covers assertion revisions explicitly frozen in its
    revision bundle. Conflict decisions never grant eligibility by themselves:
    they can only remove or add blockers on top of a reviewed, evidence-backed
    assertion. This makes the result independent of conflict row order.
    """
    backfill_legacy_fact_ledger(db, now)
    assertions = {
        str(row["assertion_id"]): dict(row)
        for row in db.execute(
            "SELECT assertion_id,review_status,eligibility,revision_digest "
            "FROM fact_assertions WHERE story_id=?",
            (story_id,),
        )
    }
    if not assertions:
        return

    evidence_supported = {
        str(row["assertion_id"])
        for row in db.execute(
            "SELECT DISTINCT o.assertion_id FROM fact_observations o "
            "JOIN fact_evidence_spans e ON e.observation_id=o.observation_id "
            "WHERE o.story_id=? AND o.status='accepted'",
            (story_id,),
        )
    }

    successful_scans: list[dict[str, Any]] = []
    for row in db.execute(
        "SELECT id,status,coverage_complete,revision_bundle_json,conflict_ids_json,created_at "
        "FROM fact_conflict_scans WHERE story_id=? ORDER BY id DESC",
        (story_id,),
    ):
        if (
            str(row["status"]) not in {"ok", "no_candidates"}
            or int(row["coverage_complete"] or 0) != 1
        ):
            continue
        try:
            raw_bundle = json.loads(row["revision_bundle_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_bundle = {}
        if not isinstance(raw_bundle, dict):
            raw_bundle = {}
        bundle = {
            str(fact_id): str(revision or "")
            for fact_id, revision in raw_bundle.items()
            if str(fact_id)
        }
        try:
            raw_conflicts = json.loads(row["conflict_ids_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_conflicts = []
        conflict_ids = (
            {
                str(value)
                for value in raw_conflicts
                if str(value)
            }
            if isinstance(raw_conflicts, list)
            else set()
        )
        successful_scans.append(
            {
                "id": int(row["id"]),
                "bundle": bundle,
                "conflict_ids": conflict_ids,
                "created_at": float(row["created_at"] or 0.0),
            }
        )

    def latest_scan_for(*fact_ids: str) -> dict[str, Any] | None:
        for scan in successful_scans:
            bundle = scan["bundle"]
            if all(fact_id in assertions for fact_id in fact_ids) and all(
                bundle.get(fact_id)
                == str(assertions[fact_id]["revision_digest"] or "")
                for fact_id in fact_ids
            ):
                return scan
        return None

    blocked: dict[str, set[str]] = {fact_id: set() for fact_id in assertions}
    for conflict in db.execute(
        "SELECT * FROM fact_conflicts WHERE story_id=? ORDER BY conflict_id",
        (story_id,),
    ):
        left = str(conflict["left_fact_id"])
        right = str(conflict["right_fact_id"])
        if left not in assertions or right not in assertions:
            continue
        left_revision = str(assertions[left]["revision_digest"] or "")
        right_revision = str(assertions[right]["revision_digest"] or "")
        reviewed_pair = latest_scan_for(left, right)
        conflict_id = str(conflict["conflict_id"])
        conflict_seen_at = float(conflict["last_seen_at"] or 0.0)

        if (
            reviewed_pair is not None
            and float(reviewed_pair["created_at"]) >= conflict_seen_at
            and conflict_id not in reviewed_pair["conflict_ids"]
        ):
            # A later complete review of these exact revisions explicitly found
            # no such conflict. An older review cannot erase a newly observed one.
            continue

        detection_scoped = (
            str(conflict["left_revision_digest"] or "") == left_revision
            and str(conflict["right_revision_digest"] or "") == right_revision
            and bool(left_revision)
            and bool(right_revision)
        )
        legacy_unscoped = (
            not str(conflict["left_revision_digest"] or "")
            and not str(conflict["right_revision_digest"] or "")
        )
        if reviewed_pair is None and not detection_scoped and not legacy_unscoped:
            # The conflict belongs to older assertion revisions. The changed
            # assertion itself is unreviewed until a new exact review.
            continue

        event = db.execute(
            "SELECT resolution,left_revision_digest,right_revision_digest "
            "FROM fact_arbitration_events "
            "WHERE story_id=? AND conflict_id=? AND state='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (story_id, conflict_id),
        ).fetchone()
        event_valid = bool(
            event
            and str(event["left_revision_digest"] or "") == left_revision
            and str(event["right_revision_digest"] or "") == right_revision
        )
        if not event_valid:
            blocked[left].add("unresolved_or_stale_conflict")
            blocked[right].add("unresolved_or_stale_conflict")
            continue

        resolution = str(event["resolution"] or "")
        if resolution == "prefer_left":
            blocked[right].add("losing_conflict")
        elif resolution == "prefer_right":
            blocked[left].add("losing_conflict")
        elif resolution == "both_valid":
            pass
        else:
            blocked[left].add("unresolved_conflict")
            blocked[right].add("unresolved_conflict")

    pending_review = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_review_attempts'").fetchone():
        for row in db.execute("SELECT a.affected_json FROM live_review_attempts a JOIN live_review_packets p ON p.packet_ref=a.packet_ref WHERE p.story_id=? AND a.state='pending'", (story_id,)):
            pending_review.update(json.loads(row['affected_json']))
    for fact_id, assertion in assertions.items():
        current_status = str(assertion["review_status"] or "")
        if current_status == "quarantined":
            review_status = "quarantined"
            eligibility = "withheld"
        elif fact_id in pending_review:
            review_status = "unreviewed"
            eligibility = "unreviewed"
        elif current_status == "withheld":
            review_status = "withheld"
            eligibility = "withheld"
        elif fact_id not in evidence_supported:
            review_status = "withheld"
            eligibility = "withheld"
        elif blocked[fact_id]:
            review_status = "disputed"
            eligibility = "withheld"
        elif latest_scan_for(fact_id) is None:
            review_status = "unreviewed"
            eligibility = "unreviewed"
        else:
            review_status = "eligible"
            eligibility = "eligible"
        db.execute(
            "UPDATE fact_assertions SET review_status=?,eligibility=?,updated_at=? "
            "WHERE story_id=? AND assertion_id=?",
            (review_status, eligibility, now, story_id, fact_id),
        )

    from .poi_memory import sync_poi_review_from_story
    sync_poi_review_from_story(db, story_id, now)

def selected_eligibility_issues(db, story_id: str) -> list[dict[str, Any]]:
    backfill_legacy_fact_ledger(db, 0.0)
    rows = db.execute(
        "SELECT f.fact_id,f.text,a.owner_selected,a.review_status,a.eligibility "
        "FROM facts f JOIN fact_assertions a "
        "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
        "WHERE f.story_id=? AND a.owner_selected=1 AND a.eligibility<>'eligible' "
        "ORDER BY f.rowid",
        (story_id,),
    )
    return [
        {
            **dict(row),
            "reason": "fact_not_eligible",
        }
        for row in rows
    ]


def eligibility_issues_for_ids(db, story_id: str, fact_ids: list[str]) -> list[dict[str, Any]]:
    backfill_legacy_fact_ledger(db, 0.0)
    ids = list(dict.fromkeys(str(value) for value in fact_ids if str(value)))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = {
        str(row["assertion_id"]): dict(row)
        for row in db.execute(
            f"SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id IN ({placeholders})",
            (story_id, *ids),
        )
    }
    issues: list[dict[str, Any]] = []
    for fact_id in ids:
        row = rows.get(fact_id)
        if row is None:
            issues.append({"fact_id": fact_id, "reason": "assertion_missing"})
            continue
        if row["eligibility"] != "eligible":
            issues.append({
                "fact_id": fact_id,
                "reason": "fact_not_eligible",
                "review_status": row["review_status"],
                "eligibility": row["eligibility"],
            })
    return issues


def persist_fact_relation_events(
    db,
    *,
    story_id: str,
    run_id: str,
    incoming_facts: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    now: float,
) -> list[str]:
    allowed_relations = {
        "equivalent",
        "different",
        "contradicts",
        "refines",
        "temporal_sequence",
        "scope_difference",
        "uncertain",
    }
    existing_ids = {
        str(item.get("existing_fact_id") or "").strip()
        for item in decisions
        if isinstance(item, dict) and str(item.get("existing_fact_id") or "").strip()
    }
    revisions: dict[str, str] = {}
    if existing_ids:
        placeholders = ",".join("?" for _ in existing_ids)
        revisions = {
            str(row["assertion_id"]): str(row["revision_digest"] or "")
            for row in db.execute(
                f"SELECT assertion_id,revision_digest FROM fact_assertions "
                f"WHERE story_id=? AND assertion_id IN ({placeholders})",
                (story_id, *sorted(existing_ids)),
            )
        }

    event_ids: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        try:
            incoming_index = int(decision.get("incoming_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= incoming_index < len(incoming_facts):
            continue
        relation = str(decision.get("relation") or "").strip()
        existing_fact_id = str(decision.get("existing_fact_id") or "").strip()
        if relation not in allowed_relations or existing_fact_id not in revisions:
            continue
        raw = incoming_facts[incoming_index]
        if not isinstance(raw, dict):
            continue
        incoming_text = " ".join(str(raw.get("text") or "").split()).strip()
        if not incoming_text:
            continue
        incoming_text = incoming_text[:1200]
        incoming_text_sha = hashlib.sha256(incoming_text.encode("utf-8")).hexdigest()
        payload = {
            "story_id": story_id,
            "run_id": run_id,
            "incoming_index": incoming_index,
            "incoming_text_sha256": incoming_text_sha,
            "existing_fact_id": existing_fact_id,
            "existing_revision_digest": revisions[existing_fact_id],
            "relation": relation,
            "model_name": str(decision.get("model_name") or "")[:120],
            "prompt_version": str(decision.get("prompt_version") or "")[:120],
            "rationale": str(decision.get("rationale") or "")[:1000],
        }
        event_id = _sha("relation_", _canonical(payload), 24)
        db.execute(
            "INSERT OR IGNORE INTO fact_relation_events("
            "event_id,story_id,run_id,incoming_index,incoming_text,incoming_text_sha256,"
            "existing_fact_id,existing_revision_digest,relation,model_name,prompt_version,"
            "rationale,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                story_id,
                run_id,
                incoming_index,
                incoming_text,
                incoming_text_sha,
                existing_fact_id,
                revisions[existing_fact_id],
                relation,
                payload["model_name"],
                payload["prompt_version"],
                payload["rationale"],
                now,
            ),
        )
        event_ids.append(event_id)
    return event_ids


def fact_revision_bundle(
    db,
    story_id: str,
    fact_ids: list[str] | None = None,
) -> dict[str, str]:
    backfill_legacy_fact_ledger(db, 0.0)
    if fact_ids is None:
        rows = db.execute(
            "SELECT assertion_id,revision_digest FROM fact_assertions "
            "WHERE story_id=? AND owner_selected=1 AND eligibility='eligible' "
            "ORDER BY assertion_id",
            (story_id,),
        )
        return {
            str(row["assertion_id"]): str(row["revision_digest"] or "")
            for row in rows
        }

    ids = list(dict.fromkeys(str(value) for value in fact_ids if str(value)))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = {
        str(row["assertion_id"]): str(row["revision_digest"] or "")
        for row in db.execute(
            f"SELECT assertion_id,revision_digest FROM fact_assertions "
            f"WHERE story_id=? AND assertion_id IN ({placeholders})",
            (story_id, *ids),
        )
    }
    return {
        fact_id: rows[fact_id]
        for fact_id in ids
        if fact_id in rows
    }


def revision_bundle_issues(
    db,
    story_id: str,
    bundle: dict[str, Any] | None,
    *,
    expected_fact_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(bundle, dict):
        bundle = {}
    normalized = {
        str(fact_id): str(revision or "")
        for fact_id, revision in bundle.items()
        if str(fact_id)
    }
    ids = list(dict.fromkeys(
        [
            *(str(value) for value in (expected_fact_ids or []) if str(value)),
            *normalized.keys(),
        ]
    ))
    current = fact_revision_bundle(db, story_id, ids)
    issues: list[dict[str, Any]] = []
    for fact_id in ids:
        expected = normalized.get(fact_id)
        actual = current.get(fact_id)
        if actual is None:
            issues.append({
                "fact_id": fact_id,
                "reason": "assertion_missing",
                "expected_revision": expected,
                "actual_revision": None,
            })
        elif expected is None:
            issues.append({
                "fact_id": fact_id,
                "reason": "revision_not_frozen",
                "expected_revision": None,
                "actual_revision": actual,
            })
        elif expected != actual:
            issues.append({
                "fact_id": fact_id,
                "reason": "revision_changed",
                "expected_revision": expected,
                "actual_revision": actual,
            })
    return issues


def eligible_selected_fact_ids(db, story_id: str) -> list[str]:
    rows = db.execute(
        "SELECT f.fact_id FROM facts f JOIN fact_assertions a "
        "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
        "WHERE f.story_id=? AND a.owner_selected=1 AND a.eligibility='eligible' "
        "AND f.evidence_supported=1 ORDER BY f.rowid",
        (story_id,),
    )
    return [str(row["fact_id"]) for row in rows]


def assertion_state(db, story_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["assertion_id"]): dict(row)
        for row in db.execute(
            "SELECT * FROM fact_assertions WHERE story_id=?",
            (story_id,),
        )
    }
