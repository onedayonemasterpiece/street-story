"""Materialize POI contradiction journal into expert review cases.

Projects Hub is expected to own expert discovery/routing UX. Street Story owns the
case, assignment snapshot and durable domain decision history.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


class PoiReviewError(ValueError):
    pass


class PoiReviewAccessError(PermissionError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _case_id(conflict_id: str) -> str:
    digest = hashlib.sha256(conflict_id.encode("utf-8")).hexdigest()[:24]
    return "poi_review_" + digest


def _claim_evidence(db, claim_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT ce.evidence_ref,ce.verification_score,"
        "e.visibility,e.owner_sub,e.workspace_id "
        "FROM poi_claim_evidence ce "
        "JOIN poi_external_events e ON e.event_id=ce.event_id "
        "WHERE ce.claim_id=? ORDER BY ce.created_at,e.event_id",
        (claim_id,),
    )
    return [
        {
            "evidence_ref": str(row["evidence_ref"]),
            "verification_score": (
                int(row["verification_score"])
                if row["verification_score"] is not None
                else None
            ),
            "visibility": str(row["visibility"]),
            "owner_sub": row["owner_sub"],
            "workspace_id": row["workspace_id"],
        }
        for row in rows
    ]


def _strongest(items: list[dict[str, Any]]) -> int | None:
    values = [
        item["verification_score"]
        for item in items
        if item["verification_score"] is not None
    ]
    return max(values) if values else None


def _unique_scopes(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [entry for group in groups for entry in group]:
        scope = {
            "visibility": item["visibility"],
            "owner_sub": item.get("owner_sub"),
            "workspace_id": item.get("workspace_id"),
        }
        key = _canonical(scope)
        if key not in seen:
            seen.add(key)
            result.append(scope)
    return result


def _required_reviews(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> int:
    left_score = _strongest(left)
    right_score = _strongest(right)
    if (
        left_score is not None
        and right_score is not None
        and min(left_score, right_score) >= 80
    ):
        return 2
    return 1


def sync_review_cases(
    store,
    conflict_ids: Iterable[str] | None = None,
) -> list[str]:
    requested = tuple(dict.fromkeys(conflict_ids or ()))
    now = store.now()
    output: list[str] = []

    with store.tx() as db:
        if requested:
            placeholders = ",".join("?" for _ in requested)
            conflicts = list(
                db.execute(
                    "SELECT * FROM poi_conflicts "
                    f"WHERE status='open' AND conflict_id IN ({placeholders})",
                    requested,
                )
            )
        else:
            conflicts = list(
                db.execute(
                    "SELECT * FROM poi_conflicts WHERE status='open'"
                )
            )

        for conflict in conflicts:
            conflict_id = str(conflict["conflict_id"])
            left_id = str(conflict["left_claim_id"])
            right_id = str(conflict["right_claim_id"])
            left = _claim_evidence(db, left_id)
            right = _claim_evidence(db, right_id)
            claims = list(
                db.execute(
                    "SELECT id,kind FROM poi_claims WHERE id IN (?,?)",
                    (left_id, right_id),
                )
            )
            subjects = sorted(
                {str(row["kind"]) for row in claims if row["kind"]}
            )
            expertise = {
                "geography": ["kaliningrad_oblast"],
                "period": [],
                "subject": subjects,
                "languages": [],
            }
            scopes = _unique_scopes(left, right)
            required_reviews = _required_reviews(left, right)
            semantic = db.execute(
                "SELECT analysis_json FROM poi_semantic_candidates "
                "WHERE poi_id=? "
                "AND ((left_claim_id=? AND right_claim_id=?) "
                "OR (left_claim_id=? AND right_claim_id=?)) "
                "AND state='confirmed_conflict' "
                "ORDER BY last_seen_at DESC LIMIT 1",
                (
                    str(conflict["poi_id"]),
                    left_id,
                    right_id,
                    right_id,
                    left_id,
                ),
            ).fetchone()
            model_analysis = (
                json.loads(str(semantic["analysis_json"]))
                if semantic and semantic["analysis_json"]
                else None
            )
            suggestion = {
                "source": "mira_semantic_review",
                "relation": str(conflict["relation"]),
                "analysis": model_analysis,
            }
            review_case_id = _case_id(conflict_id)
            existing = db.execute(
                "SELECT * FROM poi_review_cases WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO poi_review_cases("
                    "review_case_id,conflict_id,poi_id,relation,status,"
                    "required_expertise_json,required_reviews,scope_json,"
                    "detector_suggestion_json,case_revision,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        review_case_id,
                        conflict_id,
                        str(conflict["poi_id"]),
                        str(conflict["relation"]),
                        "open",
                        _canonical(expertise),
                        required_reviews,
                        _canonical(scopes),
                        _canonical(suggestion),
                        now,
                        now,
                    ),
                )
            elif str(existing["status"]) not in {"resolved", "superseded"}:
                changed = (
                    str(existing["relation"]) != str(conflict["relation"])
                    or str(existing["required_expertise_json"])
                    != _canonical(expertise)
                    or int(existing["required_reviews"]) != required_reviews
                    or str(existing["scope_json"]) != _canonical(scopes)
                )
                if changed:
                    db.execute(
                        "UPDATE poi_review_cases SET relation=?,"
                        "required_expertise_json=?,required_reviews=?,"
                        "scope_json=?,detector_suggestion_json=?,"
                        "case_revision=case_revision+1,updated_at=? "
                        "WHERE review_case_id=?",
                        (
                            str(conflict["relation"]),
                            _canonical(expertise),
                            required_reviews,
                            _canonical(scopes),
                            _canonical(suggestion),
                            now,
                            review_case_id,
                        ),
                    )
            output.append(review_case_id)

    return output


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


def _projection_scope(scopes: list[dict[str, Any]]) -> dict[str, Any]:
    private = [scope for scope in scopes if scope["visibility"] == "private"]
    if private:
        owners = {scope.get("owner_sub") for scope in private}
        return {
            "visibility": "private",
            "owner_sub": next(iter(owners)) if len(owners) == 1 else None,
            "workspace_id": None,
        }
    workspace = [
        scope for scope in scopes if scope["visibility"] == "workspace"
    ]
    if workspace:
        workspaces = {scope.get("workspace_id") for scope in workspace}
        owners = {scope.get("owner_sub") for scope in workspace}
        return {
            "visibility": "workspace",
            "owner_sub": next(iter(owners)) if len(owners) == 1 else None,
            "workspace_id": (
                next(iter(workspaces)) if len(workspaces) == 1 else None
            ),
        }
    return {
        "visibility": "public",
        "owner_sub": None,
        "workspace_id": None,
    }


def review_case_projection(
    store,
    review_case_id: str,
    *,
    actor_sub: str | None,
    workspace_ids: Iterable[str] = (),
) -> dict[str, Any]:
    workspace_set = {
        str(value) for value in workspace_ids if str(value)
    }
    with store.connection() as db:
        case = db.execute(
            "SELECT * FROM poi_review_cases WHERE review_case_id=?",
            (review_case_id,),
        ).fetchone()
        if not case:
            raise PoiReviewError("review_case_not_found")
        scopes = json.loads(str(case["scope_json"]))
        if any(
            not _scope_allowed(
                scope,
                actor_sub=actor_sub,
                workspace_ids=workspace_set,
            )
            for scope in scopes
        ):
            raise PoiReviewAccessError("review_case_evidence_forbidden")

        conflict = db.execute(
            "SELECT * FROM poi_conflicts WHERE conflict_id=?",
            (case["conflict_id"],),
        ).fetchone()
        if not conflict:
            raise PoiReviewError("review_case_conflict_missing")
        poi = db.execute(
            "SELECT * FROM pois WHERE id=?",
            (case["poi_id"],),
        ).fetchone()

        claims = []
        for claim_id in (
            str(conflict["left_claim_id"]),
            str(conflict["right_claim_id"]),
        ):
            claim = db.execute(
                "SELECT * FROM poi_claims WHERE id=?",
                (claim_id,),
            ).fetchone()
            evidence = _claim_evidence(db, claim_id)
            claims.append(
                {
                    "claim_id": claim_id,
                    "text": str(claim["text"]),
                    "verification_score": _strongest(evidence),
                    "evidence_refs": [
                        item["evidence_ref"] for item in evidence
                    ],
                }
            )

    return {
        "contract_version": "poi.review_case.v1",
        "review_case_id": str(case["review_case_id"]),
        "poi_id": str(case["poi_id"]),
        "poi_name": str(poi["canonical_name"]) if poi else None,
        "conflict_id": str(case["conflict_id"]),
        "status": str(case["status"]),
        "relation": str(case["relation"]),
        "claims": claims,
        "required_expertise": json.loads(
            str(case["required_expertise_json"])
        ),
        "required_reviews": int(case["required_reviews"]),
        "scope": _projection_scope(scopes),
        "detector_suggestion": json.loads(
            str(case["detector_suggestion_json"])
        )
        if case["detector_suggestion_json"]
        else None,
        "case_revision": int(case["case_revision"]),
    }


def list_review_cases_for_actor(
    store,
    *,
    actor_sub: str | None,
    workspace_ids: Iterable[str] = (),
    statuses: Iterable[str] = ("open", "assigned", "in_review", "deferred"),
    limit: int = 100,
) -> list[dict[str, Any]]:
    statuses = tuple(dict.fromkeys(statuses))
    if not statuses:
        return []
    limit = max(1, min(int(limit), 200))
    placeholders = ",".join("?" for _ in statuses)
    with store.connection() as db:
        rows = list(
            db.execute(
                "SELECT review_case_id FROM poi_review_cases "
                f"WHERE status IN ({placeholders}) "
                "ORDER BY updated_at DESC LIMIT ?",
                (*statuses, limit),
            )
        )

    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            output.append(
                review_case_projection(
                    store,
                    str(row["review_case_id"]),
                    actor_sub=actor_sub,
                    workspace_ids=workspace_ids,
                )
            )
        except PoiReviewAccessError:
            continue
    return output


def ingest_poi_evidence_with_reviews(
    store,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper.

    Intake only queues semantic candidates. Review cases appear after Mira/model
    confirms a conflict through apply_poi_semantic_analysis().
    """

    from .poi_external import ingest_poi_evidence

    result = ingest_poi_evidence(store, event)
    result["review_case_ids"] = []
    return result
