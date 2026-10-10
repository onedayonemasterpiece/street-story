"""Street Story owns POI IDs. Model decisions arrive through a scoped service route.

This module stores only one opaque knowledge entity reference and a compact site
description. Source passages, authors, source ACLs and historical assertions stay in RKB.
Name similarity produces candidates, never an identity selection or automatic merge.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SITE_STATES = ("standing", "ruins", "memorial", "lost_site", "historic_site")
EXTERNAL_NS = {"wikidata", "wikipedia_url", "osm_id", "wikipedia", "osm"}
SPACE = re.compile(r"\s+")


class PoiBridgeConflict(ValueError):
    pass


class PoiBridgeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["search", "get", "select", "create"]
    actor_sub: str = Field(min_length=36, max_length=36)
    query: str | None = Field(default=None, max_length=200)
    poi_id: str | None = Field(default=None, max_length=100)
    entity_id: str | None = Field(default=None, max_length=36)
    canonical_name: str | None = Field(default=None, max_length=250)
    site_state: Literal["standing", "ruins", "memorial", "lost_site", "historic_site"] | None = None
    site_context: str | None = Field(default=None, max_length=500)
    model_reason: str | None = Field(default=None, max_length=800)
    source_ref: str | None = Field(default=None, max_length=150)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    external_ids: dict[str, str] = Field(default_factory=dict, max_length=8)
    idempotency_key: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=8, ge=1, le=12)

    @model_validator(mode="after")
    def validate_request(self):
        self.actor_sub = str(UUID(self.actor_sub))
        if self.entity_id is not None:
            self.entity_id = str(UUID(self.entity_id))
        if self.action == "search" and not (self.query or self.external_ids):
            raise ValueError("search query or external IDs required")
        if self.action in ("get", "select") and not self.poi_id:
            raise ValueError("existing POI ID required")
        if self.action in ("select", "create"):
            if not self.entity_id or not self.idempotency_key or not self.source_ref:
                raise ValueError("entity, idempotency key and source reference required")
            if not re.fullmatch(r"knowledge://chunks/[0-9a-f-]{36}", self.source_ref):
                raise ValueError("exact source chunk reference required")
            if not self.model_reason or len(self.model_reason.strip()) < 12:
                raise ValueError("model identity decision required")
        if self.action == "create":
            if not self.canonical_name or not self.site_state or not self.site_context:
                raise ValueError("a physical POI needs name, site state and site context")
        if any(k not in EXTERNAL_NS or not v.strip() or len(v)>500 for k,v in self.external_ids.items()):
            raise ValueError("invalid external IDs")
        return self


def norm(text: str) -> str:
    return SPACE.sub(" ", text).strip().casefold()


def _record(db, poi_id: str, actor: str) -> dict | None:
    row = db.execute("SELECT id,status,canonical_name,latitude,longitude "
                     "FROM pois WHERE id=? AND status!='merged'", (poi_id,)).fetchone()
    if row is None:
        return None
    aliases = [x[0] for x in db.execute(
        "SELECT value FROM poi_aliases WHERE poi_id=? AND namespace='name' ORDER BY normalized_value LIMIT 16",
        (poi_id,))]
    own = [dict(x) for x in db.execute(
        "SELECT rkb_entity_id,site_state,site_context,source_ref FROM rkb_poi_links "
        "WHERE poi_id=? AND actor_sub=? ORDER BY rkb_entity_id LIMIT 20", (poi_id, actor))]
    return {
        "poi_ref": "streetstory://poi/" + row["id"],
        "poi_id": row["id"], "canonical_name": row["canonical_name"],
        "status": row["status"], "aliases": aliases,
        "representative_position": (
            {"latitude": row["latitude"], "longitude":row["longitude"],
             "type":"representative_not_historical_geometry"}
            if row["latitude"] is not None and row["longitude"] is not None else None),
        "site_descriptions": own,
    }


def _matching(db, name: str | None, external_ids: dict[str,str], actor: str, limit: int) -> list[dict]:
    ids: set[str] = set()
    for namespace, value in external_ids.items():
        for row in db.execute(
            "SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=? LIMIT 12",
            (namespace, norm(value))):
            ids.add(str(row[0]))
    if name:
        name_value = norm(name)
        for row in db.execute(
            "SELECT poi_id FROM poi_aliases WHERE namespace='name' AND normalized_value=? LIMIT 12",
            (name_value,)):
            ids.add(str(row[0]))
        for row in db.execute(
            "SELECT id FROM pois WHERE status!='merged' AND "
            "lower(canonical_name)=? LIMIT 12", (name_value,)):
            ids.add(str(row[0]))
    results = [_record(db, pid, actor) for pid in sorted(ids)]
    return [r for r in results if r is not None][:limit]


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,
                                      separators=(",",":")).encode()).hexdigest()


def execute_poi_command(store, raw: dict) -> dict:
    req = PoiBridgeCommand.model_validate(raw)
    if req.action in ("search", "get"):
        with store.connection() as db:
            if req.action == "get":
                found=_record(db,req.poi_id,req.actor_sub)
                if found is None:
                    raise PoiBridgeConflict("poi_not_found")
                return {"state":"found","poi":found}
            return {"state":"candidates","candidates":_matching(db,req.query,req.external_ids,req.actor_sub,req.limit),
                    "identity_selected":False}
    payload=req.model_dump(mode="json",exclude_none=True)
    digest=_digest(payload)
    now=store.now()
    with store.tx() as db:
        previously=db.execute("SELECT action,request_digest,resource_id FROM idempotency WHERE key=?",
                               (req.idempotency_key,)).fetchone()
        if previously:
            if previously["action"]!="rkb_poi_"+req.action or previously["request_digest"]!=digest:
                raise PoiBridgeConflict("idempotency_conflict")
            item=_record(db,previously["resource_id"],req.actor_sub)
            if not item:
                raise PoiBridgeConflict("poi_replay_target_unavailable")
            return {"state":"linked","poi":item,"replayed":True}
        attached=db.execute(
            "SELECT poi_id FROM rkb_poi_links WHERE rkb_entity_id=? AND actor_sub=?",
            (req.entity_id,req.actor_sub)).fetchone()
        if attached:
            if req.action=="select" and str(attached["poi_id"])!=req.poi_id:
                raise PoiBridgeConflict("knowledge_entity_already_bound")
            if req.action=="create":
                raise PoiBridgeConflict("knowledge_entity_already_bound")
            poi_id=str(attached["poi_id"])
        elif req.action=="select":
            existing=_record(db,req.poi_id,req.actor_sub)
            if not existing:
                raise PoiBridgeConflict("selected_poi_missing")
            poi_id=req.poi_id
        else:
            matches=_matching(db,req.canonical_name,req.external_ids,req.actor_sub,12)
            if matches:
                # The model must see existing candidates before creating a new
                # physical identity. Same-name distinct places need a reason.
                raise PoiBridgeConflict("existing_poi_candidates_require_selection")
            seed="rkb-physical-site:"+req.actor_sub+":"+req.entity_id
            poi_id="poi_ss_"+hashlib.sha256(seed.encode()).hexdigest()[:24]
            db.execute(
                "INSERT OR IGNORE INTO pois(id,status,canonical_name,latitude,longitude,created_at,updated_at) "
                "VALUES(?,'candidate',?,NULL,NULL,?,?)",
                (poi_id,req.canonical_name.strip(),now,now))
            # Alias collisions cannot cause cross-place merging.
            for value in dict.fromkeys((req.canonical_name,*req.aliases)):
                normalized=norm(value)
                db.execute(
                    "INSERT OR IGNORE INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) "
                    "VALUES(?,'name',?,?,?)",
                    (poi_id,value.strip()[:500],normalized[:500],now))
            for namespace,value in req.external_ids.items():
                db.execute(
                    "INSERT OR IGNORE INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (poi_id,namespace,value[:500],norm(value)[:500],now))
        db.execute(
            "INSERT OR IGNORE INTO rkb_poi_links "
            "(rkb_entity_id,actor_sub,poi_id,site_state,site_context,source_ref,decision_note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (req.entity_id,req.actor_sub,poi_id,req.site_state or "historic_site",
             req.site_context or "",req.source_ref,req.model_reason,now))
        db.execute(
            "INSERT INTO idempotency(key,action,request_digest,resource_type,resource_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (req.idempotency_key,"rkb_poi_"+req.action,digest,"poi",poi_id,now))
        result=_record(db,poi_id,req.actor_sub)
    return {"state":"linked","poi":result,"replayed":False}
