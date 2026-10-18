"""Deterministic persistence for human expert POI review actions.

The semantic judgment comes from the assigned human expert. This module only
validates authorization snapshots/revisions, persists append-only decisions,
applies the typed human outcome mechanically, and returns durable receipts.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


_RESOLUTIONS = {
    "prefer_left",
    "prefer_right",
    "both_valid_scope",
    "both_valid_temporal",
    "unresolved",
    "needs_more_sources",
    "wrong_poi_link",
}


class PoiReviewActionError(ValueError):
    pass


class PoiReviewActionAccessError(PermissionError):
    pass


class PoiReviewActionConflict(RuntimeError):
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


def _text(value: Any, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise PoiReviewActionError(f"{field}_invalid")
    return text


def _revision(value: Any) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError):
        raise PoiReviewActionError("expected_revision_invalid") from None
    if revision < 1:
        raise PoiReviewActionError("expected_revision_invalid")
    return revision


def _receipt_id(command_id: str) -> str:
    return "poi_review_receipt_" + hashlib.sha256(
        command_id.encode("utf-8")
    ).hexdigest()[:24]


def _command_replay(
    db,
    *,
    command_id: str,
    review_case_id: str,
    actor_sub: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM poi_review_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    if not row:
        return None
    digest = _digest(payload)
    if (
        str(row["review_case_id"]) != review_case_id
        or str(row["actor_sub"]) != actor_sub
        or str(row["kind"]) != kind
        or str(row["payload_digest"]) != digest
    ):
        raise PoiReviewActionConflict("review_command_idempotency_conflict")
    return json.loads(str(row["receipt_json"]))


def _store_command(
    db,
    *,
    command_id: str,
    review_case_id: str,
    actor_sub: str,
    kind: str,
    payload: dict[str, Any],
    receipt: dict[str, Any],
    now: float,
) -> None:
    db.execute(
        "INSERT INTO poi_review_commands("
        "command_id,review_case_id,actor_sub,kind,payload_digest,"
        "receipt_json,created_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            command_id,
            review_case_id,
            actor_sub,
            kind,
            _digest(payload),
            _canonical(receipt),
            now,
        ),
    )


def _case(db, review_case_id: str):
    row = db.execute(
        "SELECT * FROM poi_review_cases WHERE review_case_id=?",
        (review_case_id,),
    ).fetchone()
    if not row:
        raise PoiReviewActionError("review_case_not_found")
    return row


def _validated_expertise_snapshot(
    snapshot: dict[str, Any],
    *,
    actor_sub: str,
    required_json: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise PoiReviewActionAccessError("expertise_snapshot_required")
    allowed = {
        "subject",
        "verification_state",
        "geography",
        "periods",
        "subjects",
        "languages",
        "institutional_roles",
    }
    if set(snapshot) - allowed:
        raise PoiReviewActionAccessError("expertise_snapshot_invalid")
    if snapshot.get("verification_state") != "verified":
        raise PoiReviewActionAccessError("verified_expertise_required")
    if str(snapshot.get("subject") or "") != actor_sub:
        raise PoiReviewActionAccessError("expert_subject_mismatch")

    required = json.loads(required_json or "{}")
    checks = (
        ("geography", "geography"),
        ("period", "periods"),
        ("subject", "subjects"),
        ("languages", "languages"),
    )
    for required_key, snapshot_key in checks:
        needed = {
            str(value)
            for value in (required.get(required_key) or [])
            if str(value)
        }
        actual = {
            str(value)
            for value in (snapshot.get(snapshot_key) or [])
            if str(value)
        }
        if not needed.issubset(actual):
            raise PoiReviewActionAccessError("expertise_requirement_not_met")
    return {
        "subject": actor_sub,
        "verification_state": "verified",
        "geography": sorted(
            {str(v) for v in snapshot.get("geography") or [] if str(v)}
        ),
        "periods": sorted(
            {str(v) for v in snapshot.get("periods") or [] if str(v)}
        ),
        "subjects": sorted(
            {str(v) for v in snapshot.get("subjects") or [] if str(v)}
        ),
        "languages": sorted(
            {str(v) for v in snapshot.get("languages") or [] if str(v)}
        ),
        "institutional_roles": sorted(
            {
                str(v)
                for v in snapshot.get("institutional_roles") or []
                if str(v)
            }
        ),
    }


def accept_review_case(
    store,
    review_case_id: str,
    *,
    actor_sub: str,
    expertise_snapshot: dict[str, Any],
    expected_revision: int,
    command_id: str,
) -> dict[str, Any]:
    review_case_id = _text(review_case_id, "review_case_id", 300)
    actor_sub = _text(actor_sub, "actor_sub", 300)
    command_id = _text(command_id, "command_id", 300)
    expected_revision = _revision(expected_revision)
    payload = {
        "expected_revision": expected_revision,
        "expertise_snapshot": expertise_snapshot,
    }
    now = store.now()

    with store.tx() as db:
        replay = _command_replay(
            db,
            command_id=command_id,
            review_case_id=review_case_id,
            actor_sub=actor_sub,
            kind="accept",
            payload=payload,
        )
        if replay is not None:
            return replay

        case = _case(db, review_case_id)
        if str(case["status"]) in {"resolved", "superseded"}:
            raise PoiReviewActionConflict("review_case_closed")
        if int(case["case_revision"]) != expected_revision:
            raise PoiReviewActionConflict("stale_review_case_revision")
        snapshot = _validated_expertise_snapshot(
            expertise_snapshot,
            actor_sub=actor_sub,
            required_json=str(case["required_expertise_json"]),
        )
        assignment_revision = int(
            db.execute(
                "SELECT COALESCE(MAX(assignment_revision),0)+1 "
                "FROM poi_review_assignments WHERE review_case_id=?",
                (review_case_id,),
            ).fetchone()[0]
        )
        db.execute(
            "INSERT INTO poi_review_assignments("
            "review_case_id,expert_sub,assignment_revision,"
            "expertise_snapshot_json,state,created_at,updated_at"
            ") VALUES(?,?,?,?,'accepted',?,?)",
            (
                review_case_id,
                actor_sub,
                assignment_revision,
                _canonical(snapshot),
                now,
                now,
            ),
        )
        next_revision = expected_revision + 1
        db.execute(
            "UPDATE poi_review_cases SET status='in_review',"
            "case_revision=?,updated_at=? WHERE review_case_id=?",
            (next_revision, now, review_case_id),
        )
        receipt = {
            "receipt_id": _receipt_id(command_id),
            "kind": "accept",
            "review_case_id": review_case_id,
            "actor_sub": actor_sub,
            "assignment_revision": assignment_revision,
            "case_revision": next_revision,
            "status": "in_review",
        }
        _store_command(
            db,
            command_id=command_id,
            review_case_id=review_case_id,
            actor_sub=actor_sub,
            kind="accept",
            payload=payload,
            receipt=receipt,
            now=now,
        )
        return receipt


def _latest_assignment(db, review_case_id: str, actor_sub: str):
    row = db.execute(
        "SELECT * FROM poi_review_assignments "
        "WHERE review_case_id=? AND expert_sub=? "
        "AND state IN ('assigned','accepted') "
        "ORDER BY assignment_revision DESC LIMIT 1",
        (review_case_id, actor_sub),
    ).fetchone()
    if not row:
        raise PoiReviewActionAccessError("active_review_assignment_required")
    return row


def _apply_human_consensus(
    db,
    *,
    case,
    resolution: str,
    now: float,
) -> tuple[str, bool]:
    conflict = db.execute(
        "SELECT * FROM poi_conflicts WHERE conflict_id=?",
        (case["conflict_id"],),
    ).fetchone()
    if not conflict:
        raise PoiReviewActionError("review_conflict_missing")
    left = str(conflict["left_claim_id"])
    right = str(conflict["right_claim_id"])

    if resolution == "prefer_left":
        db.execute(
            "UPDATE poi_conflicts SET status='resolved',last_seen_at=? "
            "WHERE conflict_id=?",
            (now, case["conflict_id"]),
        )
        db.execute(
            "UPDATE poi_claims SET status='accepted',updated_at=? WHERE id=?",
            (now, left),
        )
        db.execute(
            "UPDATE poi_claims SET status='rejected',updated_at=? WHERE id=?",
            (now, right),
        )
        return "resolved", True

    if resolution == "prefer_right":
        db.execute(
            "UPDATE poi_conflicts SET status='resolved',last_seen_at=? "
            "WHERE conflict_id=?",
            (now, case["conflict_id"]),
        )
        db.execute(
            "UPDATE poi_claims SET status='rejected',updated_at=? WHERE id=?",
            (now, left),
        )
        db.execute(
            "UPDATE poi_claims SET status='accepted',updated_at=? WHERE id=?",
            (now, right),
        )
        return "resolved", True

    if resolution in {"both_valid_scope", "both_valid_temporal"}:
        relation = (
            "scope_difference"
            if resolution == "both_valid_scope"
            else "temporal_sequence"
        )
        db.execute(
            "UPDATE poi_conflicts SET status='resolved',relation=?,"
            "last_seen_at=? WHERE conflict_id=?",
            (relation, now, case["conflict_id"]),
        )
        db.execute(
            "UPDATE poi_claims SET status='accepted',updated_at=? "
            "WHERE id IN (?,?)",
            (now, left, right),
        )
        return "resolved", True

    if resolution == "wrong_poi_link":
        db.execute(
            "UPDATE poi_conflicts SET status='superseded',last_seen_at=? "
            "WHERE conflict_id=?",
            (now, case["conflict_id"]),
        )
        db.execute(
            "UPDATE poi_claims SET status='candidate',updated_at=? "
            "WHERE id IN (?,?)",
            (now, left, right),
        )
        return "resolved", True

    if resolution in {"unresolved", "needs_more_sources"}:
        return "deferred", False

    raise PoiReviewActionError("resolution_invalid")


def _submit_decision(
    store,
    review_case_id: str,
    *,
    actor_sub: str,
    expected_revision: int,
    resolution: str,
    rationale: str,
    confidence: float | None,
    command_id: str,
    command_kind: str,
) -> dict[str, Any]:
    review_case_id = _text(review_case_id, "review_case_id", 300)
    actor_sub = _text(actor_sub, "actor_sub", 300)
    command_id = _text(command_id, "command_id", 300)
    expected_revision = _revision(expected_revision)
    if resolution not in _RESOLUTIONS:
        raise PoiReviewActionError("resolution_invalid")
    rationale = _text(rationale, "rationale", 4000)
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise PoiReviewActionError("confidence_invalid") from None
        if not 0 <= confidence <= 1:
            raise PoiReviewActionError("confidence_invalid")

    payload = {
        "expected_revision": expected_revision,
        "resolution": resolution,
        "rationale": rationale,
        "confidence": confidence,
    }
    now = store.now()

    with store.tx() as db:
        replay = _command_replay(
            db,
            command_id=command_id,
            review_case_id=review_case_id,
            actor_sub=actor_sub,
            kind=command_kind,
            payload=payload,
        )
        if replay is not None:
            return replay

        case = _case(db, review_case_id)
        if str(case["status"]) in {"resolved", "superseded"}:
            raise PoiReviewActionConflict("review_case_closed")
        if int(case["case_revision"]) != expected_revision:
            raise PoiReviewActionConflict("stale_review_case_revision")
        assignment = _latest_assignment(db, review_case_id, actor_sub)
        assignment_revision = int(assignment["assignment_revision"])

        decision_id = "poi_review_decision_" + hashlib.sha256(
            command_id.encode("utf-8")
        ).hexdigest()[:24]
        db.execute(
            "INSERT INTO poi_review_decisions("
            "decision_id,review_case_id,expert_sub,assignment_revision,"
            "case_revision_observed,resolution,rationale,confidence,"
            "command_digest,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                review_case_id,
                actor_sub,
                assignment_revision,
                expected_revision,
                resolution,
                rationale,
                confidence,
                _digest(payload),
                now,
            ),
        )
        db.execute(
            "UPDATE poi_review_assignments SET state='submitted',updated_at=? "
            "WHERE review_case_id=? AND expert_sub=? "
            "AND assignment_revision=?",
            (now, review_case_id, actor_sub, assignment_revision),
        )

        decisions = list(
            db.execute(
                "SELECT expert_sub,resolution FROM poi_review_decisions "
                "WHERE review_case_id=? ORDER BY created_at,decision_id",
                (review_case_id,),
            )
        )
        distinct_experts = {str(row["expert_sub"]) for row in decisions}
        required_reviews = int(case["required_reviews"])
        resolution_values = {str(row["resolution"]) for row in decisions}

        applied = False
        consensus = False
        next_status = "in_review"
        if resolution == "needs_more_sources":
            next_status = "deferred"
        elif len(distinct_experts) >= required_reviews:
            if len(resolution_values) == 1:
                consensus = True
                next_status, applied = _apply_human_consensus(
                    db,
                    case=case,
                    resolution=next(iter(resolution_values)),
                    now=now,
                )
            else:
                next_status = "in_review"

        next_revision = expected_revision + 1
        db.execute(
            "UPDATE poi_review_cases SET status=?,case_revision=?,updated_at=? "
            "WHERE review_case_id=?",
            (next_status, next_revision, now, review_case_id),
        )
        receipt = {
            "receipt_id": _receipt_id(command_id),
            "kind": command_kind,
            "review_case_id": review_case_id,
            "actor_sub": actor_sub,
            "assignment_revision": assignment_revision,
            "decision_id": decision_id,
            "resolution": resolution,
            "consensus": consensus,
            "resolution_applied": applied,
            "case_revision": next_revision,
            "status": next_status,
            "submitted_reviews": len(distinct_experts),
            "required_reviews": required_reviews,
        }
        _store_command(
            db,
            command_id=command_id,
            review_case_id=review_case_id,
            actor_sub=actor_sub,
            kind=command_kind,
            payload=payload,
            receipt=receipt,
            now=now,
        )
        return receipt


def resolve_review_case(
    store,
    review_case_id: str,
    *,
    actor_sub: str,
    expected_revision: int,
    resolution: str,
    rationale: str,
    confidence: float | None,
    command_id: str,
) -> dict[str, Any]:
    return _submit_decision(
        store,
        review_case_id,
        actor_sub=actor_sub,
        expected_revision=expected_revision,
        resolution=resolution,
        rationale=rationale,
        confidence=confidence,
        command_id=command_id,
        command_kind="resolve",
    )


def request_review_research(
    store,
    review_case_id: str,
    *,
    actor_sub: str,
    expected_revision: int,
    rationale: str,
    command_id: str,
) -> dict[str, Any]:
    return _submit_decision(
        store,
        review_case_id,
        actor_sub=actor_sub,
        expected_revision=expected_revision,
        resolution="needs_more_sources",
        rationale=rationale,
        confidence=None,
        command_id=command_id,
        command_kind="request_research",
    )
