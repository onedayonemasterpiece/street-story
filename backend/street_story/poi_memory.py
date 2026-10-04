from __future__ import annotations

import hashlib
import json
from typing import Any

from .fact_ledger import merge_source_payloads
from .model_facts import normalized_claim_key


def poi_key(identity: dict[str, Any]) -> str | None:
    value = str(identity.get("candidate_id") or "").strip()
    return value or None


def _identity_alias_values(identity: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in (
        identity.get("candidate_id"),
        identity.get("candidate_name"),
        identity.get("wikidata"),
        identity.get("wikipedia_url"),
        identity.get("osm_id"),
    ):
        value = str(raw or "").strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    candidate_id = str(identity.get("candidate_id") or "")
    for candidate in identity.get("candidates") or []:
        if not isinstance(candidate, dict) or str(candidate.get("candidate_id") or "") != candidate_id:
            continue
        for raw in (
            candidate.get("candidate_id"),
            candidate.get("name"),
            candidate.get("wikidata"),
            candidate.get("wikipedia_url"),
            candidate.get("osm_id"),
        ):
            value = str(raw or "").strip()
            if value and value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
    return values




def _normalized_alias(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def ensure_poi_identity(
    db,
    identity: dict[str, Any],
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    now: float | None = None,
) -> str | None:
    """Bind Street Story's stable identity to the canonical POI registry.

    This is mechanical identity plumbing only: it does not infer semantic
    equivalence beyond an exact existing alias/name match.
    """
    key = poi_key(identity)
    name = str(identity.get("candidate_name") or "").strip()
    if not key or not name:
        return None
    ts = float(now or 0.0)

    aliases: list[tuple[str, str]] = [("street_story_candidate", key), ("name", name)]
    candidate_url = str(identity.get("candidate_url") or "").strip()
    if candidate_url.startswith("https://"):
        aliases.append(("url", candidate_url))
    chosen = next(
        (
            item for item in (identity.get("candidates") or [])
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == key
        ),
        None,
    )
    if isinstance(chosen, dict):
        for namespace, raw in (
            ("wikidata", chosen.get("wikidata")),
            ("wikipedia_url", chosen.get("wikipedia_url") or chosen.get("url") if key.startswith("wiki:") else None),
            ("osm_id", chosen.get("osm_id") or key if key.startswith("osm:") else None),
        ):
            value = str(raw or "").strip()
            if value:
                aliases.append((namespace, value))

    def alias_owner(namespace: str, value: str) -> str | None:
        row = db.execute(
            "SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=?",
            (namespace, _normalized_alias(value)),
        ).fetchone()
        return str(row["poi_id"]) if row else None

    poi_id = alias_owner("street_story_candidate", key)
    if poi_id is None:
        name_owner = alias_owner("name", name)
        if name_owner:
            poi_id = name_owner
        else:
            poi_id = "poi_ss_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            db.execute(
                "INSERT OR IGNORE INTO pois(id,status,canonical_name,latitude,longitude,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (poi_id, "candidate", name[:300], latitude, longitude, ts, ts),
            )

    db.execute(
        "UPDATE pois SET canonical_name=CASE WHEN status='candidate' THEN ? ELSE canonical_name END,"
        "latitude=COALESCE(latitude,?),longitude=COALESCE(longitude,?),updated_at=? WHERE id=?",
        (name[:300], latitude, longitude, ts, poi_id),
    )
    for namespace, value in aliases:
        normalized = _normalized_alias(value)
        owner = alias_owner(namespace, value)
        if owner and owner != poi_id:
            continue
        db.execute(
            "INSERT OR IGNORE INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) VALUES(?,?,?,?,?)",
            (poi_id, namespace, value[:500], normalized[:500], ts),
        )
    return poi_id


def _research_observation_id(
    poi_key_value: str,
    assertion_id: str,
    text: str,
    sources: list[dict[str, Any]],
    research_run_id: str | None,
    query: str,
) -> str:
    payload = json.dumps(
        {
            "poi_key": poi_key_value,
            "assertion_id": assertion_id,
            "text": text,
            "sources": merge_source_payloads(sources),
            "research_run_id": str(research_run_id or ""),
            "query": str(query or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "poiobs_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def backfill_legacy_research_memory(db, now: float) -> int:
    """One-way compatibility backfill from the lossy claim-key table.

    Legacy rows remain readable for rollback/forensics, but all current reads and
    writes use assertion identity so same-key/different-value claims coexist.
    """
    rows = list(db.execute(
        "SELECT poi_key,claim_key,fact_id,text,confidence,sources_json,created_at,updated_at "
        "FROM poi_research_facts ORDER BY created_at,poi_key,claim_key"
    ))
    inserted = 0
    for row in rows:
        assertion_id = str(row["fact_id"] or "").strip()
        text = str(row["text"] or "").strip()
        if not assertion_id or not text:
            continue
        sources = [
            source
            for source in json.loads(row["sources_json"] or "[]")
            if isinstance(source, dict)
        ]
        db.execute(
            "INSERT OR IGNORE INTO poi_research_assertions("
            "poi_key,assertion_id,semantic_key,text,confidence,sources_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                str(row["poi_key"]),
                assertion_id,
                normalized_claim_key(row["claim_key"]),
                text,
                float(row["confidence"]),
                json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
                float(row["created_at"]),
                float(row["updated_at"]),
            ),
        )
        observation_id = _research_observation_id(
            str(row["poi_key"]),
            assertion_id,
            text,
            sources,
            "legacy-migration",
            "legacy-poi-research-facts",
        )
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO poi_research_observations("
            "observation_id,poi_key,assertion_id,semantic_key,text,confidence,sources_json,"
            "research_run_id,query,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                str(row["poi_key"]),
                assertion_id,
                normalized_claim_key(row["claim_key"]),
                text,
                float(row["confidence"]),
                json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
                "legacy-migration",
                "legacy-poi-research-facts",
                float(row["created_at"] or now),
            ),
        )
        if db.total_changes > before:
            inserted += 1
    return inserted


def backfill_poi_assertion_review_state(db, now: float) -> int:
    """Project the latest durable reviewed story decision into POI memory.

    Unreviewed story states never downgrade an already reviewed POI assertion.
    Immutable POI observations remain untouched.
    """
    rows = list(db.execute(
        "SELECT poi_key,assertion_id,reviewed_at FROM poi_research_assertions "
        "ORDER BY poi_key,assertion_id"
    ))
    updated = 0
    for row in rows:
        reviewed = db.execute(
            "SELECT a.story_id,a.review_status,a.eligibility,a.updated_at "
            "FROM fact_assertions a JOIN stories s ON s.id=a.story_id "
            "WHERE a.assertion_id=? "
            "AND json_extract(s.research_json,'$.visual_identity.candidate_id')=? "
            "AND a.review_status<>'unreviewed' "
            "ORDER BY a.updated_at DESC LIMIT 1",
            (str(row["assertion_id"]), str(row["poi_key"])),
        ).fetchone()
        if not reviewed:
            continue
        reviewed_at = float(reviewed["updated_at"] or now)
        current_reviewed_at = row["reviewed_at"]
        if current_reviewed_at is not None and float(current_reviewed_at) >= reviewed_at:
            continue
        db.execute(
            "UPDATE poi_research_assertions SET review_status=?,eligibility=?,"
            "review_story_id=?,reviewed_at=?,updated_at=MAX(updated_at,?) "
            "WHERE poi_key=? AND assertion_id=?",
            (
                str(reviewed["review_status"]),
                str(reviewed["eligibility"]),
                str(reviewed["story_id"]),
                reviewed_at,
                reviewed_at,
                str(row["poi_key"]),
                str(row["assertion_id"]),
            ),
        )
        updated += 1
    return updated


def sync_poi_review_from_story(db, story_id: str, now: float) -> int:
    """Propagate only explicit reviewed story decisions to canonical POI memory."""
    story = db.execute(
        "SELECT research_json FROM stories WHERE id=?",
        (story_id,),
    ).fetchone()
    if not story:
        return 0
    try:
        research = json.loads(story["research_json"] or "{}")
    except (TypeError, ValueError):
        return 0
    identity = research.get("visual_identity") if isinstance(research, dict) else {}
    key = poi_key(identity if isinstance(identity, dict) else {})
    if not key:
        return 0

    updated = 0
    rows = list(db.execute(
        "SELECT assertion_id,review_status,eligibility,updated_at "
        "FROM fact_assertions WHERE story_id=? AND review_status<>'unreviewed'",
        (story_id,),
    ))
    for row in rows:
        reviewed_at = float(row["updated_at"] or now)
        current = db.execute(
            "SELECT reviewed_at FROM poi_research_assertions "
            "WHERE poi_key=? AND assertion_id=?",
            (key, str(row["assertion_id"])),
        ).fetchone()
        if not current:
            continue
        if current["reviewed_at"] is not None and float(current["reviewed_at"]) > reviewed_at:
            continue
        db.execute(
            "UPDATE poi_research_assertions SET review_status=?,eligibility=?,"
            "review_story_id=?,reviewed_at=?,updated_at=MAX(updated_at,?) "
            "WHERE poi_key=? AND assertion_id=?",
            (
                str(row["review_status"]),
                str(row["eligibility"]),
                story_id,
                reviewed_at,
                reviewed_at,
                key,
                str(row["assertion_id"]),
            ),
        )
        updated += 1
    return updated


def hydrate_story_facts(db, identity: dict[str, Any], story_id: str, limit: int = 60) -> int:
    """Hydrate a new topic from durable POI knowledge before another web search."""
    if db.execute("SELECT 1 FROM facts WHERE story_id=? LIMIT 1", (story_id,)).fetchone():
        return 0

    candidates: list[dict[str, Any]] = []
    candidates.extend(
        _research_memory_facts(
            db,
            identity,
            limit,
            include_unreviewed=False,
        )
    )
    remaining = max(0, limit - len(candidates))
    if remaining:
        candidates.extend(_public_regional_knowledge_facts(db, identity, remaining))

    inserted = 0
    seen: set[str] = set()
    for item in candidates:
        fact_id = str(item.get("fact_id") or "").strip()
        text = str(item.get("text") or "").strip()
        sources = [source for source in (item.get("sources") or []) if isinstance(source, dict)]
        if not fact_id or fact_id in seen or not text or not sources:
            continue
        seen.add(fact_id)
        selected = False
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        db.execute(
            "INSERT OR IGNORE INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
            "VALUES(?,?,?,?,1,?,?)",
            (
                story_id,
                fact_id,
                text[:1200],
                confidence,
                int(selected),
                json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        inserted += 1
    if inserted:
        from .fact_ledger import backfill_legacy_fact_ledger, refresh_review_status
        now = float(db.execute("SELECT unixepoch('subsec')").fetchone()[0])
        backfill_legacy_fact_ledger(db, now)
        refreshed: set[str] = set()
        for item in candidates:
            if item.get('origin') != 'poi_research':
                continue
            fact_id = str(item.get('fact_id') or '')
            memory = db.execute('SELECT review_story_id FROM poi_research_assertions WHERE poi_key=? AND assertion_id=?', (poi_key(identity), fact_id)).fetchone()
            origin = str(memory['review_story_id'] or '') if memory else ''
            if not origin or origin == story_id:
                continue
            if origin not in refreshed:
                refresh_review_status(db, origin, now)
                refreshed.add(origin)
            source = db.execute("SELECT a.display_text,a.revision_digest FROM fact_assertions a JOIN stories s ON s.id=a.story_id WHERE a.story_id=? AND a.assertion_id=? AND a.eligibility='eligible' AND json_extract(s.research_json,'$.visual_identity.candidate_id')=?", (origin, fact_id, poi_key(identity))).fetchone()
            target = db.execute('SELECT display_text,revision_digest FROM fact_assertions WHERE story_id=? AND assertion_id=?', (story_id, fact_id)).fetchone()
            if not source or not target or tuple(source) != tuple(target):
                continue
            # Reuse an existing model decision only for the SAME literal claim
            # and evidence revision. Never promote a changed cache snapshot.
            for scan in db.execute("SELECT * FROM fact_conflict_scans WHERE story_id=? AND status IN ('ok','no_candidates') AND coverage_complete=1 ORDER BY id DESC", (origin,)):
                if json.loads(scan['revision_bundle_json'] or '{}').get(fact_id) != source['revision_digest']:
                    continue
                db.execute("INSERT INTO fact_conflict_scans(story_id,poi_key,run_id,detector,status,pair_count,detected_count,coverage_complete,revision_bundle_json,conflict_ids_json,missing_aspects_json,error_type,created_at) VALUES(?,?,?,'poi_memory_reuse','no_candidates',0,0,1,?,'[]','[]',NULL,?)",
                           (story_id, poi_key(identity), f"poi-memory:{origin}:{scan['id']}", json.dumps({fact_id: source['revision_digest']}, sort_keys=True), scan['created_at']))
                break
        refresh_review_status(db, story_id, now)
    return inserted


def _research_memory_facts(
    db,
    identity: dict[str, Any],
    limit: int,
    *,
    include_unreviewed: bool = True,
) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    if include_unreviewed:
        where = (
            "poi_key=? AND eligibility<>'withheld' "
            "AND review_status<>'quarantined'"
        )
    else:
        where = "poi_key=? AND eligibility='eligible'"
    rows = list(db.execute(
        "SELECT assertion_id,semantic_key,text,confidence,sources_json,"
        "review_status,eligibility,updated_at "
        f"FROM poi_research_assertions WHERE {where} "
        "ORDER BY updated_at DESC LIMIT ?",
        (key, max(1, int(limit))),
    ))
    return [
        {
            "fact_id": str(row["assertion_id"]),
            "claim_key": str(row["semantic_key"] or ""),
            "text": str(row["text"]),
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(json.loads(row["sources_json"] or "[]")),
            "selected": False,
            "sources": json.loads(row["sources_json"] or "[]"),
            "review_status": str(row["review_status"]),
            "eligibility": str(row["eligibility"]),
            "origin": "poi_research",
        }
        for row in rows
    ]


def processed_sources(db, identity: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    rows = db.execute(
        "SELECT url,title,last_query,supports_json,last_seen_at "
        "FROM poi_research_sources WHERE poi_key=? ORDER BY last_seen_at DESC LIMIT ?",
        (key, max(1, min(int(limit), 200))),
    )
    return [
        {
            "url": str(row["url"]),
            "title": str(row["title"]),
            "last_query": str(row["last_query"]),
            "supports": json.loads(row["supports_json"] or "[]"),
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def persist_research_memory(
    db,
    identity: dict[str, Any],
    facts: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    query: str,
    now: float,
    research_run_id: str | None = None,
) -> None:
    key = poi_key(identity)
    if not key:
        return
    ensure_poi_identity(db, identity, now=now)
    source_by_url: dict[str, dict[str, Any]] = {}

    def add_source(raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            return
        url = str(raw.get("url") or "").rstrip("/")
        if not url.startswith("https://"):
            return
        prior = source_by_url.get(url)
        source_by_url[url] = (
            merge_source_payloads([prior], [raw])[0]
            if prior is not None
            else merge_source_payloads([raw])[0]
        )

    for source in sources:
        add_source(source)
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        for source in fact.get("sources") or []:
            add_source(source)

    for url, source in source_by_url.items():
        new_supports = [
            item for item in (source.get("supports") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        current_source = db.execute(
            "SELECT title,supports_json,first_seen_at FROM poi_research_sources "
            "WHERE poi_key=? AND url=?",
            (key, url),
        ).fetchone()
        old_supports = (
            json.loads(current_source["supports_json"] or "[]")
            if current_source else []
        )
        merged_source = merge_source_payloads(
            [{
                "url": url,
                "title": str(current_source["title"]) if current_source else "",
                "supports": old_supports,
            }],
            [{
                **source,
                "url": url,
                "supports": new_supports,
            }],
        )[0]
        merged_supports = [
            item for item in (merged_source.get("supports") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        first_seen = float(current_source["first_seen_at"]) if current_source else now
        db.execute(
            "INSERT INTO poi_research_sources(poi_key,url,title,supports_json,last_query,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(poi_key,url) DO UPDATE SET "
            "title=excluded.title,supports_json=excluded.supports_json,"
            "last_query=excluded.last_query,last_seen_at=excluded.last_seen_at",
            (
                key,
                url,
                str(merged_source.get("title") or url)[:300],
                json.dumps(merged_supports, ensure_ascii=False, separators=(",", ":")),
                str(query or "")[:1000],
                first_seen,
                now,
            ),
        )

    for fact in facts:
        if not isinstance(fact, dict):
            continue
        claim_key = normalized_claim_key(fact.get("claim_key"))
        assertion_id = str(fact.get("fact_id") or "").strip()
        text = str(fact.get("text") or "").strip()
        fact_sources = [
            source for source in (fact.get("sources") or [])
            if isinstance(source, dict)
            and str(source.get("url") or "").startswith("https://")
            and any(
                isinstance(support, dict)
                and str(support.get("text") or "").strip()
                for support in (source.get("supports") or [])
            )
        ]
        if not assertion_id or not text or not fact_sources:
            continue
        try:
            confidence = max(0.0, min(1.0, float(fact.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        current = db.execute(
            "SELECT semantic_key,text,confidence,sources_json,created_at "
            "FROM poi_research_assertions WHERE poi_key=? AND assertion_id=?",
            (key, assertion_id),
        ).fetchone()
        prior_sources = (
            json.loads(current["sources_json"] or "[]")
            if current else []
        )
        merged_sources = merge_source_payloads(prior_sources, fact_sources)
        created_at = float(current["created_at"]) if current else now

        observation_id = _research_observation_id(
            key,
            assertion_id,
            text,
            fact_sources,
            research_run_id,
            query,
        )
        db.execute(
            "INSERT OR IGNORE INTO poi_research_observations("
            "observation_id,poi_key,assertion_id,semantic_key,text,confidence,sources_json,"
            "research_run_id,query,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                key,
                assertion_id,
                claim_key,
                text[:1200],
                confidence,
                json.dumps(fact_sources, ensure_ascii=False, separators=(",", ":")),
                str(research_run_id or "")[:120] or None,
                str(query or "")[:1000],
                now,
            ),
        )
        db.execute(
            "INSERT INTO poi_research_assertions("
            "poi_key,assertion_id,semantic_key,text,confidence,sources_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(poi_key,assertion_id) DO UPDATE SET "
            "semantic_key=COALESCE(excluded.semantic_key,poi_research_assertions.semantic_key),"
            "text=excluded.text,"
            "confidence=MAX(poi_research_assertions.confidence,excluded.confidence),"
            "sources_json=excluded.sources_json,updated_at=excluded.updated_at",
            (
                key,
                assertion_id,
                claim_key,
                text[:1200],
                confidence,
                json.dumps(merged_sources, ensure_ascii=False, separators=(",", ":")),
                created_at,
                now,
            ),
        )

        # Compatibility snapshot only. Never update by semantic key: this table
        # is intentionally lossy and no longer authoritative.
        if claim_key:
            db.execute(
                "INSERT OR IGNORE INTO poi_research_facts("
                "poi_key,claim_key,fact_id,text,confidence,sources_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    claim_key,
                    assertion_id,
                    text[:1200],
                    confidence,
                    json.dumps(merged_sources, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    now,
                ),
            )

def _public_regional_knowledge_facts(db, identity: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    aliases = _identity_alias_values(identity)
    if not aliases:
        return []
    normalized = [" ".join(value.split()).casefold() for value in aliases]
    placeholders = ",".join("?" for _ in normalized)
    poi_rows = list(db.execute(
        f"SELECT DISTINCT poi_id FROM poi_aliases WHERE normalized_value IN ({placeholders})",
        tuple(normalized),
    ))
    if len(poi_rows) != 1:
        return []
    poi_id = str(poi_rows[0]["poi_id"])
    rows = db.execute(
        """
        SELECT c.id,c.semantic_key,c.kind,c.text,c.status,
               e.evidence_ref,e.source_family_id,e.author_score,e.publication_score,
               e.provenance_score,e.verification_score,e.evidence_json,
               x.source_ref,x.payload_json,x.updated_at
        FROM poi_claims c
        JOIN poi_claim_evidence e ON e.claim_id=c.id
        JOIN poi_external_events x ON x.event_id=e.event_id
        WHERE c.poi_id=? AND x.visibility='public'
          AND c.status IN ('candidate','accepted','contested')
        ORDER BY x.updated_at DESC
        LIMIT ?
        """,
        (poi_id, max(1, int(limit))),
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fact_id = str(row["id"])
        if fact_id in seen:
            continue
        seen.add(fact_id)
        try:
            evidence_payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence_payload = {}
        result.append({
            "fact_id": fact_id,
            "claim_key": str(row["semantic_key"] or ""),
            "text": str(row["text"]),
            "confidence": (float(row["verification_score"]) / 100.0) if row["verification_score"] is not None else 0.0,
            "evidence_supported": True,
            "selected": False,
            "sources": [{
                "type": "regional_knowledge",
                "title": str(row["source_ref"]),
                "ref": str(row["evidence_ref"]),
                "source_family_id": str(row["source_family_id"]),
                "author_subject_authority": row["author_score"],
                "publication_method_score": row["publication_score"],
                "provenance_precision_score": row["provenance_score"],
                "evidence_verification_score": row["verification_score"],
                "evidence": evidence_payload,
            }],
            "poi_id": poi_id,
            "poi_claim_status": str(row["status"]),
        })
        if len(result) >= limit:
            break
    return result


def prior_facts(db, identity: dict[str, Any], story_id: str, limit: int = 60) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _research_memory_facts(
        db,
        identity,
        limit,
        include_unreviewed=True,
    ):
        if item["fact_id"] in seen:
            continue
        seen.add(item["fact_id"])
        result.append(item)
        if len(result) >= limit:
            return result

    rows = db.execute(
        "SELECT f.fact_id,f.text,f.confidence,f.evidence_supported,f.selected,f.sources_json,s.updated_at "
        "FROM facts f JOIN stories s ON s.id=f.story_id "
        "LEFT JOIN fact_assertions a "
        "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
        "WHERE s.id<>? AND json_extract(s.research_json,'$.visual_identity.candidate_id')=? "
        "AND COALESCE(a.eligibility,'unreviewed')<>'withheld' "
        "AND COALESCE(a.review_status,'unreviewed')<>'quarantined' "
        "ORDER BY s.updated_at DESC,f.rowid LIMIT ?",
        (story_id, key, limit),
    )
    for row in rows:
        fact_id = str(row["fact_id"])
        if fact_id in seen:
            continue
        seen.add(fact_id)
        result.append({
            "fact_id": fact_id,
            "claim_key": "",
            "text": str(row["text"]),
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "sources": json.loads(row["sources_json"] or "[]"),
            "origin": "street_story",
        })
        if len(result) >= limit:
            return result

    for item in _public_regional_knowledge_facts(db, identity, limit - len(result)):
        if item["fact_id"] in seen:
            continue
        seen.add(item["fact_id"])
        result.append({**item, "origin": "regional_knowledge"})
        if len(result) >= limit:
            break
    return result
