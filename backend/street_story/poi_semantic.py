"""LLM-first semantic review for POI claim candidate pairs.

Deterministic code may shortlist bounded pairs, enforce access, validate model
output, persist the journal and create review cases. It must not decide whether
claims truly contradict each other.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


_RELATIONS = {
    "contradiction",
    "scope_difference",
    "temporal_sequence",
    "source_disagreement",
    "uncertain",
}
_SUGGESTED = {
    "prefer_left",
    "prefer_right",
    "both_valid_scope",
    "both_valid_temporal",
    "unresolved",
    "needs_more_sources",
    "wrong_poi_link",
}


class PoiSemanticError(ValueError):
    pass


class PoiSemanticAccessError(PermissionError):
    pass


class PoiSemanticConflict(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _candidate_id(left_claim_id: str, right_claim_id: str) -> str:
    pair = "|".join(sorted((left_claim_id, right_claim_id)))
    return "poi_semantic_" + hashlib.sha256(pair.encode("utf-8")).hexdigest()[:24]


def _conflict_id(left_claim_id: str, right_claim_id: str) -> str:
    pair = "|".join(sorted((left_claim_id, right_claim_id)))
    return "poi_conflict_" + hashlib.sha256(pair.encode("utf-8")).hexdigest()[:24]


def _scope_allowed(
    scope: dict[str, Any],
    *,
    actor_sub: str | None,
    workspace_ids: set[str],
) -> bool:
    visibility = scope.get("visibility")
    if visibility == "public":
        return True
    if visibility == "private":
        return bool(actor_sub and scope.get("owner_sub") == actor_sub)
    if visibility == "workspace":
        return bool(
            scope.get("workspace_id")
            and scope.get("workspace_id") in workspace_ids
        )
    return False


def _evidence_allowed(
    row,
    scope: dict[str, Any],
) -> bool:
    visibility = str(row["visibility"])
    if visibility == "public":
        return True
    if visibility == "private":
        return bool(
            scope.get("owner_sub")
            and row["owner_sub"] == scope.get("owner_sub")
        )
    if visibility == "workspace":
        return bool(
            scope.get("workspace_id")
            and row["workspace_id"] == scope.get("workspace_id")
        )
    return False


def _candidate_pairs(
    items: list[dict[str, Any]],
    *,
    focus_claim_id: str,
    max_pairs: int,
) -> list[dict[str, Any]]:
    """Mechanically bound comparisons for the new/focus claim.

    This function deliberately does not inspect kind, claim_key, dates, wording,
    source scores or any other semantic signal. Mira decides whether a pair is
    equivalent, contradictory, scope-different or unrelated.
    """

    focus = next(
        (item for item in items if str(item.get("fact_id") or "") == focus_claim_id),
        None,
    )
    if focus is None:
        return []

    others = sorted(
        (
            item
            for item in items
            if str(item.get("fact_id") or "")
            and str(item.get("fact_id") or "") != focus_claim_id
        ),
        key=lambda item: str(item.get("fact_id") or ""),
    )
    return [
        {"left": focus, "right": other}
        for other in others[:max_pairs]
    ]


def queue_poi_semantic_candidates(
    db,
    *,
    poi_id: str,
    normalized_scope: dict[str, Any],
    items: list[dict[str, Any]],
    focus_claim_id: str,
    now: float,
    max_pairs: int = 24,
) -> list[str]:
    """Persist only bounded candidate pairs; no contradiction decision occurs."""

    candidate_ids: list[str] = []
    for pair in _candidate_pairs(
        items,
        focus_claim_id=focus_claim_id,
        max_pairs=max_pairs,
    ):
        ids = {str(pair["left"]["fact_id"]), str(pair["right"]["fact_id"])}
        left, right = sorted(ids)
        candidate_id = _candidate_id(left, right)
        db.execute(
            "INSERT INTO poi_semantic_candidates("
            "candidate_id,poi_id,left_claim_id,right_claim_id,scope_json,state,"
            "times_seen,first_seen_at,last_seen_at"
            ") VALUES(?,?,?,?,?,'pending_model',1,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "times_seen=poi_semantic_candidates.times_seen+1,"
            "last_seen_at=excluded.last_seen_at,"
            "scope_json=excluded.scope_json "
            "WHERE poi_semantic_candidates.state='pending_model'",
            (
                candidate_id,
                poi_id,
                left,
                right,
                _canonical(normalized_scope),
                now,
                now,
            ),
        )
        candidate_ids.append(candidate_id)
    return sorted(set(candidate_ids))


def _claim_projection(db, claim_id: str, scope: dict[str, Any]) -> dict[str, Any]:
    claim = db.execute(
        "SELECT id,semantic_key,kind,text,status FROM poi_claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    if not claim:
        raise PoiSemanticError("semantic_candidate_claim_missing")

    evidence_rows = list(
        db.execute(
            "SELECT ce.evidence_ref,ce.verification_score,"
            "e.visibility,e.owner_sub,e.workspace_id "
            "FROM poi_claim_evidence ce "
            "JOIN poi_external_events e ON e.event_id=ce.event_id "
            "WHERE ce.claim_id=? ORDER BY ce.created_at,e.event_id",
            (claim_id,),
        )
    )
    allowed = [
        row for row in evidence_rows if _evidence_allowed(row, scope)
    ]
    if not allowed:
        raise PoiSemanticAccessError("semantic_candidate_evidence_forbidden")
    scores = [
        int(row["verification_score"])
        for row in allowed
        if row["verification_score"] is not None
    ]
    return {
        "claim_id": str(claim["id"]),
        "semantic_key": str(claim["semantic_key"]),
        "kind": str(claim["kind"]),
        "text": str(claim["text"]),
        "status": str(claim["status"]),
        "verification_score": max(scores) if scores else None,
        "evidence_refs": [str(row["evidence_ref"]) for row in allowed],
    }


def semantic_candidate_projection(
    store,
    candidate_id: str,
    *,
    actor_sub: str | None,
    workspace_ids: Iterable[str] = (),
) -> dict[str, Any]:
    workspace_set = {str(value) for value in workspace_ids if str(value)}
    with store.connection() as db:
        candidate = db.execute(
            "SELECT * FROM poi_semantic_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise PoiSemanticError("semantic_candidate_not_found")
        scope = json.loads(str(candidate["scope_json"]))
        if not _scope_allowed(
            scope,
            actor_sub=actor_sub,
            workspace_ids=workspace_set,
        ):
            raise PoiSemanticAccessError("semantic_candidate_forbidden")
        left = _claim_projection(db, str(candidate["left_claim_id"]), scope)
        right = _claim_projection(db, str(candidate["right_claim_id"]), scope)
        poi = db.execute(
            "SELECT canonical_name FROM pois WHERE id=?",
            (candidate["poi_id"],),
        ).fetchone()

    return {
        "contract_version": "poi.semantic_candidate.v1",
        "candidate_id": str(candidate["candidate_id"]),
        "poi_id": str(candidate["poi_id"]),
        "poi_name": str(poi["canonical_name"]) if poi else None,
        "left": left,
        "right": right,
        "scope": scope,
        "state": str(candidate["state"]),
    }


def normalize_semantic_analysis(
    value: dict[str, Any],
    *,
    expected_candidate_id: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PoiSemanticError("semantic_analysis_invalid")
    allowed = {
        "contract_version",
        "candidate_id",
        "classification",
        "relation",
        "suggested_resolution",
        "confidence",
        "rationale",
    }
    if set(value) - allowed:
        raise PoiSemanticError("semantic_analysis_unknown_field")
    if value.get("contract_version") != "poi.semantic_review.v1":
        raise PoiSemanticError("semantic_analysis_contract_invalid")
    if str(value.get("candidate_id") or "") != expected_candidate_id:
        raise PoiSemanticError("semantic_analysis_candidate_mismatch")

    classification = str(value.get("classification") or "")
    if classification not in {"conflict", "no_conflict"}:
        raise PoiSemanticError("semantic_analysis_classification_invalid")

    relation = value.get("relation")
    suggested = value.get("suggested_resolution")
    if classification == "conflict":
        relation = str(relation or "")
        suggested = str(suggested or "")
        if relation not in _RELATIONS:
            raise PoiSemanticError("semantic_analysis_relation_invalid")
        if suggested not in _SUGGESTED:
            raise PoiSemanticError("semantic_analysis_resolution_invalid")
    else:
        if relation not in {None, ""} or suggested not in {None, ""}:
            raise PoiSemanticError("semantic_analysis_no_conflict_payload_invalid")
        relation = None
        suggested = None

    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError):
        raise PoiSemanticError("semantic_analysis_confidence_invalid") from None
    if not 0 <= confidence <= 1:
        raise PoiSemanticError("semantic_analysis_confidence_invalid")
    rationale = str(value.get("rationale") or "").strip()
    if not 3 <= len(rationale) <= 1200:
        raise PoiSemanticError("semantic_analysis_rationale_invalid")

    return {
        "contract_version": "poi.semantic_review.v1",
        "candidate_id": expected_candidate_id,
        "classification": classification,
        "relation": relation,
        "suggested_resolution": suggested,
        "confidence": confidence,
        "rationale": rationale,
    }


def apply_poi_semantic_analysis(
    store,
    candidate_id: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_semantic_analysis(
        analysis,
        expected_candidate_id=candidate_id,
    )
    analysis_digest = _digest(normalized)
    now = store.now()

    with store.tx() as db:
        candidate = db.execute(
            "SELECT * FROM poi_semantic_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise PoiSemanticError("semantic_candidate_not_found")

        state = str(candidate["state"])
        if state != "pending_model":
            if (
                str(candidate["analysis_digest"] or "") == analysis_digest
                and str(candidate["analysis_json"] or "")
                == _canonical(normalized)
            ):
                conflict = db.execute(
                    "SELECT conflict_id FROM poi_conflicts "
                    "WHERE poi_id=? AND left_claim_id=? AND right_claim_id=?",
                    (
                        candidate["poi_id"],
                        candidate["left_claim_id"],
                        candidate["right_claim_id"],
                    ),
                ).fetchone()
                return {
                    "candidate_id": candidate_id,
                    "classification": normalized["classification"],
                    "conflict_id": str(conflict["conflict_id"]) if conflict else None,
                    "replayed": True,
                }
            raise PoiSemanticConflict("semantic_analysis_idempotency_conflict")

        if normalized["classification"] == "no_conflict":
            db.execute(
                "UPDATE poi_semantic_candidates SET state='confirmed_no_conflict',"
                "analysis_digest=?,analysis_json=?,last_seen_at=? "
                "WHERE candidate_id=?",
                (
                    analysis_digest,
                    _canonical(normalized),
                    now,
                    candidate_id,
                ),
            )
            return {
                "candidate_id": candidate_id,
                "classification": "no_conflict",
                "conflict_id": None,
                "review_case_ids": [],
                "replayed": False,
            }

        left = str(candidate["left_claim_id"])
        right = str(candidate["right_claim_id"])
        conflict_id = _conflict_id(left, right)
        db.execute(
            "INSERT INTO poi_conflicts("
            "conflict_id,poi_id,left_claim_id,right_claim_id,relation,status,"
            "times_seen,first_seen_at,last_seen_at"
            ") VALUES(?,?,?,?,?,'open',1,?,?) "
            "ON CONFLICT(conflict_id) DO UPDATE SET "
            "relation=excluded.relation,"
            "times_seen=poi_conflicts.times_seen+1,"
            "last_seen_at=excluded.last_seen_at",
            (
                conflict_id,
                str(candidate["poi_id"]),
                left,
                right,
                normalized["relation"],
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE poi_claims SET status='contested',updated_at=? "
            "WHERE id IN (?,?)",
            (now, left, right),
        )
        db.execute(
            "UPDATE poi_semantic_candidates SET state='confirmed_conflict',"
            "analysis_digest=?,analysis_json=?,last_seen_at=? "
            "WHERE candidate_id=?",
            (
                analysis_digest,
                _canonical(normalized),
                now,
                candidate_id,
            ),
        )

    from .poi_reviews import sync_review_cases

    review_case_ids = sync_review_cases(store, [conflict_id])
    return {
        "candidate_id": candidate_id,
        "classification": "conflict",
        "conflict_id": conflict_id,
        "review_case_ids": review_case_ids,
        "replayed": False,
    }
