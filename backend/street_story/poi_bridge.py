from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Iterable

from .db import Store


FACT_CONTRACT = "poi.fact_evidence.v1"
MEDIA_CONTRACT = "poi.media_evidence.v1"
_ALLOWED_VISIBILITY = {"private", "workspace", "public"}
_ALLOWED_MEDIA_RELATIONS = {"depicts", "illustrates", "map_of", "detail_of"}
_PUBLIC_MEDIA_RIGHTS = {
    "licensed",
    "permission_granted",
    "public_domain_verified",
    "statutory_access_verified",
}
_SINGLE_VALUE_KINDS = {"architect", "foundation", "location"}
_REVIEW_RESOLUTIONS = {
    "prefer_left",
    "prefer_right",
    "both_valid_scope",
    "both_valid_temporal",
    "unresolved",
    "needs_more_sources",
    "wrong_poi_link",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _id(prefix: str, *parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()[:24]


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _display(value: str, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _scope(event: dict[str, Any]) -> tuple[str, str | None, str | None]:
    scope = event.get("scope")
    if not isinstance(scope, dict):
        raise PoiBridgeError("invalid_scope")
    visibility = str(scope.get("visibility") or "")
    if visibility not in _ALLOWED_VISIBILITY:
        raise PoiBridgeError("invalid_visibility")
    owner = str(scope.get("owner_sub") or "").strip() or None
    workspace = str(scope.get("workspace_id") or "").strip() or None
    if visibility == "private" and not owner:
        raise PoiBridgeError("private_owner_required")
    if visibility == "workspace" and not workspace:
        raise PoiBridgeError("workspace_required")
    return visibility, owner, workspace


def _assert_actor_scope(
    visibility: str,
    owner_sub: str | None,
    workspace_id: str | None,
    *,
    actor_sub: str | None,
    workspace_ids: Iterable[str],
) -> None:
    if visibility == "public":
        return
    if visibility == "private":
        if not actor_sub or actor_sub != owner_sub:
            raise PoiBridgeError("private_scope_denied")
        return
    allowed = {str(value) for value in workspace_ids}
    if not workspace_id or workspace_id not in allowed:
        raise PoiBridgeError("workspace_scope_denied")


def _scope_where(
    *,
    actor_sub: str | None,
    workspace_ids: Iterable[str],
) -> tuple[str, list[Any]]:
    clauses = ["visibility='public'"]
    params: list[Any] = []
    if actor_sub:
        clauses.append("(visibility='private' AND owner_sub=?)")
        params.append(actor_sub)
    workspaces = [str(value) for value in dict.fromkeys(workspace_ids)]
    if workspaces:
        placeholders = ",".join("?" for _ in workspaces)
        clauses.append(
            f"(visibility='workspace' AND workspace_id IN ({placeholders}))"
        )
        params.extend(workspaces)
    return "(" + " OR ".join(clauses) + ")", params


class PoiBridgeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PoiKnowledgeBridge:
    """Canonical POI intake/review layer. OAuth/HTTP remains outside this class."""

    def __init__(self, store: Store):
        self.store = store

    def ingest(
        self,
        event: dict[str, Any],
        *,
        actor_sub: str | None = None,
        workspace_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise PoiBridgeError("event_must_be_object")
        contract = str(event.get("contract_version") or "")
        if contract not in {FACT_CONTRACT, MEDIA_CONTRACT}:
            raise PoiBridgeError("unsupported_contract")
        if event.get("producer") != "regional_knowledge":
            raise PoiBridgeError("unsupported_producer")

        event_id = str(event.get("event_id") or "").strip()
        idem = str(event.get("idempotency_key") or "").strip()
        if not event_id or not idem:
            raise PoiBridgeError("event_identity_required")
        visibility, owner_sub, workspace_id = _scope(event)
        _assert_actor_scope(
            visibility,
            owner_sub,
            workspace_id,
            actor_sub=actor_sub,
            workspace_ids=workspace_ids,
        )
        payload_raw = _canonical(event)
        request_digest = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
        now = self.store.now()

        with self.store.tx() as db:
            replay = db.execute(
                "SELECT * FROM poi_intake_events "
                "WHERE event_id=? OR idempotency_key=?",
                (event_id, idem),
            ).fetchone()
            if replay is not None:
                if replay["request_digest"] != request_digest:
                    raise PoiBridgeError("idempotency_conflict")
                return self._receipt_for_event(db, replay)

            db.execute(
                """
                INSERT INTO poi_intake_events(
                  event_id,idempotency_key,request_digest,contract_version,producer,
                  visibility,owner_sub,workspace_id,payload_json,state,
                  resolved_poi_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'received',NULL,?,?)
                """,
                (
                    event_id,
                    idem,
                    request_digest,
                    contract,
                    "regional_knowledge",
                    visibility,
                    owner_sub,
                    workspace_id,
                    payload_raw,
                    now,
                    now,
                ),
            )

            resolution = self._resolve_poi(
                db,
                event.get("poi_locator"),
                now=now,
            )
            if resolution["state"] == "identity_review":
                case_id = self._identity_review_case(
                    db,
                    event_id=event_id,
                    event=event,
                    candidate_poi_ids=resolution["candidate_poi_ids"],
                    visibility=visibility,
                    owner_sub=owner_sub,
                    workspace_id=workspace_id,
                    now=now,
                )
                db.execute(
                    "UPDATE poi_intake_events "
                    "SET state='identity_review',updated_at=? WHERE event_id=?",
                    (now, event_id),
                )
                return {
                    "event_id": event_id,
                    "state": "identity_review",
                    "review_case_id": case_id,
                    "candidate_poi_ids": resolution["candidate_poi_ids"],
                }

            poi_id = str(resolution["poi_id"])
            if contract == FACT_CONTRACT:
                detail = self._ingest_fact(
                    db,
                    event=event,
                    poi_id=poi_id,
                    request_digest=request_digest,
                    visibility=visibility,
                    owner_sub=owner_sub,
                    workspace_id=workspace_id,
                    now=now,
                )
            else:
                detail = self._ingest_media(
                    db,
                    event=event,
                    poi_id=poi_id,
                    request_digest=request_digest,
                    visibility=visibility,
                    owner_sub=owner_sub,
                    workspace_id=workspace_id,
                    now=now,
                )

            db.execute(
                "UPDATE poi_intake_events SET state='processed',resolved_poi_id=?,"
                "updated_at=? WHERE event_id=?",
                (poi_id, now, event_id),
            )
            return {
                "event_id": event_id,
                "state": "processed",
                "poi_id": poi_id,
                **detail,
            }

    def _receipt_for_event(self, db, intake) -> dict[str, Any]:
        event_id = str(intake["event_id"])
        state = str(intake["state"])
        result: dict[str, Any] = {
            "event_id": event_id,
            "state": state,
            "poi_id": intake["resolved_poi_id"],
            "replayed": True,
        }
        evidence = db.execute(
            "SELECT claim_id,evidence_id FROM poi_evidence WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if evidence is not None:
            result["claim_id"] = evidence["claim_id"]
            result["evidence_id"] = evidence["evidence_id"]
        media = db.execute(
            "SELECT media_link_id FROM poi_media_links WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if media is not None:
            result["media_link_id"] = media["media_link_id"]
        review = db.execute(
            "SELECT review_case_id FROM poi_review_cases WHERE subject_ref=? "
            "ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if review is not None:
            result["review_case_id"] = review["review_case_id"]
        return result

    def _resolve_poi(
        self,
        db,
        locator: Any,
        *,
        now: float,
    ) -> dict[str, Any]:
        if not isinstance(locator, dict):
            raise PoiBridgeError("poi_locator_required")
        names = [
            _display(value, 300)
            for value in (locator.get("names") or [])
            if _display(value, 300)
        ]
        if not names:
            raise PoiBridgeError("poi_name_required")
        external = locator.get("external_ids") or {}
        if not isinstance(external, dict) or len(external) > 20:
            raise PoiBridgeError("invalid_external_ids")

        external_matches: set[str] = set()
        for key, value in external.items():
            alias_type = "external:" + _normalize_text(str(key))[:80]
            normalized = _normalize_text(str(value))[:500]
            if not normalized:
                continue
            rows = db.execute(
                "SELECT poi_id FROM poi_aliases "
                "WHERE alias_type=? AND normalized_value=?",
                (alias_type, normalized),
            )
            external_matches.update(str(row["poi_id"]) for row in rows)
        if len(external_matches) > 1:
            return {
                "state": "identity_review",
                "candidate_poi_ids": sorted(external_matches),
            }

        poi_id: str | None = next(iter(external_matches), None)
        if poi_id is None:
            name_matches: set[str] = set()
            for name in names:
                rows = db.execute(
                    "SELECT poi_id FROM poi_aliases "
                    "WHERE alias_type='name' AND normalized_value=?",
                    (_normalize_text(name),),
                )
                name_matches.update(str(row["poi_id"]) for row in rows)
            if len(name_matches) > 1:
                return {
                    "state": "identity_review",
                    "candidate_poi_ids": sorted(name_matches),
                }
            poi_id = next(iter(name_matches), None)

        if poi_id is None:
            poi_id = "poi_" + uuid.uuid4().hex[:24]
            db.execute(
                "INSERT INTO pois(id,status,canonical_name,created_at,updated_at) "
                "VALUES(?,'active',?,?,?)",
                (poi_id, names[0], now, now),
            )

        for name in names:
            db.execute(
                "INSERT OR IGNORE INTO poi_aliases("
                "poi_id,alias_type,alias_value,normalized_value,created_at"
                ") VALUES(?,?,?,?,?)",
                (poi_id, "name", name, _normalize_text(name), now),
            )
        for key, value in external.items():
            clean_key = _normalize_text(str(key))[:80]
            raw_value = _display(str(value), 500)
            normalized = _normalize_text(raw_value)
            if not clean_key or not normalized:
                continue
            try:
                db.execute(
                    "INSERT OR IGNORE INTO poi_aliases("
                    "poi_id,alias_type,alias_value,normalized_value,created_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        poi_id,
                        "external:" + clean_key,
                        raw_value,
                        normalized,
                        now,
                    ),
                )
            except Exception as exc:
                # Unique external alias may have been bound by a prior accepted POI.
                owner = db.execute(
                    "SELECT poi_id FROM poi_aliases "
                    "WHERE alias_type=? AND normalized_value=?",
                    ("external:" + clean_key, normalized),
                ).fetchone()
                if owner is not None and str(owner["poi_id"]) != poi_id:
                    raise PoiBridgeError("external_identity_conflict") from exc
                raise

        return {"state": "resolved", "poi_id": poi_id}

    def _identity_review_case(
        self,
        db,
        *,
        event_id: str,
        event: dict[str, Any],
        candidate_poi_ids: list[str],
        visibility: str,
        owner_sub: str | None,
        workspace_id: str | None,
        now: float,
    ) -> str:
        case_id = _id("review_", "identity", event_id)
        locator = event.get("poi_locator") or {}
        suggestion = {
            "candidate_poi_ids": candidate_poi_ids,
            "poi_locator": locator,
        }
        db.execute(
            """
            INSERT OR IGNORE INTO poi_review_cases(
              review_case_id,conflict_id,subject_ref,poi_id,status,relation,
              required_expertise_json,required_reviews,visibility,owner_sub,
              workspace_id,detector_suggestion_json,revision,created_at,updated_at
            ) VALUES(?,NULL,?,NULL,'open','identity_ambiguity',?,1,?,?,?,?,1,?,?)
            """,
            (
                case_id,
                event_id,
                _canonical({
                    "geography": ["kaliningrad_oblast"],
                    "subject": ["poi_identity"],
                    "period": [],
                    "languages": [],
                }),
                visibility,
                owner_sub,
                workspace_id,
                _canonical(suggestion),
                now,
                now,
            ),
        )
        return case_id

    def _ingest_fact(
        self,
        db,
        *,
        event: dict[str, Any],
        poi_id: str,
        request_digest: str,
        visibility: str,
        owner_sub: str | None,
        workspace_id: str | None,
        now: float,
    ) -> dict[str, Any]:
        claim = event.get("claim")
        evidence = event.get("evidence")
        if not isinstance(claim, dict) or not isinstance(evidence, dict):
            raise PoiBridgeError("fact_payload_invalid")
        semantic_key = _display(claim.get("semantic_key"), 300)
        kind = _display(claim.get("kind"), 80)
        text = _display(claim.get("text"), 1000)
        if not semantic_key or not kind or not text:
            raise PoiBridgeError("fact_claim_required")
        normalized = _normalize_text(text)
        claim_id = _id("pclaim_", poi_id, semantic_key, normalized)
        time_scope = _display(claim.get("time_scope"), 100) or None
        db.execute(
            """
            INSERT INTO poi_claims(
              claim_id,poi_id,semantic_key,kind,text,normalized_text,time_scope,
              status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'active',?,?)
            ON CONFLICT(poi_id,semantic_key,normalized_text) DO UPDATE SET
              updated_at=excluded.updated_at
            """,
            (
                claim_id,
                poi_id,
                semantic_key,
                kind,
                text,
                normalized,
                time_scope,
                now,
                now,
            ),
        )
        existing = db.execute(
            "SELECT claim_id FROM poi_claims "
            "WHERE poi_id=? AND semantic_key=? AND normalized_text=?",
            (poi_id, semantic_key, normalized),
        ).fetchone()
        claim_id = str(existing["claim_id"])

        source_family = _display(evidence.get("source_family_id"), 300) or "unknown"
        score = evidence.get("evidence_verification_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                raise PoiBridgeError("verification_score_invalid") from None
            if not 0 <= score <= 100:
                raise PoiBridgeError("verification_score_invalid")

        event_id = str(event["event_id"])
        evidence_id = _id("pevd_", event_id)
        db.execute(
            """
            INSERT INTO poi_evidence(
              evidence_id,event_id,idempotency_key,request_digest,poi_id,claim_id,
              producer,visibility,owner_sub,workspace_id,source_family_id,
              verification_score,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                evidence_id,
                event_id,
                str(event["idempotency_key"]),
                request_digest,
                poi_id,
                claim_id,
                "regional_knowledge",
                visibility,
                owner_sub,
                workspace_id,
                source_family,
                score,
                _canonical(event),
                now,
            ),
        )
        conflicts = self._detect_claim_conflicts(
            db,
            poi_id=poi_id,
            claim_id=claim_id,
            kind=kind,
            semantic_key=semantic_key,
            time_scope=time_scope,
            visibility=visibility,
            owner_sub=owner_sub,
            workspace_id=workspace_id,
            now=now,
        )
        return {
            "claim_id": claim_id,
            "evidence_id": evidence_id,
            "conflict_ids": conflicts,
        }

    def _detect_claim_conflicts(
        self,
        db,
        *,
        poi_id: str,
        claim_id: str,
        kind: str,
        semantic_key: str,
        time_scope: str | None,
        visibility: str,
        owner_sub: str | None,
        workspace_id: str | None,
        now: float,
    ) -> list[str]:
        rows = list(db.execute(
            "SELECT * FROM poi_claims WHERE poi_id=? AND claim_id<>? "
            "AND kind=? AND status NOT IN ('rejected','superseded')",
            (poi_id, claim_id, kind),
        ))
        created: list[str] = []
        for other in rows:
            if str(other["normalized_text"]) == _normalize_text(
                db.execute(
                    "SELECT text FROM poi_claims WHERE claim_id=?",
                    (claim_id,),
                ).fetchone()["text"]
            ):
                continue
            relation: str | None = None
            if str(other["semantic_key"]) == semantic_key:
                relation = "contradiction"
            elif kind in _SINGLE_VALUE_KINDS:
                relation = "source_disagreement"
            elif time_scope and other["time_scope"] and str(other["time_scope"]) != time_scope:
                relation = "temporal_sequence"
            if relation is None:
                continue

            left, right = sorted([claim_id, str(other["claim_id"])])
            conflict_id = _id("pconf_", poi_id, left, right)
            strongest_by_claim = []
            for candidate_claim_id in (left, right):
                row = db.execute(
                    "SELECT MAX(verification_score) AS score "
                    "FROM poi_evidence WHERE claim_id=?",
                    (candidate_claim_id,),
                ).fetchone()
                strongest_by_claim.append(
                    None if row is None else row["score"]
                )
            both_strong = all(
                value is not None and float(value) >= 80
                for value in strongest_by_claim
            )
            required_reviews = 2 if relation == "contradiction" and both_strong else 1
            review_visibility, review_owner, review_workspace = (
                self._conflict_scope(db, left, right)
            )
            db.execute(
                """
                INSERT INTO poi_conflicts(
                  conflict_id,poi_id,left_claim_id,right_claim_id,relation,status,
                  detector_rationale,required_reviews,created_at,updated_at
                ) VALUES(?,?,?,?,?,'open',?,?,?,?)
                ON CONFLICT(conflict_id) DO UPDATE SET
                  relation=excluded.relation,
                  required_reviews=max(poi_conflicts.required_reviews,excluded.required_reviews),
                  updated_at=excluded.updated_at
                """,
                (
                    conflict_id,
                    poi_id,
                    left,
                    right,
                    relation,
                    "Deterministic POI claim collision; no automatic truth vote.",
                    required_reviews,
                    now,
                    now,
                ),
            )
            if relation in {"contradiction", "source_disagreement", "uncertain"}:
                db.execute(
                    "UPDATE poi_claims SET status='contested',updated_at=? "
                    "WHERE claim_id IN (?,?)",
                    (now, left, right),
                )
            self._ensure_conflict_review_case(
                db,
                conflict_id=conflict_id,
                poi_id=poi_id,
                kind=kind,
                relation=relation,
                required_reviews=required_reviews,
                visibility=review_visibility,
                owner_sub=review_owner,
                workspace_id=review_workspace,
                now=now,
            )
            created.append(conflict_id)
        return created

    def _conflict_scope(
        self,
        db,
        left_claim_id: str,
        right_claim_id: str,
    ) -> tuple[str, str | None, str | None]:
        rows = list(db.execute(
            "SELECT visibility,owner_sub,workspace_id FROM poi_evidence "
            "WHERE claim_id IN (?,?)",
            (left_claim_id, right_claim_id),
        ))
        if not rows:
            return "private", None, None

        private = [row for row in rows if row["visibility"] == "private"]
        if private:
            owners = {
                str(row["owner_sub"])
                for row in private
                if row["owner_sub"]
            }
            return (
                "private",
                next(iter(owners)) if len(owners) == 1 else None,
                None,
            )

        workspace = [row for row in rows if row["visibility"] == "workspace"]
        if workspace:
            workspaces = {
                str(row["workspace_id"])
                for row in workspace
                if row["workspace_id"]
            }
            if len(workspaces) == 1:
                return "workspace", None, next(iter(workspaces))
            return "private", None, None

        return "public", None, None

    def _ensure_conflict_review_case(
        self,
        db,
        *,
        conflict_id: str,
        poi_id: str,
        kind: str,
        relation: str,
        required_reviews: int,
        visibility: str,
        owner_sub: str | None,
        workspace_id: str | None,
        now: float,
    ) -> str:
        case_id = _id("review_", conflict_id)
        db.execute(
            """
            INSERT INTO poi_review_cases(
              review_case_id,conflict_id,subject_ref,poi_id,status,relation,
              required_expertise_json,required_reviews,visibility,owner_sub,
              workspace_id,detector_suggestion_json,revision,created_at,updated_at
            ) VALUES(?, ?,NULL,?,'open',?,?,?,?,?,?,NULL,1,?,?)
            ON CONFLICT(review_case_id) DO UPDATE SET
              required_reviews=max(poi_review_cases.required_reviews,excluded.required_reviews),
              updated_at=excluded.updated_at
            """,
            (
                case_id,
                conflict_id,
                poi_id,
                relation,
                _canonical({
                    "geography": ["kaliningrad_oblast"],
                    "subject": [kind],
                    "period": [],
                    "languages": [],
                }),
                required_reviews,
                visibility,
                owner_sub,
                workspace_id,
                now,
                now,
            ),
        )
        return case_id

    def _ingest_media(
        self,
        db,
        *,
        event: dict[str, Any],
        poi_id: str,
        request_digest: str,
        visibility: str,
        owner_sub: str | None,
        workspace_id: str | None,
        now: float,
    ) -> dict[str, Any]:
        media = event.get("media")
        evidence = event.get("evidence")
        if not isinstance(media, dict) or not isinstance(evidence, dict):
            raise PoiBridgeError("media_payload_invalid")
        illustration_ref = str(media.get("illustration_ref") or "")
        relation = str(media.get("relation") or "")
        crop_hash = str(media.get("source_crop_sha256") or "")
        if not illustration_ref.startswith("knowledge://illustrations/"):
            raise PoiBridgeError("illustration_ref_invalid")
        if relation not in _ALLOWED_MEDIA_RELATIONS:
            raise PoiBridgeError("media_relation_invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", crop_hash):
            raise PoiBridgeError("media_hash_invalid")
        media_visibility = str(media.get("visibility") or "")
        if media_visibility != visibility:
            raise PoiBridgeError("media_visibility_mismatch")
        rights_status = str(media.get("rights_status") or "unknown")
        event_id = str(event["event_id"])
        media_link_id = _id("pmedia_", event_id)
        source_family = _display(evidence.get("source_family_id"), 300) or "unknown"
        db.execute(
            """
            INSERT INTO poi_media_links(
              media_link_id,event_id,idempotency_key,request_digest,poi_id,
              illustration_ref,relation,time_scope,kind,caption,page_id,
              source_crop_sha256,rights_status,visibility,owner_sub,workspace_id,
              source_family_id,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                media_link_id,
                event_id,
                str(event["idempotency_key"]),
                request_digest,
                poi_id,
                illustration_ref,
                relation,
                _display(media.get("time_scope"), 100) or None,
                _display(media.get("kind"), 80) or "other",
                _display(media.get("caption"), 2000) or None,
                str(media.get("page_id") or ""),
                crop_hash,
                rights_status,
                visibility,
                owner_sub,
                workspace_id,
                source_family,
                _canonical(event),
                now,
            ),
        )
        return {"media_link_id": media_link_id}

    def media_for_poi(
        self,
        poi_id: str,
        *,
        actor_sub: str | None = None,
        workspace_ids: Iterable[str] = (),
        publication_only: bool = False,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        where, params = _scope_where(
            actor_sub=actor_sub,
            workspace_ids=workspace_ids,
        )
        sql = (
            "SELECT * FROM poi_media_links WHERE poi_id=? AND "
            + where
        )
        values: list[Any] = [poi_id, *params]
        if publication_only:
            placeholders = ",".join("?" for _ in _PUBLIC_MEDIA_RIGHTS)
            sql += f" AND rights_status IN ({placeholders})"
            values.extend(sorted(_PUBLIC_MEDIA_RIGHTS))
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 100)))
        with self.store.connection() as db:
            rows = list(db.execute(sql, values))
        return [
            {
                "media_link_id": row["media_link_id"],
                "poi_id": row["poi_id"],
                "illustration_ref": row["illustration_ref"],
                "relation": row["relation"],
                "time_scope": row["time_scope"],
                "kind": row["kind"],
                "caption": row["caption"],
                "page_id": row["page_id"],
                "source_crop_sha256": row["source_crop_sha256"],
                "rights_status": row["rights_status"],
                "visibility": row["visibility"],
                "publishable": row["rights_status"] in _PUBLIC_MEDIA_RIGHTS,
            }
            for row in rows
        ]

    def review_cases(
        self,
        *,
        actor_sub: str | None = None,
        workspace_ids: Iterable[str] = (),
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where, params = _scope_where(
            actor_sub=actor_sub,
            workspace_ids=workspace_ids,
        )
        sql = "SELECT * FROM poi_review_cases WHERE " + where
        values: list[Any] = [*params]
        if status:
            sql += " AND status=?"
            values.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 100)))
        with self.store.connection() as db:
            rows = list(db.execute(sql, values))
            result = []
            for row in rows:
                item = dict(row)
                item["required_expertise"] = json.loads(
                    item.pop("required_expertise_json") or "{}"
                )
                item["detector_suggestion"] = json.loads(
                    item.pop("detector_suggestion_json") or "null"
                )
                if row["conflict_id"]:
                    claims = list(db.execute(
                        """
                        SELECT c.claim_id,c.text,
                               (SELECT MAX(e.verification_score)
                                FROM poi_evidence e
                                WHERE e.claim_id=c.claim_id) AS verification_score
                        FROM poi_conflicts x
                        JOIN poi_claims c
                          ON c.claim_id IN (x.left_claim_id,x.right_claim_id)
                        WHERE x.conflict_id=?
                        ORDER BY c.claim_id
                        """,
                        (row["conflict_id"],),
                    ))
                    item["claims"] = [dict(value) for value in claims]
                else:
                    item["claims"] = []
                result.append(item)
        return result

    def assign_review(
        self,
        review_case_id: str,
        *,
        expert_sub: str,
        expertise_snapshot: dict[str, Any],
        assigned_by: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if expertise_snapshot.get("verification_state") != "verified":
            raise PoiBridgeError("expert_profile_not_verified")
        now = self.store.now()
        with self.store.tx() as db:
            case = db.execute(
                "SELECT * FROM poi_review_cases WHERE review_case_id=?",
                (review_case_id,),
            ).fetchone()
            if case is None:
                raise PoiBridgeError("review_case_not_found")
            if int(case["revision"]) != int(expected_revision):
                raise PoiBridgeError("stale_review_revision")
            required = json.loads(case["required_expertise_json"] or "{}")
            for field in ("geography", "subject", "period", "languages"):
                needed = set(required.get(field) or [])
                offered = set(expertise_snapshot.get(field) or [])
                if needed and not needed.issubset(offered):
                    raise PoiBridgeError("expertise_mismatch")
            db.execute(
                """
                INSERT INTO poi_review_assignments(
                  review_case_id,expert_sub,assignment_revision,
                  expertise_snapshot_json,status,assigned_by,assigned_at,updated_at
                ) VALUES(?,?,?,?,'assigned',?,?,?)
                ON CONFLICT(review_case_id,expert_sub,assignment_revision)
                DO UPDATE SET updated_at=excluded.updated_at
                """,
                (
                    review_case_id,
                    expert_sub,
                    int(expected_revision),
                    _canonical(expertise_snapshot),
                    assigned_by,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE poi_review_cases SET status='assigned',updated_at=? "
                "WHERE review_case_id=? AND status='open'",
                (now, review_case_id),
            )
        return {
            "review_case_id": review_case_id,
            "expert_sub": expert_sub,
            "assignment_revision": int(expected_revision),
            "status": "assigned",
        }

    def resolve_review(
        self,
        review_case_id: str,
        *,
        expert_sub: str,
        expected_revision: int,
        resolution: str,
        rationale: str,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        if resolution not in _REVIEW_RESOLUTIONS:
            raise PoiBridgeError("review_resolution_invalid")
        rationale = _display(rationale, 2000)
        if not rationale:
            raise PoiBridgeError("review_rationale_required")
        if confidence is not None:
            confidence = max(0.0, min(1.0, float(confidence)))
        now = self.store.now()
        with self.store.tx() as db:
            case = db.execute(
                "SELECT * FROM poi_review_cases WHERE review_case_id=?",
                (review_case_id,),
            ).fetchone()
            if case is None:
                raise PoiBridgeError("review_case_not_found")
            if int(case["revision"]) != int(expected_revision):
                raise PoiBridgeError("stale_review_revision")
            assignment = db.execute(
                "SELECT * FROM poi_review_assignments "
                "WHERE review_case_id=? AND expert_sub=? "
                "AND assignment_revision=? AND status IN ('assigned','accepted')",
                (review_case_id, expert_sub, int(expected_revision)),
            ).fetchone()
            if assignment is None:
                raise PoiBridgeError("review_assignment_required")

            decision_id = _id(
                "decision_",
                review_case_id,
                expert_sub,
                str(expected_revision),
            )
            receipt = {
                "review_case_id": review_case_id,
                "decision_id": decision_id,
                "expected_revision": int(expected_revision),
                "resolution": resolution,
            }
            db.execute(
                """
                INSERT INTO poi_review_decisions(
                  decision_id,review_case_id,expert_sub,expected_case_revision,
                  resolution,rationale,confidence,submitted_at,receipt_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(review_case_id,expert_sub,expected_case_revision)
                DO NOTHING
                """,
                (
                    decision_id,
                    review_case_id,
                    expert_sub,
                    int(expected_revision),
                    resolution,
                    rationale,
                    confidence,
                    now,
                    _canonical(receipt),
                ),
            )
            stored = db.execute(
                "SELECT * FROM poi_review_decisions "
                "WHERE review_case_id=? AND expert_sub=? "
                "AND expected_case_revision=?",
                (review_case_id, expert_sub, int(expected_revision)),
            ).fetchone()
            if stored["resolution"] != resolution or stored["rationale"] != rationale:
                raise PoiBridgeError("review_idempotency_conflict")
            db.execute(
                "UPDATE poi_review_assignments SET status='submitted',updated_at=? "
                "WHERE review_case_id=? AND expert_sub=? AND assignment_revision=?",
                (now, review_case_id, expert_sub, int(expected_revision)),
            )

            decisions = list(db.execute(
                "SELECT resolution FROM poi_review_decisions "
                "WHERE review_case_id=? AND expected_case_revision=?",
                (review_case_id, int(expected_revision)),
            ))
            required = int(case["required_reviews"])
            final_status = "in_review"
            final_resolution: str | None = None
            if len(decisions) >= required:
                choices = {str(item["resolution"]) for item in decisions}
                if len(choices) == 1:
                    only = next(iter(choices))
                    if only == "needs_more_sources":
                        final_status = "deferred"
                    elif only == "unresolved":
                        final_status = "open"
                    else:
                        final_status = "resolved"
                        final_resolution = only
                else:
                    final_status = "open"

                db.execute(
                    "UPDATE poi_review_cases SET status=?,revision=revision+1,"
                    "updated_at=? WHERE review_case_id=?",
                    (final_status, now, review_case_id),
                )
                if case["conflict_id"]:
                    db.execute(
                        "UPDATE poi_conflicts SET status=?,final_resolution=?,"
                        "updated_at=? WHERE conflict_id=?",
                        (
                            "resolved" if final_status == "resolved" else
                            "deferred" if final_status == "deferred" else "open",
                            final_resolution,
                            now,
                            case["conflict_id"],
                        ),
                    )
            else:
                db.execute(
                    "UPDATE poi_review_cases SET status='in_review',updated_at=? "
                    "WHERE review_case_id=?",
                    (now, review_case_id),
                )

            current = db.execute(
                "SELECT status,revision FROM poi_review_cases WHERE review_case_id=?",
                (review_case_id,),
            ).fetchone()
            return {
                **receipt,
                "status": current["status"],
                "case_revision": int(current["revision"]),
                "received_reviews": len(decisions),
                "required_reviews": required,
            }
