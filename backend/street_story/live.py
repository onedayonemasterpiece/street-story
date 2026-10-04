from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import uuid
from urllib.parse import unquote
from collections import deque
from datetime import datetime, timezone
from typing import Any

from live_interaction import LiveSocketSessionHost as LiveSessionHost

from .live_visual_comparison import LiveVisualComparisonMixin
from .config import Settings
from .research_budget import PAGE_UNITS, response_units, bounded_inventory
from . import review_packets, research_repairs
from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .gemini import GeminiUnavailable
from .fact_conflicts import (
    conflict_rows,
    conflict_scan_items,
    normalize_model_conflict_records,
    persist_fact_conflicts,
    record_fact_review_scan,
    resolve_fact_conflict,
)
from .fact_ledger import (
    candidate_assertion_id,
    eligibility_issues_for_ids,
    eligible_selected_fact_ids,
    fact_revision_bundle,
    persist_fact_candidates,
    persist_fact_relation_events,
    refresh_review_status,
    revision_bundle_issues,
    selected_eligibility_issues,
    set_owner_selection,
)
from .model_facts import (
    merge_model_fact_inventory,
    normalized_claim_key,
    validated_model_fact_text,
)
from .live_author_intent import (
    begin_turn,
    consent_receipt,
    has_place_consent,
    noise_receipt,
    observe_input_timing,
    observe_transcript,
    suspected_noise_turn,
)
from .research_runs import (
    begin_research_run,
    chunk_checkpoint,
    mark_chunk,
    manifest_complete,
    record_chunk_batch,
    register_discovered_source,
    run_manifest,
    set_run_state,
)
from .service import ConflictError, InvalidStateError, StreetStoryService, canonical, digest


logger = logging.getLogger("street_story.live")


LIVE_SCHEMA = review_packets.SCHEMA + r"""
CREATE TABLE IF NOT EXISTS live_editor_state(
  story_id TEXT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
  text_revision INTEGER NOT NULL DEFAULT 0,
  literal_json TEXT NOT NULL DEFAULT '[]',
  history_json TEXT NOT NULL DEFAULT '[]',
  last_change TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_commands(
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  command_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(story_id,command_id)
);
CREATE TABLE IF NOT EXISTS live_publication_confirmations(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  text_revision INTEGER NOT NULL,
  text_value TEXT NOT NULL,
  visual_revision TEXT NOT NULL,
  asset_ref TEXT NOT NULL,
  destinations_json TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  timezone TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS live_confirmation_story_idx
  ON live_publication_confirmations(story_id,created_at DESC);
CREATE TABLE IF NOT EXISTS live_diagnostics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS live_diagnostics_story_time_idx
  ON live_diagnostics(story_id,created_at DESC);
CREATE INDEX IF NOT EXISTS live_diagnostics_session_time_idx
  ON live_diagnostics(session_id,created_at DESC);
"""


def ensure_live_schema(service: StreetStoryService) -> None:
    with service.store.connection() as db:
        db.executescript(LIVE_SCHEMA)
    _backfill_legacy_live_messages(service)


def _merge_live_transcript(current: str, fragment: str) -> str:
    current = str(current or "").strip()
    fragment = str(fragment or "").strip()
    if not current:
        return fragment
    if not fragment:
        return current
    if fragment.startswith(current):
        return fragment
    if current.endswith(fragment):
        return current
    overlap = min(len(current), len(fragment))
    while overlap >= 3 and current[-overlap:] != fragment[:overlap]:
        overlap -= 1
    return (current + fragment[overlap:]) if overlap >= 3 else f"{current} {fragment}"


def _backfill_legacy_live_messages(service: StreetStoryService) -> None:
    """Recover pre-durable Live chat from retained transcript diagnostics.

    Only stories with no durable chat are eligible. Known suspected-noise turns
    are skipped. This is a one-time recovery path for sessions recorded before
    live_messages existed.
    """
    with service.store.connection() as db:
        story_ids = [
            str(row["story_id"])
            for row in db.execute(
                "SELECT DISTINCT d.story_id FROM live_diagnostics d "
                "WHERE d.event_type IN ('input_transcript','output_transcript') "
                "AND NOT EXISTS(SELECT 1 FROM live_messages m WHERE m.story_id=d.story_id)"
            )
        ]

    for story_id in story_ids:
        with service.store.connection() as db:
            rows = list(db.execute(
                "SELECT id,session_id,event_type,payload_json,created_at "
                "FROM live_diagnostics WHERE story_id=? AND event_type IN "
                "('input_transcript','output_transcript','turn_complete','interrupted','suspected_noise_turn') "
                "ORDER BY id LIMIT 1200",
                (story_id,),
            ))
        if not rows:
            continue

        messages: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        skip_noise: set[str] = set()
        seq_by_session: dict[str, int] = {}

        def finish() -> None:
            nonlocal active
            if active is not None and str(active.get("text") or "").strip():
                active["final"] = True
                messages.append(active)
            active = None

        for row in rows:
            session_id = str(row["session_id"] or "")
            event_type = str(row["event_type"])
            if event_type == "suspected_noise_turn":
                skip_noise.add(session_id)
                continue
            if event_type in {"turn_complete", "interrupted"}:
                finish()
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            role = "user" if event_type == "input_transcript" else "assistant"
            if role == "user" and session_id in skip_noise:
                skip_noise.discard(session_id)
                continue
            if active is not None and active["session_id"] == session_id and active["role"] == role:
                active["text"] = _merge_live_transcript(str(active["text"]), text)[:8000]
                active["updated_at"] = float(row["created_at"])
                continue
            finish()
            seq = seq_by_session.get(session_id, 0) + 1
            seq_by_session[session_id] = seq
            active = {
                "session_id": session_id,
                "message_key": f"legacy:{session_id}:{seq}:{role}",
                "role": role,
                "text": text[:8000],
                "final": False,
                "created_at": float(row["created_at"]),
                "updated_at": float(row["created_at"]),
            }
        finish()

        if not messages:
            continue
        with service.store.tx() as db:
            if db.execute("SELECT 1 FROM live_messages WHERE story_id=? LIMIT 1", (story_id,)).fetchone():
                continue
            if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
                continue
            for item in messages[-100:]:
                db.execute(
                    "INSERT OR IGNORE INTO live_messages("
                    "story_id,session_id,message_key,role,text,final,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        story_id, item["session_id"], item["message_key"], item["role"],
                        item["text"], int(bool(item["final"])),
                        item["created_at"], item["updated_at"],
                    ),
                )
        logger.info(
            "street_story_live_history_backfill %s",
            canonical({"story_id": story_id, "message_count": len(messages[-100:])}),
        )


def live_history(service: StreetStoryService, story_id: str, limit: int = 8) -> list[dict[str, str]]:
    with service.store.connection() as db:
        rows = list(db.execute(
            "SELECT role,text FROM live_messages WHERE story_id=? AND text<>'' ORDER BY id DESC LIMIT ?",
            (story_id, max(1, min(int(limit), 20))),
        ))
    rows.reverse()
    return [
        {"role": "model" if str(row["role"]) == "assistant" else "user", "text": str(row["text"])[:700]}
        for row in rows
        if str(row["role"]) in {"user", "assistant"} and str(row["text"]).strip()
    ]


def _diagnostic_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _diagnostic_value(item, depth + 1)
            for key, item in list(value.items())[:48]
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item, depth + 1) for item in list(value)[:48]]
    return str(value)[:500]


def record_live_diagnostic(
    service: StreetStoryService,
    story_id: str,
    session_id: str,
    source: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if not re.fullmatch(r"story_[A-Za-z0-9]{8,80}", story_id):
        return
    if not re.fullmatch(r"live_[A-Za-z0-9]{8,80}", session_id):
        return
    source = str(source or "unknown")[:40]
    event_type = str(event_type or "unknown")[:80]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", event_type):
        return
    safe = _diagnostic_value(payload or {})
    now = service.store.now()
    with service.store.tx() as db:
        if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
            return
        db.execute(
            "INSERT INTO live_diagnostics(story_id,session_id,source,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (story_id, session_id, source, event_type, canonical(safe), now),
        )
        db.execute("DELETE FROM live_diagnostics WHERE created_at < ?", (now - 7 * 24 * 3600,))


def _bounded_text(value: Any, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ConflictError("live_text_required", "Text is required")
    if len(text) > limit:
        raise ConflictError("live_text_too_long", f"Text exceeds {limit} characters")
    return text


def _search_source_ref(url: str) -> str:
    canonical_url = str(url or "").rstrip("/")
    return "websrc_" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
    }
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "parameters": schema}


REPAIR_FIELDS = {
    "fact": {"type": "integer"}, "reason": {"type": "string"},
    "facts": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "claim_key": {"type": "string"}, "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": {"type": "integer"}},
        "context_refs": {"type": "array", "items": {"type": "string"}},
    }, "required": ["text", "claim_key"]}},
}


FUNCTIONS = [
    _tool_schema('find_place_articles',
        'Find actual article URLs for uncertain photo identity. Discovery hypotheses only, never facts or visual proof. Then compare_place_images examines the article illustrations.',
        {'query': {'type': 'string'}}, ['query']),
    _tool_schema('compare_place_images',
        'Show SOURCE and the next article illustrations to this same Live model for visual comparison. After Wikipedia fails, automatically search up to 20 websites and examine their article images, including later gallery photos. Call record_place_comparison after every group.',
        {'query': {'type': 'string', 'description': 'Object-name hypothesis or visible distinctive details; never author confirmation.'},
         'article_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional actual article URLs found by your native Google Search. They are fetched as hypotheses; only decoded illustrations and your comparison prove identity.'}}),
    _tool_schema('record_place_comparison',
        'Record YOUR visual comparison of the SOURCE/REF snapshot just received. This is model evidence, not author consent. Match requires distinctive repeated details and confidence >=0.90. On no match continue compare_place_images.',
        {'comparison_id': {'type': 'string'}, 'status': {'type': 'string', 'enum': ['match', 'uncertain', 'mismatch']},
         'candidate_id': {'type': 'string'}, 'object_name': {'type': 'string'}, 'confidence': {'type': 'number'},
         'observations': {'type': 'array', 'items': {'type': 'string'}},
         'alternative_candidate_ids': {'type': 'array', 'items': {'type': 'string'}}},
        ['comparison_id', 'status', 'candidate_id', 'confidence', 'observations', 'alternative_candidate_ids']),
    _tool_schema(
        "read_topic",
        "Read the current authoritative Street Story topic, facts, visual and publication state. No mutation.",
    ),
    _tool_schema(
        "get_facts",
        "Read the durable fact inventory page by page when read_topic's compact projection is not enough. "
        "Use this before semantic deduplication, contradiction review or arbitration that may involve facts outside the snapshot.",
        {
            "cursor": {
                "type": "integer",
                "description": "Opaque server cursor from the previous page. Omit for the first page.",
            },
            "limit": {
                "type": "integer",
                "description": "Page size from 1 to 50. Defaults to 30.",
            },
            "selected_only": {"type": "boolean"},
            "eligibility": {
                "type": "string",
                "enum": ["all", "unreviewed", "eligible", "withheld"],
            },
        },
    ),
    _tool_schema(
        "get_evidence",
        "Read exact durable evidence spans for one or more facts. Returns source URL/version, chunk ID, offsets, "
        "support kind and verbatim span text. Use it for evidence comparison and conflict arbitration instead of relying "
        "on source counts or compact summaries.",
        {
            "fact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One to 20 exact fact IDs from get_facts/read_topic.",
            },
            "cursor": {
                "type": "integer",
                "description": "Opaque server cursor from the previous page. Omit for the first page.",
            },
            "limit": {
                "type": "integer",
                "description": "Live evidence page size is capped at 5 to respect the shared response budget. Follow next_cursor until has_more=false.",
            },
        },
        ["fact_ids"],
    ),
    _tool_schema(
        "get_research_chunk",
        "Read a document chunk of the SAME research run before facts exist. Omit chunk_id for the next unfinished "
        "chunk. For the first document choose a discovered source_ref by competence and provenance; copy its short ref instead of rewriting a long URL. source_url remains supported for exact legacy URLs. Uses the guarded fetch pipeline. Returns frozen "
        "source version, exact core/context, batch_id and expected_story_revision. Resume does not repeat completed chunks.",
        {"run_id": {"type": "string"}, "source_ref": {"type": "string", "description": "First read: choose a competent discovery source and copy exact source_ref. After a saved page, use empty string to follow the next unread page/source."}, "source_url": {"type": "string"}, "chunk_id": {"type": "string"}, "passage_cursor": {"type": "integer", "description": "Follow next_passage_cursor before completing this chunk; unseen pages remain pending."}},
        ["run_id", "source_ref"],
    ),
    _tool_schema(
        "resolve_place",
        "Resolve the photographed place before factual research. Uses the source photo plus GPS when available, OSM and nearby Wikipedia candidates, and visual identity. This does not publish or rewrite the post.",
        {
            "owner_hint": {
                "type": "string",
                "description": "Optional concise place/object name explicitly stated by the author, for example 'Бранденбургские ворота, Калининград'.",
            },
        },
    ),
    _tool_schema(
        "confirm_place",
        "Confirm the photographed place after the author explicitly identifies or confirms it. Prefer candidate_id returned by resolve_place; candidate_name is allowed when it uniquely matches a returned candidate.",
        {
            "candidate_id": {"type": "string"},
            "candidate_name": {"type": "string"},
        },
    ),
    _tool_schema(
        "reject_place",
        "Reject the CURRENT photographed object only when the author explicitly says it is wrong. Invalidates old factual/visual approval and tries alternatives without reselecting the rejected candidate. Does not delete the photo or publish.",
        {"candidate_id": {"type": "string"}, "reason": {"type": "string"}},
        ["candidate_id"],
    ),
    _tool_schema(
        "search_web",
        "Search one focused aspect of the identified subject for evidence. For a broad request to collect facts, "
        "Mira should read credible discovered documents in small pages, then search only specific remaining gaps, semantically merge the "
        "results and enrich already-known facts with new supporting sources. The result returns to this same Gemini "
        "Live conversation and never rewrites publication text by itself.",
        {
            "confirmed_poi_id": {"type": "string", "description": "Copy candidate_id of the currently confirmed visual_identity."},
            "query_matches_poi": {"type": "boolean", "description": "Your semantic check of query and goal against confirmed canonical name, aliases and geography. False means correct the query before searching."},
            "query": {
                "type": "string",
                "description": "Concise internet-search query. It may be optimized for retrieval.",
            },
            "coverage_goal": {
                "type": "string",
                "description": "What the evidence must actually answer. Preserve important user constraints such as left/center/right positions, exact names, authorship, dates or inscriptions even if the search query is shorter.",
            },
        },
        ["query", "confirmed_poi_id", "query_matches_poi"],
    ),
    _tool_schema(
        "save_research_facts",
        "Persist Mira's semantic extraction from the most recent search_web discovery evidence. "
        "Use only when search_web returned discovery-only sources/snippets without durable facts. "
        "Every source_ref and evidence_ref must be copied exactly from the latest discovery-only search result. "
        "For each fact choose only the exact evidence_refs whose passages support that fact; a source URL by itself is not evidence. "
        "The server validates refs and preserves only the selected passages without inferring fact meaning.",
        {
            "run_id": {
                "type": "string",
                "description": "Exact research_run_id returned by search_web. Required when more than one discovery run could be current.",
            },
            "batch_id": {
                "type": "string",
                "description": "Exact save_batch_id returned by search_web. Guards replay and cross-run writes.",
            },
            "chunk_id": {"type": "string"},
            "batch_index": {"type": "integer"},
            "expected_story_revision": {"type": "integer"},
            "continuation_needed": {"type": "boolean"},
            "inventory_reviewed": {"type": "boolean", "description": "True only after Mira read the whole existing inventory and chose equivalence IDs herself."},
            "source_matches_poi": {"type": "boolean", "description": "Your semantic check that these source passages concern the confirmed object, including city/geography. Wrong-object cores must be checkpointed facts=[]; never import their claims."},
            "source_content_valid": {"type": "boolean", "description": "False for navigation/menu/challenge/error fragments instead of readable article content. They cannot establish article completion."},
            "batch_reviewed": {"type": "boolean", "description": "True after checking this small batch against its OWN chosen passages and local duplicate/conflict context. Eligible findings can be used immediately; this is not a full-inventory review."},
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_key": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["supported", "insufficient", "contradicted", "possible_conflict"]},
                        "atomic": {"type": "boolean"},
                        "support_complete": {"type": "boolean"},
                        "qualifiers_preserved": {"type": "boolean"},
                        "review_reason": {"type": "string", "description": "Brief checkable support or withholding reason, never private reasoning."},
                        "existing_fact_id": {"type": "string"},
                        "text": {"type": "string", "description": "One independently selectable atomic assertion. Each named figure/person gets a separate fact; never bundle a list of people, separate roles or events in one fact."},
                        "evidence_quotes": {"type": "array", "items": {"type": "string"}, "description": "Optional verbatim alternative to evidence_refs from chunk evidence_passages. Never rewrite the quoted source."},
                        "confidence": {"type": "number"},
                        "selected": {"type": "boolean"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "passage_ids": {"type": "array", "items": {"type": "integer"}, "description": "For chunk batches, choose numeric passage_id from evidence_passages. Prefer these to copying long evidence_ref hashes. Leave evidence_refs/source_refs empty when using passage_ids."},
                    },
                    "required": [
                        "claim_key", "text", "confidence", "selected", "verdict", "atomic", "support_complete", "qualifiers_preserved", "review_reason",
                        "source_refs", "evidence_refs",
                    ],
                },
            },
        },
        ["facts", "source_matches_poi", "batch_reviewed"],
    ),
    _tool_schema(
        "record_fact_conflicts",
        "Persist conflicts that Mira itself detects between current evidence-backed facts. "
        "The server validates fact IDs and relation shape only; it does not choose conflicting pairs.",
        {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "left_fact_id": {"type": "string"},
                        "right_fact_id": {"type": "string"},
                        "relation": {
                            "type": "string",
                            "enum": [
                                "contradiction",
                                "scope_difference",
                                "temporal_sequence",
                                "source_disagreement",
                                "uncertain",
                            ],
                        },
                        "suggested_resolution": {
                            "type": "string",
                            "enum": ["prefer_left", "prefer_right", "both_valid", "unresolved"],
                        },
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "left_fact_id",
                        "right_fact_id",
                        "relation",
                        "suggested_resolution",
                        "confidence",
                        "rationale",
                    ],
                },
            },
        },
        ["conflicts"],
    ),
    _tool_schema("get_review_packet", "Read a frozen semantic review packet. Follow cursor until has_more=false. Local fact/evidence numbers replace long hashes. Compare support, negation, roles, equivalence and conflicts across all pages. To reconsider a completed or erroneous review, supply supersedes_packet_ref and its run_id; this creates an independent versioned attempt, retaining the old receipt.",
        {"run_id": {"type": "string"}, "packet_ref": {"type": "string"}, "cursor": {"type": "integer"}, "supersedes_packet_ref": {"type": "string"}, "recheck_facts": {"type": "array", "items": {"type": "integer"}}, "allow_partial_review": {"type": "boolean", "description": "Explicitly request partial coverage review with missing_aspects; otherwise finish all cores first."}}),
    _tool_schema("get_review_context", "Read adjacent literal passages from the SAME retained source version, even after extraction completed. Does not change checkpoints or search again. Context is not supporting evidence until explicitly attached by repair_research_fact. Follow next_args for the bounded window, or document_cursor for another addressed window.",
        {"packet_ref": {"type": "string"}, "fact": {"type": "integer"}, "evidence": {"type": "integer"}, "cursor": {"type": "integer"}, "document_cursor": {"type": "integer"}}, ["packet_ref", "fact"]),
    _tool_schema("assess_review_packet", "Ask the configured research model for independent semantic advice on this frozen packet. Follow next_args for all bounded batches. Advice is not final eligibility: Mira must retrieve context, bind evidence, batch-repair and review current revisions. If unavailable, Mira performs the same checks herself.",
        {"packet_ref": {"type": "string"}, "cursor": {"type": "integer"}, "decision_cursor": {"type": "integer"}}, ["packet_ref"]),
    _tool_schema("repair_research_fact", "Atomically repair 1–12 candidates from this SAME frozen packet before requesting a new one. Model owns evidence attachment, narrowed text and splitting. Read missing context first, then submit all needed repairs together. Same-text repair preserves selection; changed meanings are new unselected claims. Retain original observations/lineage, withhold old variants, then get a NEW packet and review. Max 8 replacements per parent, 2 repairs per parent and 12 per run.",
        {"packet_ref": {"type": "string"}, "repairs": {"type": "array", "items": {"type": "object", "properties": REPAIR_FIELDS, "required": ["fact", "reason", "facts"]}}}, ["packet_ref", "repairs"]),
    _tool_schema(
        "finalize_fact_review",
        "Commit semantic decisions for a frozen packet returned by get_review_packet. Never use a batch ID as packet_ref. Read all packet pages, use exact ZERO-BASED fact/evidence numbers, explicitly assess support/negation/roles/equivalence and compare relations across pages. Does not publish.",
        {
            "packet_ref": {"type": "string", "description": "Copy ONLY packet_ref returned by get_review_packet; never invent it."},
            "decisions": {"type": "array", "items": {"type": "object", "properties": {
                "fact": {"type": "integer", "description": "Zero-based local fact number from packet items."},
                "verdict": {"type": "string", "enum": ["supported", "not_supported", "contradicted", "role_mismatch", "insufficient", "repair_needed"]},
                "reason": {"type": "string", "description": "Brief basis in THIS fact's attached spans; do not borrow unbound evidence from another candidate."},
                "atomic": {"type": "boolean"}, "support_complete": {"type": "boolean"}, "qualifiers_preserved": {"type": "boolean"},
                "claims": {"type": "array", "items": {"type": "string"}, "description": "Enumerate independently selectable assertions actually present in the candidate; each depicted person is independently selectable. Multiple entries require repair/split before supported."},
                "basis_quotes": {"type": "array", "items": {"type": "string"}, "description": "Literal short quotations from this fact's selected attached evidence. Every substantive attribute must follow; never quote another candidate's passage. Empty only for unsupported decisions."},
                "evidence": {"type": "array", "items": {"type": "integer"}, "description": "Zero-based evidence numbers within THIS fact, from packet items."},
                "equivalent_to": {"type": "integer", "description": "Optional canonical fact number for a semantic duplicate. Still return an explicit support verdict for EVERY fact including the canonical one. A supported canonical may reference itself."}}, "required": ["fact", "verdict", "evidence", "reason", "atomic", "support_complete", "qualifiers_preserved", "claims", "basis_quotes"]}},
            "relations_complete": {"type": "boolean", "description": "True only after comparing ALL packet pages for equivalence and conflicts."},
            "conflicts": {"type": "array", "items": {"type": "object", "properties": {
                "left": {"type": "integer"}, "right": {"type": "integer"},
                "relation": {"type": "string", "enum": ["contradiction", "scope_difference", "temporal_sequence", "source_disagreement", "uncertain"]},
                "resolution": {"type": "string", "enum": ["prefer_left", "prefer_right", "both_valid", "unresolved"]},
                "confidence": {"type": "number"}, "rationale": {"type": "string"}},
                "required": ["left", "right", "relation", "resolution", "confidence", "rationale"]}},
            "coverage_complete": {"type": "boolean"},
            "missing_aspects": {"type": "array", "items": {"type": "string"}},
        },
        ["packet_ref", "decisions", "relations_complete", "conflicts", "coverage_complete", "missing_aspects"],
    ),
    _tool_schema(
        "resolve_fact_conflict",
        "Record Mira's evidence-based arbitration of an already detected fact conflict. "
        "This changes only the internal conflict ledger; it does not silently rewrite the publication or hide facts.",
        {
            "conflict_id": {"type": "string"},
            "resolution": {
                "type": "string",
                "enum": ["prefer_left", "prefer_right", "both_valid", "unresolved"],
            },
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        ["conflict_id", "resolution", "reason", "confidence"],
    ),
    _tool_schema(
        "select_facts",
        "Change selected evidence-backed facts without rewriting the current publication text.",
        {
            "fact_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["fact_ids"],
    ),
    _tool_schema(
        "set_concept",
        "Store or replace the current publication concept/angle without silently rewriting the draft. "
        "Use when the author says what the story should focus on; if this changes selected facts, "
        "call select_facts separately and tell the author what changed.",
        {"concept": {"type": "string"}},
        ["concept"],
    ),
    _tool_schema(
        "edit_text",
        "Replace the current publication text after an author editing request. Preserve literal spans unless the author explicitly allowed changing them.",
        {
            "expected_text_revision": {"type": "integer"},
            "new_text": {"type": "string"},
            "change_summary": {"type": "string"},
            "allow_literal_changes": {"type": "boolean"},
        },
        ["expected_text_revision", "new_text", "change_summary"],
    ),
    _tool_schema(
        "literal_begin",
        "Begin a temporary verbatim-dictation mode before the author dictates exact publication wording.",
        {
            "position": {"type": "string", "enum": ["start", "end", "replace_all"]},
        },
        ["position"],
    ),
    _tool_schema(
        "literal_finish",
        "Finish the active verbatim dictation and apply the captured user transcript exactly, excluding the finish command itself.",
    ),
    _tool_schema(
        "literal_cancel",
        "Cancel the active verbatim dictation without changing publication text.",
    ),
    _tool_schema(
        "undo",
        "Undo the most recent accepted text mutation while keeping other independent topic state.",
    ),
    _tool_schema(
        "generate_visual",
        "Generate or regenerate the visual through Street Story's existing VibePublish boundary. Use for visual requests only.",
        {
            "visual_instruction": {
                "type": "string",
                "description": "Optional concise visual-only author instruction such as 'чуть теплее'.",
            },
            "fact_ids": {"type": "array", "items": {"type": "string"}},
        },
    ),
    _tool_schema(
        "prepare_publication",
        "Prepare an exact publication confirmation card. This does not publish.",
        {
            "destinations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": "Exact destination alias from the current Street Story capabilities; do not prefix it with words such as alias or channel.",
                },
            },
            "scheduled_for": {
                "type": "string",
                "description": "Absolute ISO-8601 time with offset, not a relative phrase.",
            },
            "timezone": {"type": "string"},
        },
        ["destinations", "scheduled_for", "timezone"],
    ),
    _tool_schema(
        "confirm_publication",
        "Publish/schedule only an already prepared exact confirmation after one explicit author confirmation.",
        {
            "confirmation_id": {"type": "string"},
        },
        ["confirmation_id"],
    ),
    _tool_schema(
        "cancel_publication",
        "Explicitly cancel the latest scheduled publication through VibePublish and provider readback.",
    ),
]


# The model emits independent claims in each evidence group. The server only
# flattens that model-owned structure; it never splits prose.
_save_declaration = next(f for f in FUNCTIONS if f['name'] == 'save_research_facts')
_save_declaration['description'] = ('Persist checked discovery snippets OR a small frozen document page. For sufficient search snippets, pass exact run_id and batch_id from search_web, and flat facts with nonempty exact source_refs/evidence_refs and every review flag. Each evidence group must include both ref arrays. For frozen document pages these arrays may be empty when numeric passage_ids bind the evidence. Never speak an unsaved snippet as a fact. For document pages use passage_ids/claims groups. '
                                    'The server binds the current read checkpoint; enumerate independent claims grouped by own numeric passage_ids. '
                                    'Do not rewrite quotes or evidence hashes: the server binds these passage numbers to exact immutable source spans. '
                                    'Withhold doubtful claims, save good supported findings immediately, then follow the returned next unread page.')
_finding_schema = _save_declaration['parameters']['properties']['facts']['items']
_finding_schema['properties']['existing_fact_id']['description'] = (
    'Compare the complete known_fact_inventory by meaning before saving. Copy its exact fact_id for an equivalent claim; '
    'use an empty string only for a genuinely new claim. A fact_id belongs here, never in claim_key.'
)
_claim_fields = {k: v for k, v in _finding_schema['properties'].items()
                 if k not in {'source_refs', 'evidence_refs', 'evidence_quotes', 'passage_ids'}}
# Keep the existing direct snippet fields alongside the compact document grouping.
# The backend validates which evidence format applies; neither path invents refs.
_finding_schema['properties']['claims'] = {'type': 'array', 'description': 'Enumerate EACH independently selectable assertion in these passages. Each depicted person or independent role/event is its own object, never one compound sentence.',
                                          'items': {'type': 'object', 'properties': _claim_fields,
                                                    'required': ['claim_key', 'existing_fact_id', 'text', 'confidence', 'selected', 'verdict', 'atomic', 'support_complete', 'qualifiers_preserved', 'review_reason']}}
_finding_schema['required'] = ['source_refs', 'evidence_refs', 'existing_fact_id']
_save_parameters = _save_declaration['parameters']
_save_parameters['properties'] = {key: _save_parameters['properties'][key]
                                  for key in ('run_id', 'batch_id', 'facts', 'batch_reviewed', 'source_matches_poi', 'source_content_valid')}
_save_parameters['required'] = ['facts', 'batch_reviewed', 'source_matches_poi']

SYSTEM_INSTRUCTION = """
You are Street Story's voice editor Mira. Work only on the current topic and its visible image/text publication. Support iterative edits; keep replies brief and useful, in Russian rather than long work reports.

Voice and intent:
- Keep один стабильный голосовой образ Миры: calm natural delivery, steady pace and character; no impersonation, accents or switching voices. Moderate emotion only when appropriate.
- Russian is the owner's default language. Short foreign fragments in silence/rustling are likely ASR noise: do not invent speech or answer unintelligible sounds. Support deliberate coherent foreign speech and explicit language changes.
- A one-word or clearly fragmented input (однословный или явно обрывочный ввод) must not start expensive tools. Clarify intent without asking the owner to name an object they are trying to identify.
- Use only available product functions: no shell/SQL/HTTP or hidden external actions. A mutation is complete only after its result/readback. Never repeat an unknown-result mutation; read state first. Continue the same Live conversation after tool results.

Photo and identity:
- The topic photo is supplied as a separate visual snapshot. Describe only visible features; admit when the snapshot is unavailable. A question "что видно/что ты видишь на фото" is visual: не вызывай resolve_place/search_web just to answer it.
- Backend identification runs automatically after photo selection. EXIF coordinates center nearby OSM/Wikipedia discovery; coordinates alone do not identify the object. visual_identity match/owner_confirmed is mandatory before factual research, final generate_visual or prepare_publication.
- Reuse an automatic match and briefly say the object was found; do not rerun resolve_place without reason. Reuse confirmed identity from the topic.
- For uncertain/mismatch, call compare_place_images and visually compare SOURCE against each REF in this same Live session. After EVERY group call record_place_comparison so the owner sees the processed-illustration counter grow during the work. On no match continue the later gallery photos; broad article search starts automatically after Wikipedia. Stop on proved match or exhausted. If the helper search is unavailable, use native Google Search and pass article_urls; unavailable images are not visual mismatches. Do not ask the owner to name the unknown object. Different pages of one physical building are not competing objects.
- confirm_place requires fresh voluntary explicit owner speech naming and confirming the object. Greetings, "what?", silence, your inference or tool arguments are not consent. Не проси автора подтвердить объект, который он сам пытается определить.
- If the owner says it is the wrong object, call reject_place with current candidate_id instead of repeating confirmation.
- Missing GPS in the supplied copy does not prove the original lacks coordinates. Explain granting geotag access and selecting the original with the topic button.

Research and durable evidence:
- Never invent facts. Broad requests research substantial aspects, including named architectural elements. Read/save material from discovered sources in the current run before searching again for a specific gap. Search count is not a goal: avoid repeated queries and stop when searches add no facts/evidence.
- Separate retrieval query from coverage_goal. Short queries must retain all owner requirements in coverage_goal, including positions such as left/center/right. Use visible sculptures, figures, inscriptions, coats of arms and plaques as coverage hints: targeted search must answer the named detail concretely, not merely describe the building.
- For more findings within the same scope, retain the previous exact coverage_goal. Use a different goal only for a genuinely different question or verification; explain the new missing aspect. Completed unchanged chunks in the same scope are reused. Menu/challenge fragments require source_content_valid=false and facts=[]; do not call them an article without facts.
- discovery_only is not a research result. Sufficient snippets require immediate save_research_facts with run_id=research_run_id, batch_id=save_batch_id, exact source_ref/evidence_ref and batch_reviewed=true. Insufficient snippets require get_research_chunk, not invented or empty snippet claims. Never speak unsaved findings. Report only supported claim text from the successful durable save receipt, without extra remembered details. Only a successful durable save authorizes reporting a claim; do not present old inventory or snippets as newly found facts.
- Attach only each claim's own supporting evidence refs. Semantically equivalent claims use exact existing_fact_id; enrich evidence rather than multiplying paraphrases.
- get_research_chunk is paginated: check/save the current small page before following save receipt next_args to the next unread page. facts=[] means no useful claims on that page, not completed research. Do not skip an unread page to a new search. Do not reread saved pages.
- After no_claims with zero new durable observations, continue get_research_chunk(run_id only), at most three full-source attempts. Only after bounded source exhaustion honestly say no new confirmed facts were found.
- Choose competent full sources by URL/title/provenance: museum, protection catalog or encyclopedia before arbitrary tourist paraphrases. Copy the exact short source_ref; do not rewrite the URL or choose by row order. Check reliability and internal contradictions; a dubious date/style is not supported simply because a page says it.
- Before saving each claim review its passage_ids: verdict, atomic, support_complete, qualifiers_preserved and brief review_reason. batch_reviewed checks only this small batch. Use insufficient/possible_conflict for doubtful claims; good supported claims are immediately durable and available. Preserve time and modality: a projected cost is not an incurred cost, a request is not its outcome. New facts remain selected=false; never select on behalf of the owner.
- completed/partial describe source processing; partial must not hide good saved facts. Normal reviewed batches do not require get_review_packet/finalize_fact_review again.
- Legacy/recovery without batch_reviewed: use the same run, read core/context and save each batch with chunk_id, batch_index, batch_id, expected_story_revision and numeric passage_ids from evidence_passages (or exact evidence_refs/verbatim evidence_quotes). Empty facts=[] is checked no_claims only for that chunk. continuation_needed keeps that chunk for the next batch. Read the full get_facts inventory; inventory_reviewed confirms your equivalence review and permits taking over an unavailable helper's semantic work.
- Explicit old-candidate review: follow assess_review_packet and all next_args when get_review_packet requests it. This is independent verification, not a previous verdict or preapproved result. needs_context requires get_review_context. For compound/repair_needed make one grouped repair_research_fact with precise refs, preserve lineage, reread the new packet and review new revisions. Advice is not review. When the helper is unavailable, perform the semantic review yourself, never declare supported merely to finish.
- supported requires every material attribute in OWN attached spans: dates, roles, quantities, object parts, stages and qualifications. Do not borrow another candidate's evidence. Missing antecedents mean insufficient, not a false event. Do not turn "probably" into certainty. Correct old erroneous support using get_review_packet with supersedes_packet_ref; never overwrite the old receipt.
- One checkbox chooses one independent claim. Separate each person, role, distinct event and date; never save several people as one claim. Atomicity, names, stable claim_key, equivalence, contradictions and evidence sufficiency are your semantic work, not server regex/splitting rules. Check names and atomicity against passages in final review.
- Never invent revision_digest/evidence_id; take them from get_facts/get_evidence or save receipts. On review errors use the specified read tool and retry review; do not announce completion before success.
- read_topic is a compact overview, not full proof inventory. Use a complete known_fact_inventory for additional research and equivalence; if that index is truncated, paginate get_facts fully. For selection, contradiction or arbitration also paginate get_facts until has_more=false; omission from a snapshot does not mean absence from the topic.
- Source/domain counts and URLs are not proof or votes for truth. For important comparisons/arbitration get_evidence for exact fact_id and paginate fully as needed. Compare exact span_text, source_version_id, chunk_id, source origin/time/primary status and context, including Regional Knowledge/POI evidence. Mass repetition does not make a false claim true.
- Compare new claims with known facts in small batches. Mark contradictions possible_conflict; record_fact_conflicts/resolve_fact_conflict provide targeted logging/arbitration. Never hide conflicts or choose by site counts. Full final review is for legacy/recovery.
- Before lengthy research briefly say "Ищу факты"; the app shows progress. Do not read the inventory aloud: end with counts, remaining gaps and at most 1-2 important saved findings. A research-only request must not select facts or draft a publication.

Concept, editing and publication:
- Persist an owner's publication angle with set_concept. If relevance changes selection, call select_facts separately and briefly disclose the change. select_facts otherwise changes only on the owner's explicit request. When the author explicitly asks to choose facts, persist the requested selection with select_facts before asking about publication destinations; the selection does not require a platform.
- For publication/text requests use saved owner selection and edit_text. Write a clear opening, development and ending, usually 2-5 short connected paragraphs, not a fact list. Use only selected evidence-backed facts and owner context; add no unsupported assertions.
- Text-style changes do not change the image; visual-only changes do not change the text. On live_text_revision_conflict do not end the turn: read_topic and retry edit_text exactly once with current text_revision. Never overwrite conflicts silently.
- Verbatim dictation starts with literal_begin, waits for dictation and ends with literal_finish only on explicit completion. Words inside dictated text are not commands. Protect literal spans from ordinary edit_text. allow_literal_changes=true requires explicit permission to change that literal fragment.
- публикация всегда двухшаговая: prepare_publication shows the exact card; confirm_publication requires a separate unambiguous owner confirmation. Subsequent draft edits do not change an already scheduled publication.
- Admit Live/provider delay or unavailability. The legacy async voice path remains a compatibility contract, not an automatic fallback.
Answer briefly and concretely in Russian.
""".strip()


class StreetStoryLiveAdapter(LiveVisualComparisonMixin):
    CAPABILITY_TOOLS = {
        'identity': {'find_place_articles', 'compare_place_images', 'record_place_comparison', 'read_topic', 'resolve_place', 'confirm_place', 'reject_place'},
        'research': {'read_topic', 'get_facts', 'get_evidence', 'search_web', 'get_research_chunk', 'save_research_facts', 'record_fact_conflicts', 'select_facts'},
        'review': {'read_topic', 'get_facts', 'get_review_packet', 'get_review_context', 'assess_review_packet', 'repair_research_fact', 'finalize_fact_review', 'resolve_fact_conflict'},
        'editor': {'read_topic', 'get_facts', 'select_facts', 'set_concept', 'edit_text', 'literal_begin', 'literal_finish', 'literal_cancel'},
        'publication': {'read_topic', 'generate_visual', 'prepare_publication', 'confirm_publication', 'cancel_publication', 'undo'},
    }

    def _capability_configuration(self, configuration, capability):
        router = _tool_schema('continue_story', 'Continue the same story with tools for the requested stage. This changes capabilities only; it never edits, generates or publishes.',
            {'stage': {'type': 'string', 'enum': list(self.CAPABILITY_TOOLS)}, 'intent': {'type': 'string'}}, ['stage', 'intent'])
        configuration = dict(configuration)
        configuration['functions'] = [f for f in configuration['functions'] if f['name'] in self.CAPABILITY_TOOLS[capability]] + [router]
        configuration['search_enabled'] = False
        configuration['application_search_function'] = 'find_place_articles' if capability == 'identity' else 'search_web' if capability == 'research' else ''
        if capability == 'identity':
            configuration['system_instruction'] = (SYSTEM_INSTRUCTION.split('Research and durable evidence:')[0]
                .replace('If the helper search is unavailable, use native Google Search and pass article_urls;',
                         'If the API search is unavailable, report its error and retain the queue for continuation;')
                + '\nAfter a proved match use continue_story stage=research for facts, editor for concept/text, publication for visuals/post. Never invent facts or perform publication before the separate author confirmation.')
        else:
            configuration['system_instruction'] += '\nUse continue_story to access another stage: research, review, editor, publication or identity. Changing stage is not consent for mutations.'
        return configuration

    def resolve_capability(self, session, call):
        if call.get('name') != 'continue_story':
            return None
        stage = (call.get('args') or {}).get('stage')
        if stage not in self.CAPABILITY_TOOLS:
            raise ConflictError('live_stage_invalid', 'Неизвестный этап.')
        initialized = self.initialize(resource_id=session.resource_id, actor=session.actor, model=session.model, full_configuration=True)
        continuation = str((call.get('args') or {}).get('intent') or '')[:1200]
        if stage != 'identity' and (initialized['context'].get('visual_identity') or {}).get('status') not in {'match', 'owner_confirmed'}:
            stage = 'identity'
            continuation = 'Identity is still unresolved. Continue compare_place_images and record_place_comparison with saved references before switching stages.'
        return {'capability': stage, 'configuration': self._capability_configuration(initialized['configuration'], stage),
            'context': initialized['context'], 'continuation': continuation}

    def __init__(self, service: StreetStoryService, emit, write):
        self.service = service
        self.emit = emit
        self.write = write
        ensure_live_schema(service)

    def initialize(self, *, resource_id: str, actor: Any, model: str, **_args: Any) -> dict[str, Any]:
        state = self._topic_state(resource_id)
        # A growing story must still leave lease room for history, photo and
        # the owner's first input. The paginated inventory remains authoritative.
        context = self._compact_context(state, fact_preview_limit=8)
        context.pop('identity_progress', None)  # UI progress is pushed; do not charge it in setup.
        # A short preview hid older facts from additional-research comparison.
        # Supply whole assertion text/IDs cheaply; proofs stay in paginated tools.
        context["facts"] = [fact for fact in context["facts"] if fact["selected"]]
        context["known_fact_inventory_fields"] = ["fact_id", "text"]
        index = []
        for fact in state["story"].get("facts", []):
            proposed = [*index, [fact["fact_id"], fact["text"]]]
            if len(canonical(proposed).encode()) > 12_000:
                break
            index = proposed
        context["known_fact_inventory"] = index
        context["known_fact_inventory_truncated"] = len(index) < context["fact_count"]
        context["facts_instruction"] = (
            "Use the known_fact_inventory for semantic equivalence and missing-aspect research; "
            "reuse its exact existing_fact_id for known claims. It contains assertions, not proof. "
            "If truncated, read all get_facts pages. Read get_facts/get_evidence for selection or verification."
        )
        reviewing = (state.get('research_run') or {}).get('state') == 'verifying'
        # Normal research already has its formation and review rules below.
        # Send the additional legacy candidate policy only during verification;
        # duplicating it on every setup consumes the same lease as bootstrap.
        instruction = ('During research, discovery is not an answer. After search_web, call save_research_facts or get_research_chunk before speaking any factual finding. Only a successful save receipt authorizes reporting that finding.\nResearch formation policy: ' + review_packets.EXTRACTION_CHECKS
                       + '\n' + SYSTEM_INSTRUCTION)
        if reviewing:
            # A resumed verification phase must not frame the old inventory as facts
            # already established by the authoritative product-state snapshot.
            context['candidate_count'] = len(state['story'].get('facts', []))
            context['facts'] = []
            context['review_policy'] = review_packets.REVIEW_CHECKS
            instruction = ('Current phase: independent verification of unverified candidates. '
                           + review_packets.REVIEW_CHECKS + '\n'
                           + instruction)
        initialized = {
            "state": {
                "recent_user": deque(maxlen=24),
                "live_first_research": True,
                "recent_model": deque(maxlen=16),
                "literal": None,
                "live_message_seq": 0,
                "live_message": None,
            },
            "context": context,
            "configuration": {
                "system_instruction": instruction,
                "context_instruction": "Authoritative current topic snapshot; product functions supersede this snapshot when state changes: ",
                "functions": [{**function, "description": (
                    "Save checked snippets or frozen passages. Copy exact nonempty source_refs/evidence_refs for snippets; "
                    "empty arrays only with numeric passage_ids. Compare known_fact_inventory: equivalent claim -> exact existing_fact_id, genuinely new -> empty string. Never speak unsaved findings; follow next_args."
                    if function["name"] == "save_research_facts" else
                    "First document: choose a competent discovery source and copy source_ref. Follow saved next_args for later pages; empty source_ref follows the next unread source."
                    if function["name"] == "get_research_chunk" else function["description"].split(". ")[0][:140]
                )} for function in FUNCTIONS],
                "voice": "Aoede",
                "media_resolution": "MEDIA_RESOLUTION_MEDIUM",
                "manual_activity_detection": True,
                "search_enabled": (state['story'].get('visual_identity') or {}).get('status') not in {'match', 'owner_confirmed'},
                "application_search_function": 'find_place_articles' if (state['story'].get('visual_identity') or {}).get('status') not in {'match', 'owner_confirmed'} else 'search_web',
            },
            "response": {
                "story_id": resource_id,
                "text_revision": state["editor"]["text_revision"],
                "revision": state["story"]["revision"],
            },
        }
        if not _args.get('full_configuration'):
            capability = 'identity' if (state['story'].get('visual_identity') or {}).get('status') not in {'match', 'owner_confirmed'} else 'review' if reviewing else 'research'
            initialized['capability'] = capability
            initialized['configuration'] = self._capability_configuration(initialized['configuration'], capability)
        return initialized

    def input(self, session, message: dict[str, Any]) -> None:
        if session.state.get("research_run_id") and (message.get("activity_start") or message.get("text")):
            session.state["research_author_interrupted"] = True
        if message.get("activity_start"):
            begin_turn(session)
        text = message.get("text")
        if isinstance(text, str) and text.strip() and len(text) <= 4000:
            clean = text.strip()
            # Persist accepted author input before the provider sees it. Text input
            # is not guaranteed to be echoed back as provider input_transcript.
            self._persist_live_message(session, "user", clean)
            begin_turn(session, clean, origin="text")

    def _finalize_live_message(self, session) -> None:
        active = session.state.get("live_message")
        if not isinstance(active, dict) or not active.get("key"):
            session.state["live_message"] = None
            return
        with self.service.store.tx() as db:
            db.execute(
                "UPDATE live_messages SET final=1,updated_at=? WHERE story_id=? AND message_key=?",
                (self.service.store.now(), session.resource_id, str(active["key"])),
            )
        session.state["live_message"] = None

    def _persist_live_message(self, session, role: str, fragment: str) -> None:
        if role not in {"user", "assistant"}:
            return
        fragment = str(fragment or "").strip()
        if not fragment:
            return
        active = session.state.get("live_message")
        now = self.service.store.now()
        with self.service.store.tx() as db:
            if isinstance(active, dict) and active.get("key") and active.get("role") == role:
                row = db.execute(
                    "SELECT text FROM live_messages WHERE story_id=? AND message_key=?",
                    (session.resource_id, str(active["key"])),
                ).fetchone()
                if row:
                    merged = _merge_live_transcript(str(row["text"]), fragment)[:8000]
                    db.execute(
                        "UPDATE live_messages SET text=?,updated_at=? WHERE story_id=? AND message_key=?",
                        (merged, now, session.resource_id, str(active["key"])),
                    )
                    return
            if isinstance(active, dict) and active.get("key"):
                db.execute(
                    "UPDATE live_messages SET final=1,updated_at=? WHERE story_id=? AND message_key=?",
                    (now, session.resource_id, str(active["key"])),
                )
            seq = int(session.state.get("live_message_seq") or 0) + 1
            key = f"{session.id}:{seq}:{role}"
            db.execute(
                "INSERT INTO live_messages(story_id,session_id,message_key,role,text,final,created_at,updated_at) "
                "VALUES(?,?,?,?,?,0,?,?)",
                (session.resource_id, session.id, key, role, fragment[:8000], now, now),
            )
            db.execute(
                "DELETE FROM live_messages WHERE story_id=? AND id NOT IN "
                "(SELECT id FROM live_messages WHERE story_id=? ORDER BY id DESC LIMIT 200)",
                (session.resource_id, session.resource_id),
            )
        session.state["live_message_seq"] = seq
        session.state["live_message"] = {"key": key, "role": role}

    def on_event(self, session, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "unknown")
        text = str(event.get("text") or "").strip()
        if kind == 'grounding':
            chunks = (event.get('metadata') or {}).get('groundingChunks') or []
            sources = [{'url': item['web']['uri'], 'title': item['web'].get('title', '')}
                       for item in chunks if isinstance(item, dict) and isinstance(item.get('web'), dict)
                       and isinstance(item['web'].get('uri'), str)]
            if sources:
                session.state['identity_article_sources'] = sources[:20]
        if kind == "tool_call":
            session.state["research_provider_tool_pending"] = True
            session.state["research_continuation_queued"] = False
        if kind == "output_transcript":
            session.state["research_continuation_queued"] = False
        if kind == "input_timing":
            observe_input_timing(session, event)
        if kind == "resource_budget" and event.get("modality") == "tool_response":
            session.state["research_admission"] = {
                key: event.get(key) for key in ("status", "code", "estimated_units", "requested_units", "granted_units")
            }
            # The shared transport owns its 95-second wait for this unsent reply.
            # A temporary denial must not cancel a run which can still be admitted.
            record_live_diagnostic(self.service, session.resource_id, session.id, "backend", "research_admission", session.state["research_admission"])
        if kind == "input_transcript" and text:
            suspected = observe_transcript(session, text)
            if suspected:
                receipt = noise_receipt(session)
                record_live_diagnostic(
                    self.service,
                    session.resource_id,
                    session.id,
                    "backend",
                    "suspected_noise_turn",
                    receipt,
                )
                logger.info(
                    "street_story_live_suspected_noise %s",
                    canonical({"story_id": session.resource_id, "session_id": session.id, **receipt}),
                )
            else:
                recent: deque[str] = session.state["recent_user"]
                if not recent or recent[-1] != text:
                    recent.append(text)
                self._persist_live_message(session, "user", text)
                literal = session.state.get("literal")
                if isinstance(literal, dict):
                    buf: list[str] = literal.setdefault("buffer", [])
                    if not buf or buf[-1] != text:
                        buf.append(text[:2000])
                        del buf[:-64]
        elif kind == "output_transcript" and text:
            recent_model: deque[str] = session.state["recent_model"]
            if not recent_model or recent_model[-1] != text:
                recent_model.append(text)
            self._persist_live_message(session, "assistant", text)

        if kind in {"error", "closed"}:
            self._pause_research(session, "live_" + kind + "_resume_required")
        if kind in {"turn_complete", "interrupted", "error", "closed"}:
            self._finalize_live_message(session)
        if kind == "turn_complete":
            self._continue_identity(session)
            self._continue_pending_research(session)

        if kind in {"input_transcript", "output_transcript"} and text:
            role = "user" if kind == "input_transcript" else "assistant"
            payload = {"role": role, "text": text[:8000], "provider_at": event.get("provider_at")}
            record_live_diagnostic(self.service, session.resource_id, session.id, "provider", kind, payload)
            logger.info(
                "street_story_live_transcript %s",
                canonical({"story_id": session.resource_id, "session_id": session.id, **payload}),
            )
        elif kind in {
            "input_timing", "tool_result", "tool_call", "turn_complete", "generation_complete",
            "interrupted", "error", "closed", "resource_budget", "resource_budget_wait",
            "resource_budget_ready", "input_dropped", "timing", "interaction_status",
        }:
            excluded = {"data", "metadata", "text", "args", "response"}
            payload = {key: value for key, value in event.items() if key not in excluded}
            record_live_diagnostic(self.service, session.resource_id, session.id, "provider", kind, payload)
            logger.info(
                "street_story_live_event %s",
                canonical({
                    "story_id": session.resource_id,
                    "session_id": session.id,
                    "type": kind,
                    **payload,
                }),
            )

    def _visual_snapshot(self, story_id: str) -> tuple[bytes, int, int] | None:
        with self.service.store.connection() as db:
            story = self.service._story_row(db, story_id)
            path = str(story["photo_path"] or "")
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.thumbnail((768, 768), Image.Resampling.LANCZOS)
                width, height = image.size
                for quality in (76, 68, 60, 52):
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=True)
                    data = buffer.getvalue()
                    if len(data) <= 220 * 1024:
                        return data, width, height
        except Exception as exc:
            logger.warning(
                "street_story_live_snapshot_prepare_failed %s",
                canonical({"story_id": story_id, "type": type(exc).__name__}),
            )
        return None

    def _send_visual_snapshot(self, session) -> None:
        snapshot = self._visual_snapshot(session.resource_id)
        if snapshot is None:
            self.emit(session, {"type": "visual_context", "status": "unavailable"})
            record_live_diagnostic(
                self.service, session.resource_id, session.id, "backend", "visual_context",
                {"status": "unavailable"},
            )
            return
        data, width, height = snapshot
        self.write(
            session,
            {
                "type": "snapshot",
                "data": base64.b64encode(data).decode("ascii"),
                "mime_type": "image/jpeg",
                "context": {"kind": "source_photo", "story_id": session.resource_id},
                "optional": True,
            },
        )
        payload = {"status": "ready", "width": width, "height": height, "jpeg_bytes": len(data)}
        self.emit(session, {"type": "visual_context", **payload})
        record_live_diagnostic(
            self.service, session.resource_id, session.id, "backend", "visual_context", payload
        )

    def on_started(self, session) -> None:
        record_live_diagnostic(
            self.service,
            session.resource_id,
            session.id,
            "backend",
            "voice_profile",
            {"voice": "Aoede", "model": str(session.model), "phase": "started"},
        )
        self._send_visual_snapshot(session)

    def on_resumed(self, session) -> None:
        record_live_diagnostic(
            self.service,
            session.resource_id,
            session.id,
            "backend",
            "voice_profile",
            {"voice": "Aoede", "model": str(session.model), "phase": "resumed"},
        )
        self._send_visual_snapshot(session)
        self._send_pending_comparison(session)
        self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(session.resource_id))})

    def _pause_research(self, session, reason):
        session.state["research_cancelled"] = True
        owned = set(session.state.get("research_run_ids") or [])
        if session.state.get("research_run_id"):
            owned.add(session.state["research_run_id"])
        if owned:
            with self.service.store.tx() as db:
                for run_id in owned:
                    db.execute("UPDATE research_runs SET state='partial',status_detail=?,updated_at=? "
                               "WHERE run_id=? AND story_id=? AND state NOT IN ('completed','cancelled','partial','failed')", (reason, self.service.store.now(), run_id, session.resource_id))

    def on_stopped(self, session) -> None:
        self._pause_research(session, "live_stopped_resume_required")

    @staticmethod
    def _live_research_progress(db, story_id, run_id):
        observations = db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=? AND run_id=? AND status='accepted'", (story_id, run_id)).fetchone()[0]
        sources = db.execute("SELECT source_version_id,status FROM research_run_sources WHERE run_id=?", (run_id,)).fetchall()
        return {'observations': observations,
                'attempts': sum(bool(row['source_version_id']) or row['status'] == 'failed' for row in sources),
                'remaining': sum(not row['source_version_id'] and row['status'] != 'failed' for row in sources)}

    def _continue_pending_research(self, session):
        run_id = str(session.state.get("research_run_id") or "")
        if not run_id or getattr(session, "closed", False) or getattr(session, "awaiting_audio", False) or any(session.state.get(key) for key in (
            "research_cancelled", "research_tool_busy", "research_provider_tool_pending",
            "research_author_interrupted", "research_continuation_queued",
        )):
            return
        with self.service.store.connection() as db:
            run = db.execute("SELECT state FROM research_runs WHERE run_id=? AND story_id=?", (run_id, session.resource_id)).fetchone()
            if not run or run["state"] in {"completed", "partial", "failed", "cancelled"}:
                return
            pending = db.execute("SELECT COUNT(*) FROM research_chunk_runs WHERE run_id=? AND status NOT IN ('extracted','no_claims')", (run_id,)).fetchone()[0]
            unfetched = db.execute("SELECT COUNT(*) FROM research_run_sources WHERE run_id=? AND source_version_id IS NULL", (run_id,)).fetchone()[0]
            saved_batches = db.execute(
                "SELECT COUNT(*) FROM research_chunk_batches WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            facts = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=?", (session.resource_id,)).fetchone()[0]
            recipe = session.state.get('research_chunk_receipts', {}).get(session.state.get('research_current_chunk_id')) or {}
            unread_save = bool(recipe.get('run_id') == run_id and recipe.get('batch_id') and not db.execute(
                "SELECT 1 FROM live_commands WHERE story_id=? AND command_id=? AND tool_name='save_research_facts'",
                (session.resource_id, recipe.get('batch_id'))).fetchone())
        if session.state.get('live_first_research'):
            with self.service.store.connection() as db:
                progress = self._live_research_progress(db, session.resource_id, run_id)
            if pending or (progress['remaining'] and progress['attempts'] < 3):
                attempts = int(session.state.get("research_continuation_count") or 0)
                if attempts < 12:
                    next_tool = (
                        "save_research_facts"
                        if pending and unread_save
                        else "get_research_chunk"
                    )
                    session.state["research_continuation_count"] = attempts + 1
                    session.state["research_continuation_queued"] = True
                    self.write(
                        session,
                        {
                            "type": "text",
                            "text": (
                                "Server context for the author's current research request: "
                                f"run {run_id} has unread evidence. Already saved supported findings remain usable. "
                                "Save sufficient NEW discovery snippets immediately with exact source_ref/evidence_ref and scoped review. Compare the known_fact_inventory; equivalent claims must reuse existing_fact_id and are not new findings. Never speak unsaved snippets as facts. Do not answer with remembered or "
                                "search-snippet facts. Continue "
                                f"{next_tool}. For get_research_chunk choose one competent "
                                "source by title/provenance and copy its exact source_ref from "
                                "the previous search result. For save_research_facts review "
                                "the evidence already read and preserve every qualifier. "
                                "After no_claims use get_research_chunk(run_id only) for the next source, at most three full-source attempts. "
                                "If this work adds no new supported claim, report that honestly. Distinguish previously saved facts and added evidence from new eligible claims."
                            ),
                        },
                    )
                    record_live_diagnostic(
                        self.service,
                        session.resource_id,
                        session.id,
                        "backend",
                        "research_continuation",
                        {
                            "run_id": run_id,
                            "next_tool": next_tool,
                            "attempt": attempts + 1,
                            "pending_chunks": pending,
                            "unfetched_sources": unfetched,
                            "saved_batches": saved_batches,
                            "reason": "unread_saved_source_work",
                            **progress,
                        },
                    )
                    logger.info('street_story_research_continue run=%s next=%s observations=%s sources_attempted=%s remaining=%s', run_id, next_tool, progress['observations'], progress['attempts'], progress['remaining'])
                    return
                self._pause_research(session, 'live_continuation_exhausted')
                self._emit_research_progress(session, stage='partial', active=False, query='', source_count=0, fact_count=facts)
                return
            # A finished answer may offer useful partial findings. Do not inject
            # another whole-inventory review request into the normal Live flow.
            # Pending provider calls are excluded above; the saved cursor remains
            # writable for an explicit continuation in this same conversation.
            with self.service.store.tx() as db:
                set_run_state(db, run_id, 'partial' if progress['observations'] or pending else 'completed', detail='live_answer_partial' if progress['observations'] else 'live_continuation_exhausted' if pending else 'live_no_new_confirmed_facts', now=self.service.store.now(), completed=not progress['observations'] and not pending)
            self._emit_research_progress(session, stage='partial' if progress['observations'] else 'completed', active=False, query='', source_count=0, fact_count=facts)
            return
        attempts = int(session.state.get("research_continuation_count") or 0)
        if attempts >= 2:
            self._pause_research(session, "model_review_continuation_exhausted")
            self._emit_research_progress(session, stage="partial", active=False, query="", source_count=0, fact_count=facts)
            return
        tool = "get_research_chunk" if pending or unfetched else "get_review_packet"
        session.state["research_continuation_count"] = attempts + 1
        session.state["research_continuation_queued"] = True
        # Scoped server context for the SAME authorized operation and lease.
        # No accepted tool is rerun; only the model chooses/executes the next step.
        self.write(session, {"type": "text", "text": "Server context for the author's already authorized research operation: run " + run_id + " is still incomplete, with " + str(facts) + " durable facts. Continue " + tool + " with run_id only and follow returned next_args/pages, then explicit semantic review. Preserve checkpoints; this context grants no new author consent or publication permission. Do not announce completion while the run is incomplete."})
        record_live_diagnostic(self.service, session.resource_id, session.id, "backend", "research_continuation", {"run_id": run_id, "next_tool": tool, "attempt": attempts + 1, "pending_chunks": pending})

    async def execute_tool(self, session, call: dict[str, Any]) -> dict[str, Any]:
        session.state["research_tool_busy"] = True
        name = call.get("name")
        if name in {"search_web", "get_research_chunk"}:
            session.state["research_output_pending"] = True
        try:
            result = await self._execute_tool(session, call)
            if name == "save_research_facts" and any(
                fact.get("evidence_supported") or fact.get("verdict") == "supported"
                for fact in result.get("facts", [])
            ):
                session.state["research_output_pending"] = False
            elif name == "get_research_chunk" and result.get("all_chunks_processed") and result.get("next_tool") is None:
                session.state["research_output_pending"] = False
            if session.state.get("research_run_id") and name in {"select_facts", "set_concept", "edit_text", "generate_visual", "prepare_publication"}:
                # A successful owner editing action ends research input focus.
                # Keep pending documents/facts durable for an explicit resume.
                session.state["research_author_interrupted"] = True
                session.state["research_output_pending"] = False
                self._pause_research(session, "live_owner_switched_to_editing")
                topic = self._topic_state(session.resource_id)
                completed = (topic.get("research_run") or {}).get("state") == "completed"
                self._emit_research_progress(session, stage="completed" if completed else "partial", active=False,
                    query="", source_count=topic["story"].get("source_count", 0), fact_count=len(topic["story"].get("facts", [])))
                record_live_diagnostic(self.service, session.resource_id, getattr(session, "id", ""), "backend", "research_owner_editing", {
                    "run_id": session.state["research_run_id"], "tool": name, "research_input_active": False,
                })
            return result
        except ConflictError as exc:
            record_live_diagnostic(self.service, session.resource_id, getattr(session, "id", ""), "backend", "live_tool_rejected", {
                "tool": name, "code": exc.code, "run_id": session.state.get("research_run_id"),
            })
            if call.get("name") in {"get_review_packet", "finalize_fact_review"} and exc.code in {
                "live_review_decisions_invalid", "live_review_canonical_invalid", "live_fact_review_evidence_invalid", "live_review_packet_unknown",
            }:
                attempts = int(session.state.get("invalid_review_attempts") or 0) + 1
                session.state["invalid_review_attempts"] = attempts
                if attempts >= 3:
                    self._pause_research(session, "invalid_review_contract_budget_exhausted")
                    self._emit_research_progress(session, stage="partial", active=False, query="", source_count=0, fact_count=0)
                    raise ConflictError("live_research_partial", "Review paused after three invalid references/verdicts. Saved facts and packet decisions remain; resume the same run in a new Live session.") from exc
            raise
        finally:
            session.state["research_tool_busy"] = False
            session.state["research_provider_tool_pending"] = False

    async def _execute_tool(self, session, call: dict[str, Any]) -> dict[str, Any]:
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        command_id = str(call.get("id") or "")
        story_id = session.resource_id

        if name == 'find_place_articles':
            return await self._find_place_articles(session, args)

        if name == "read_topic":
            result = self._topic_state(story_id)
            compact = self._compact_context(result)
            self.emit(session, {"type": "product_state", "state": compact})
            return await self._next_visual_result(session, compact)
        if name == "get_facts":
            run_id = session.state.get('research_run_id')
            if session.state.get('live_first_research') and run_id:
                with self.service.store.connection() as db:
                    run = db.execute("SELECT state FROM research_runs WHERE run_id=? AND story_id=?", (run_id, story_id)).fetchone()
                    progress = self._live_research_progress(db, story_id, run_id)
                if run and run['state'] not in {'completed', 'failed', 'cancelled'} and not progress['observations'] and progress['remaining'] and progress['attempts'] < 3:
                    raise ConflictError('live_research_more_sources_required', 'Zero new durable observations. Next action: get_research_chunk with run_id only; save sufficient snippets before a factual answer.')
            return bounded_inventory(lambda page: self._get_facts(story_id, page), name, args, "facts")
        if name == "get_review_packet":
            packet = review_packets.read(self, session, args)
            session.state["research_run_id"] = packet["run_id"]
            session.state.setdefault("research_run_ids", []).append(packet["run_id"])
            self._emit_research_progress(session, stage="review" if packet.get("review_available", True) else "extracting", active=True, query="", source_count=0, fact_count=packet.get("total_facts", 0))
            return packet
        if name == 'assess_review_packet':
            return await research_repairs.assess(self, session, args)
        if name == "get_review_context":
            return research_repairs.context(self, session, args)
        if name == "get_evidence":
            # Live reads are paginated by the existing cursor contract so one
            # verbose evidence reply cannot exceed the shared token budget.
            try:
                page_limit = max(1, min(int(args.get("limit") or 5), 5))
            except (TypeError, ValueError):
                raise ConflictError("live_pagination_invalid", "limit must be an integer") from None
            return bounded_inventory(lambda page: self._get_evidence(story_id, page), name, {**args, "limit": page_limit}, "evidence")
        if name == "get_research_chunk":
            return self._model_result(name, await self._get_research_chunk(session, args))
        if name == "literal_begin":
            return self._literal_begin(session, args)
        if name == "literal_cancel":
            session.state["literal"] = None
            self.emit(session, {"type": "literal_mode", "active": False, "cancelled": True})
            return {"ok": True, "literal_mode": False}

        if suspected_noise_turn(session):
            receipt = noise_receipt(session)
            record_live_diagnostic(
                self.service,
                story_id,
                session.id,
                "backend",
                "suspected_noise_tool_blocked",
                {"tool": name[:80], **receipt},
            )
            return {
                "ignored": True,
                "reason": "suspected_noise_turn",
                "instruction": "Do not mutate product state or answer this fragment. Wait for the author's next clear utterance.",
            }

        if name == "repair_research_fact":
            result = research_repairs.repair(self, session, command_id, args)
            self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(story_id))})
            repairs = result.get('repairs', [result])
            logger.info("street_story_research_repair story=%s run=%s parents=%s children=%s policy=%s", story_id, session.state.get("research_run_id"), len(repairs), sum(len(r['fact_ids']) for r in repairs), review_packets.POLICY_VERSION)
            return result

        if not command_id:
            raise ConflictError("live_command_id_required", "Provider call id is required for mutations")

        # literal_finish binds idempotency to the captured transcript assembled
        # server-side, not to the provider's empty function arguments.
        if name != "literal_finish":
            replay = self._command_replay(story_id, command_id, name, args)
            if replay is not None:
                return self._model_result(name, replay)

        if name == "compare_place_images":
            result = await self._compare_place_images(session, args)
        elif name == "record_place_comparison":
            result = self._record_place_comparison(session, command_id, args)
        elif name == "resolve_place":
            result = await self._resolve_place(session, command_id, args)
        elif name == "reject_place":
            result = await self._reject_place(session, command_id, args)
        elif name == "confirm_place":
            result = await self._confirm_place(session, command_id, args)
        elif name == "search_web":
            result = await self._search_web(session, command_id, args)
        elif name == "save_research_facts":
            try:
                result = await self._save_research_facts(session, command_id, args)
            except ConflictError as exc:
                invalid_codes = {"live_research_evidence_unknown", "live_research_passage_unknown", "live_research_quote_invalid", "live_research_quotes_required"}
                if exc.code in invalid_codes:
                    run_id = str(args.get("run_id") or session.state.get("research_run_id") or "")
                    failures = session.state.setdefault("research_invalid_batches", {})
                    failures[run_id] = failures.get(run_id, 0) + 1
                    if failures[run_id] >= 3:
                        with self.service.store.tx() as db:
                            self._research_run_guard(db, session, run_id)
                            set_run_state(db, run_id, "partial", detail="invalid_batch_budget_exhausted", now=self.service.store.now(), completed=False)
                            saved_count = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=?", (story_id,)).fetchone()[0]
                            source_count = db.execute("SELECT COUNT(*) FROM research_run_sources WHERE run_id=?", (run_id,)).fetchone()[0]
                        session.state["research_cancelled"] = True
                        self._emit_research_progress(session, stage="partial", active=False, query="", source_count=source_count, fact_count=saved_count)
                        record_live_diagnostic(self.service, story_id, session.id, "backend", "research_partial", {"run_id": run_id, "reason": "invalid_batch_budget_exhausted", "attempts": failures[run_id]})
                        raise ConflictError("live_research_partial", "Research paused after three invalid evidence batches. Saved findings remain durable. Resume this run in a new Live session using numeric passage_ids.") from None
                raise
            session.state.setdefault("research_invalid_batches", {}).pop(str(result.get("research_run_id") or ""), None)
            session.state["research_discovery_without_save"] = 0
        elif name == "record_fact_conflicts":
            result = self._record_fact_conflicts(session, command_id, args)
        elif name == "finalize_fact_review":
            if args.get("packet_ref"):
                staged, expanded, rejected = review_packets.prepare(self, session, args)
                result = staged if staged is not None else self._finalize_fact_review(session, command_id, expanded, packet_rejected=rejected)
            else:
                if args.get("decisions") is not None:
                    raise ConflictError("live_review_packet_required", "Call get_review_packet with run_id ONLY. Then copy its packet_ref and exact zero-based fact/evidence numbers. A batch_id is not a packet_ref.")
                result = self._finalize_fact_review(session, command_id, args)
        elif name == "resolve_fact_conflict":
            result = self._resolve_fact_conflict(session, command_id, args)
        elif name == "select_facts":
            result = self._select_facts(story_id, command_id, args)
        elif name == "set_concept":
            result = self._set_concept(story_id, command_id, args)
        elif name == "edit_text":
            result = self._edit_text(story_id, command_id, args)
        elif name == "literal_finish":
            result = self._literal_finish(session, command_id)
        elif name == "undo":
            result = self._undo(story_id, command_id)
        elif name == "generate_visual":
            result = self._generate_visual(story_id, command_id, args)
        elif name == "prepare_publication":
            result = await self._prepare_publication(story_id, command_id, args)
            self.emit(session, {"type": "publication_confirmation", **result["confirmation"]})
        elif name == "confirm_publication":
            result = self._confirm_publication(story_id, command_id, args)
        elif name == "cancel_publication":
            result = self._cancel_publication(story_id, command_id)
        else:
            raise ConflictError("live_tool_unknown", f"Unknown Live tool: {name}")

        if name != "literal_finish":
            self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(story_id))})
        if name == 'compare_place_images' and result.get('comparison_id'):
            return result
        projected = self._model_result(name, result)
        if name == 'record_place_comparison' and not result.get('matched'):
            return await self._next_visual_result(session, projected)
        if name == "save_research_facts" and response_units(name, projected, command_id) > PAGE_UNITS:
            projected = {key: projected.get(key) for key in ("research_run_id", "payload_saved", "chunk_id", "save_batch_id", "review_required", "continuation_required", "next_tool")}
            projected.update({"facts_page_required": True, "read_tool": "get_facts", "saved_fact_count": len(result.get("facts") or [])})
        if name in {"save_research_facts", "finalize_fact_review"}:
            record_live_diagnostic(self.service, story_id, session.id, "backend", "research_reply_size", {"tool": name, "estimated_envelope_units": response_units(name, projected, command_id), "page_ceiling": PAGE_UNITS})
        return projected

    @staticmethod
    def _compact_identity(identity: Any) -> dict[str, Any] | None:
        if not isinstance(identity, dict) or not identity:
            return None
        candidates = [item for item in identity.get("candidates", []) if isinstance(item, dict)]
        chosen = str(identity.get("candidate_id") or "")
        # The selected object must survive compaction even when it was last in OSM/Wikipedia.
        candidates.sort(key=lambda item: str(item.get("candidate_id") or "") != chosen)
        selected = next((item for item in candidates if str(item.get('candidate_id') or '') == chosen), {})
        return {
            key: identity.get(key)
            for key in ("status", "candidate_id", "candidate_name", "canonical_name", "aliases", "locality", "country", "confidence", "candidate_url", "photo_sha256", "generation")
        } | {
            "canonical_name": identity.get('canonical_name') or identity.get('candidate_name') or selected.get('name'),
            "aliases": identity.get('aliases') or selected.get('entity_aliases') or [],
            "observations": [str(value)[:300] for value in identity.get("observations", [])[:3]],
            "candidate_count": len(candidates),
            "candidates": [
                {key: item.get(key) for key in ("candidate_id", "name", "type", "url")}
                for item in candidates[:8]
            ],
        }

    @classmethod
    def _model_result(cls, name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Project only the model reply; keep durable results and the Android API complete.

        Sending the full story after every tool duplicates research, candidates, facts,
        sources and visual prompts. Those bytes are charged again to the shared Live
        budget. Publication confirmation, revisions and literal spans remain exact.
        Replay passes through the same projection without repeating an external effect.
        """
        projected = dict(result)
        story = result.get("story")
        if isinstance(story, dict):
            keys = ("id", "state", "revision", "place_name", "source_count", "error")
            projected["story"] = {key: story[key] for key in keys if key in story}
            if name not in {"search_web", "save_research_facts"} and "draft_text" not in result:
                projected["story"]["draft_text"] = str(story.get("draft_text") or "")[:5000]
        if name == "search_web":
            discovery_only = bool(result.get("discovery_only"))
            compact_sources = []
            for source in result.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                supports = [
                    str(support.get("text") or "")[:360]
                    for support in (source.get("supports") or [])
                    if isinstance(support, dict) and str(support.get("text") or "").strip()
                ]
                if discovery_only:
                    compact_sources.append(
                        {
                            "source_ref": source.get("source_ref"),
                            "url": str(source.get("url") or "")[:1000],
                            "title": str(source.get("title") or "")[:140],
                            "supports": supports[:2],
                        }
                    )
                else:
                    compact_sources.append(
                        {
                            "source_ref": source.get("source_ref"),
                            "type": str(source.get("type") or "web"),
                            "title": str(source.get("title") or "")[:140],
                            "url": str(source.get("url") or "")[:400],
                        }
                    )
            compact_facts = []
            for fact in result.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                compact_facts.append(
                    {
                        "fact_id": fact.get("fact_id"),
                        "claim_key": fact.get("claim_key"),
                        "text": str(fact.get("text") or "")[:280],
                        "confidence": fact.get("confidence"),
                        "selected": bool(fact.get("selected")),
                        "evidence_supported": bool(fact.get("evidence_supported")),
                        "source_count": len(
                            [source for source in (fact.get("sources") or []) if isinstance(source, dict)]
                        ),
                    }
                )
            explicit_next_tool = result.get("next_tool")
            projected = {
                "query": str(result.get("query") or "")[:400],
                "coverage_goal": result.get("coverage_goal"),
                "sources_skipped_completed": result.get("sources_skipped_completed", 0),
                "research_run_id": result.get("research_run_id"),
                "save_batch_id": result.get("save_batch_id"),
                "summary": str(result.get("summary") or "")[:480],
                "search_provider": result.get("search_provider"),
                "discovery_only": discovery_only,
                "semantic_completion": result.get("semantic_completion"),
                "semantic_status": result.get("semantic_status"),
                "coverage_satisfied": bool(result.get("coverage_satisfied")),
                "missing_aspects": list(result.get("missing_aspects") or [])[:20],
                "extraction_complete": result.get("extraction_complete"),
                "continuation_reason": result.get("continuation_reason"),
                "review_required": bool(result.get("review_required")),
                "completed": bool(result.get('completed')),
                "continuation_required": bool(
                    explicit_next_tool
                    or (
                        discovery_only
                        and result.get("semantic_status") == "live_model_required"
                    )
                    or (
                        not discovery_only
                        and result.get("review_required")
                    )
                ),
                "next_tool": (
                    explicit_next_tool
                    or (
                        "save_research_facts"
                        if discovery_only
                        and result.get("semantic_status") == "live_model_required"
                        else (
                            "finalize_fact_review"
                            if not discovery_only and result.get("review_required")
                            else None
                        )
                    )
                ),
                "next_args": result.get("next_args"),
                "instruction": result.get("instruction"),
                "extraction_audit": result.get("extraction_audit"),
                "source_count": len([source for source in (result.get("sources") or []) if isinstance(source, dict)]),
                "facts": compact_facts[:32],
                "sources": compact_sources[:12],
                "fact_conflicts": list(result.get("fact_conflicts") or [])[:6],
                "story": projected.get("story"),
            }
            if result.get("instruction"):
                projected.update({"instruction": result["instruction"], "next_args": result.get("next_args")})
        elif name == "get_research_chunk":
            # Passages already contain every core character needed for extraction.
            # Avoid charging the shared Live budget for three copies of the page.
            projected.pop("core_text", None)
            projected.pop("context_text", None)
            manifest = projected.get("research_manifest")
            if isinstance(manifest, dict):
                projected["research_manifest"] = {"counts": manifest.get("counts"), "state": (manifest.get("run") or {}).get("state")}
            checkpoint = projected.get("checkpoint")
            if isinstance(checkpoint, dict):
                projected["checkpoint"] = {**checkpoint, "facts": [
                    {"claim_key": f.get("claim_key"), "text": str(f.get("text") or "")[:600], "existing_fact_id": f.get("existing_fact_id")}
                    for f in checkpoint.get("facts", []) if isinstance(f, dict)
                ]}
        elif name == "save_research_facts":
            projected = {
                "research_run_id": result.get("research_run_id"),
                "payload_saved": bool(result.get("payload_saved")),
                "save_research_audit": result.get("save_research_audit"),
                "chunk_id": result.get("chunk_id"),
                "save_batch_id": result.get("save_batch_id"),
                "review_required": bool(result.get("review_required")),
                "completed": bool(result.get('completed')),
                "continuation_required": bool(result.get("continuation_required")),
                "next_tool": result.get("next_tool"),
                "next_args": result.get("next_args"),
                "facts": [
                    {
                        "fact_id": fact.get("fact_id"),
                        "revision_digest": fact.get("revision_digest"),
                        "supporting_evidence_ids": list(fact.get("supporting_evidence_ids") or []),
                        "text": str(fact.get("text") or "")[:280],
                        "selected": bool(fact.get("selected")),
                        "source_count": len(
                            [source for source in (fact.get("sources") or []) if isinstance(source, dict)]
                        ),
                        "sources": [
                            {
                                "type": str(source.get("type") or "web"),
                                "title": str(source.get("title") or "")[:120],
                                "url": str(source.get("url") or "")[:360],
                            }
                            for source in (fact.get("sources") or [])[:6]
                            if isinstance(source, dict)
                        ],
                    }
                    for fact in (result.get("facts") or [])[:32]
                    if isinstance(fact, dict)
                ],
                "selected_fact_ids": list(result.get("selected_fact_ids") or [])[:80],
                "story": projected.get("story"),
            }
            if result.get('review_required') is False:
                projected['facts'] = [{'fact_id': f.get('fact_id'), 'text': f.get('text'),
                                       'verdict': (f.get('live_review') or {}).get('verdict')} for f in result.get('facts', [])]
            if result.get('next_tool') == 'get_research_chunk':
                projected['next_args'] = {'run_id': result.get('research_run_id')}
                projected['instruction'] = 'Continue get_research_chunk with run_id ONLY. The server returns the next unread page and skips completed cores. Saved supported findings are already available.'
        if name == "search_web" and result.get("discovery_only") is True:
            compact_sources = []
            for source in (result.get("sources") or [])[:20]:
                if not isinstance(source, dict):
                    continue
                source_ref = str(source.get("source_ref") or "").strip()
                if not source_ref:
                    continue
                evidence = []
                seen_evidence: set[str] = set()
                for support in (source.get("supports") or [])[:6]:
                    if not isinstance(support, dict):
                        continue
                    snippet = str(support.get("text") or "").strip()
                    evidence_ref = str(support.get("evidence_ref") or "").strip()
                    if (
                        snippet
                        and evidence_ref
                        and evidence_ref not in seen_evidence
                    ):
                        evidence.append({
                            "evidence_ref": evidence_ref,
                            "text": snippet[:360],
                        })
                        seen_evidence.add(evidence_ref)
                compact_sources.append({
                    "source_ref": source_ref,
                    "url": str(source.get('url') or ''),
                    "title": str(source.get('title') or '')[:100],
                    "evidence": evidence,
                })
            projected["sources"] = compact_sources
            # A search-provider summary is unsaved prose, not a product finding.
            # Keep the addressable evidence for the model's semantic work, but do
            # not present a ready-made answer beside the required save action.
            projected.pop("summary", None)
            projected["instruction"] = (
                "RESEARCH IN PROGRESS, NOT READY FOR A FACTUAL ANSWER. Your next response must be a function call: "
                "save_research_facts using next_args and the exact snippet refs if sufficient, otherwise get_research_chunk. "
                "Do not speak factual findings before a successful save receipt. "
                + str(result.get("instruction") or "")
            )
            projected.pop("fact_conflicts", None)
            # Keep every source addressable. Omit whole snippets, never truncate
            # qualifiers, when their model envelope exceeds the page budget.
            omitted = 0
            for source in reversed(projected["sources"]):
                while source["evidence"] and response_units(name, projected) > PAGE_UNITS - 240:
                    source["evidence"].pop()
                    omitted += 1
            if omitted:
                projected["snippet_budget_omitted"] = omitted
                projected["instruction"] += " Sources without snippets remain available: choose a competent source_ref and read its full document."
        elif name == "search_web" and result.get("semantic_completion"):
            projected["sources"] = []
            projected.pop("fact_conflicts", None)
        elif name == "save_research_facts" and result.get('review_required') is not False:
            projected["facts"] = [
                {
                    "fact_id": str(fact.get("fact_id") or ""),
                    "text": str(fact.get("text") or "")[:280],
                    "revision_digest": fact.get("revision_digest"),
                    "supporting_evidence_ids": list(fact.get("supporting_evidence_ids") or []),
                    "source_count": len(fact.get("sources") or []),
                }
                for fact in (result.get("facts") or [])[:32]
                if isinstance(fact, dict)
            ]
            projected["save_research_audit"] = result.get(
                "save_research_audit"
            )
            projected["fact_reconciliation"] = result.get(
                "fact_reconciliation"
            )
        elif name == "finalize_fact_review":
            projected = {
                "research_run_id": result.get("research_run_id"),
                "complete": bool(result.get("complete")),
                "coverage_complete": bool(result.get("coverage_complete")),
                "missing_aspects": list(result.get("missing_aspects") or [])[:40],
                "reviewed_assertion_count": int(result.get("reviewed_assertion_count") or 0),
                "eligible_count": int(result.get("eligible_count") or 0),
                "withheld_count": int(result.get("withheld_count") or 0),
                "unreviewed_count": int(result.get("unreviewed_count") or 0),
                "conflict_ids": list(result.get("conflict_ids") or [])[:40],
                **{key: result[key] for key in ("packet_ref", "review_saved", "remaining_facts", "cross_packet_review_required") if key in result},
            }
        if "visual_identity" in result:
            projected["visual_identity"] = cls._compact_identity(result["visual_identity"])
        logging.getLogger("uvicorn.error").info(
            "street_story_live_tool_reply name=%s full_chars=%d model_chars=%d",
            name, len(canonical(result)), len(canonical(projected)),
        )
        return projected

    def _editor_row(self, db, story_id: str):
        story = self.service._story_row(db, story_id)
        now = self.service.store.now()
        db.execute(
            "INSERT OR IGNORE INTO live_editor_state(story_id,text_revision,literal_json,history_json,updated_at) VALUES(?,0,'[]','[]',?)",
            (story_id, now),
        )
        row = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
        return story, row

    def _topic_state(self, story_id: str) -> dict[str, Any]:
        with self.service.store.tx() as db:
            row = self.service._story_row(db, story_id)
            self.service._hydrate_poi_memory(db, row)
            row = self.service._story_row(db, story_id)
            story = self.service._story_repr(db, row)
            story['latitude'], story['longitude'] = row['latitude'], row['longitude']
            editor = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
            if editor:
                editor_state = {
                    "text_revision": int(editor["text_revision"]),
                    "literal_spans": json.loads(editor["literal_json"] or "[]"),
                    "last_change": editor["last_change"],
                }
            else:
                editor_state = {"text_revision": 0, "literal_spans": [], "last_change": None}
            jobs = [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "state": item["state"],
                    "last_error": self.service.settings.redact(str(item["last_error"] or "")) or None,
                }
                for item in db.execute(
                    "SELECT id,kind,state,last_error FROM jobs WHERE story_id=? ORDER BY created_at DESC LIMIT 8",
                    (story_id,),
                )
            ]
            fact_conflict_state = conflict_rows(db, story_id, limit=20)
            latest_run = db.execute("SELECT run_id,state,goal,status_detail,identity_generation FROM research_runs WHERE story_id=? ORDER BY created_at DESC LIMIT 1", (story_id,)).fetchone()
            confirmation = db.execute(
                "SELECT * FROM live_publication_confirmations WHERE story_id=? ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
            latest_confirmation = None
            if confirmation:
                latest_confirmation = {
                    "confirmation_id": confirmation["id"],
                    "text_revision": confirmation["text_revision"],
                    "destinations": json.loads(confirmation["destinations_json"]),
                    "scheduled_for": confirmation["scheduled_for"],
                    "timezone": confirmation["timezone"],
                    "state": confirmation["state"],
                }
        return {
            "story": story,
            "editor": editor_state,
            "jobs": jobs,
            "confirmation": latest_confirmation,
            "fact_conflicts": fact_conflict_state,
            "research_run": dict(latest_run) if latest_run else None,
        }

    def _get_facts(self, story_id: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            cursor = max(0, int(args.get("cursor") or 0))
            limit = max(1, min(int(args.get("limit") or 30), 50))
        except (TypeError, ValueError):
            raise ConflictError("live_pagination_invalid", "cursor and limit must be integers") from None
        selected_only = bool(args.get("selected_only"))
        eligibility = str(args.get("eligibility") or "all").strip()
        if eligibility not in {"all", "unreviewed", "eligible", "withheld"}:
            raise ConflictError("live_fact_filter_invalid", "Unsupported eligibility filter")

        where = ["f.story_id=?", "f.rowid>?"]
        params: list[Any] = [story_id, cursor]
        if selected_only:
            where.append("a.owner_selected=1")
        if eligibility != "all":
            where.append("a.eligibility=?")
            params.append(eligibility)

        sql = (
            "SELECT f.rowid AS cursor_value,f.fact_id,f.text,f.confidence,f.evidence_supported,"
            "a.owner_selected,a.review_status,a.eligibility,a.revision_digest,"
            "(SELECT COUNT(*) FROM fact_observations o "
            " WHERE o.story_id=f.story_id AND o.assertion_id=f.fact_id) AS observation_count,"
            "(SELECT COUNT(*) FROM fact_evidence_spans e JOIN fact_observations o2 "
            " ON o2.observation_id=e.observation_id "
            " WHERE o2.story_id=f.story_id AND o2.assertion_id=f.fact_id) AS evidence_span_count "
            "FROM facts f JOIN fact_assertions a "
            "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
            "WHERE " + " AND ".join(where) + " ORDER BY f.rowid LIMIT ?"
        )
        with self.service.store.tx() as db:
            self.service._hydrate_poi_memory(db, self.service._story_row(db, story_id))
            rows = list(db.execute(sql, (*params, limit + 1)))
        page = rows[:limit]
        has_more = len(rows) > limit
        return {
            "facts": [
                {
                    "fact_id": str(row["fact_id"]),
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "owner_selected": bool(row["owner_selected"]),
                    "review_status": str(row["review_status"]),
                    "eligibility": str(row["eligibility"]),
                    "revision_digest": str(row["revision_digest"] or ""),
                    "observation_count": int(row["observation_count"] or 0),
                    "evidence_span_count": int(row["evidence_span_count"] or 0),
                }
                for row in page
            ],
            "next_cursor": int(page[-1]["cursor_value"]) if has_more and page else None,
            "has_more": has_more,
        }

    def _get_evidence(self, story_id: str, args: dict[str, Any]) -> dict[str, Any]:
        raw_ids = args.get("fact_ids")
        if not isinstance(raw_ids, list):
            raise ConflictError("live_fact_ids_required", "fact_ids must be an array")
        fact_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
        if not fact_ids or len(fact_ids) > 20:
            raise ConflictError("live_fact_ids_invalid", "Provide between 1 and 20 fact IDs")
        try:
            cursor = max(0, int(args.get("cursor") or 0))
            limit = max(1, min(int(args.get("limit") or 30), 50))
        except (TypeError, ValueError):
            raise ConflictError("live_pagination_invalid", "cursor and limit must be integers") from None

        placeholders = ",".join("?" for _ in fact_ids)
        sql = (
            "SELECT e.rowid AS cursor_value,o.assertion_id AS fact_id,o.observation_id,o.run_id,o.batch_id,"
            "o.model_name,o.prompt_version,o.created_at AS observation_created_at,"
            "e.evidence_id,e.source_url,e.source_version_id,e.support_kind,e.span_text,e.span_sha256,"
            "e.chunk_id,e.span_start,e.span_end,e.relation,e.created_at AS evidence_created_at,"
            "sv.final_url,sv.content_sha256,sv.read_status,sv.char_count "
            "FROM fact_evidence_spans e "
            "JOIN fact_observations o ON o.observation_id=e.observation_id "
            "LEFT JOIN source_versions sv ON sv.source_version_id=e.source_version_id "
            f"WHERE o.story_id=? AND o.assertion_id IN ({placeholders}) AND e.rowid>? "
            "ORDER BY e.rowid LIMIT ?"
        )
        with self.service.store.connection() as db:
            known = {
                str(row["assertion_id"])
                for row in db.execute(
                    f"SELECT assertion_id FROM fact_assertions WHERE story_id=? AND assertion_id IN ({placeholders})",
                    (story_id, *fact_ids),
                )
            }
            unknown = [fact_id for fact_id in fact_ids if fact_id not in known]
            if unknown:
                raise ConflictError(
                    "live_fact_id_unknown",
                    "Unknown fact IDs: " + ", ".join(unknown[:5]),
                )
            rows = list(db.execute(sql, (story_id, *fact_ids, cursor, limit + 1)))

        page = rows[:limit]
        has_more = len(rows) > limit
        return {
            "evidence": [
                {
                    "fact_id": str(row["fact_id"]),
                    "observation_id": str(row["observation_id"]),
                    "research_run_id": str(row["run_id"]),
                    "batch_id": str(row["batch_id"]),
                    "model_name": str(row["model_name"]),
                    "prompt_version": str(row["prompt_version"]),
                    "evidence_id": str(row["evidence_id"]),
                    "source_url": str(row["source_url"]),
                    "source_version_id": str(row["source_version_id"]),
                    "support_kind": str(row["support_kind"]),
                    "span_text": str(row["span_text"]),
                    "span_sha256": str(row["span_sha256"]),
                    "chunk_id": str(row["chunk_id"] or "") or None,
                    "span_start": row["span_start"],
                    "span_end": row["span_end"],
                    "relation": str(row["relation"]),
                    "source_version": (
                        {
                            "final_url": str(row["final_url"]),
                            "content_sha256": str(row["content_sha256"]),
                            "read_status": str(row["read_status"]),
                            "char_count": int(row["char_count"]),
                        }
                        if row["final_url"] is not None
                        else None
                    ),
                }
                for row in page
            ],
            "next_cursor": int(page[-1]["cursor_value"]) if has_more and page else None,
            "has_more": has_more,
        }

    @staticmethod
    def _compact_context(state: dict[str, Any], *, fact_preview_limit: int = 48) -> dict[str, Any]:
        story = state["story"]
        inventory = list(story.get("facts", []))
        if fact_preview_limit < 48:
            inventory.sort(key=lambda item: not bool(item.get("selected")))
        facts = [
            {
                "fact_id": item.get("fact_id"),
                "text": str(item.get("text") or "")[:280],
                "selected": bool(item.get("selected")),
                "has_attached_evidence": bool(item.get("evidence_supported")),
                "eligibility": item.get("eligibility", "unreviewed"),
            }
            for item in inventory[:fact_preview_limit]
        ]
        visual = story.get("visual") if isinstance(story.get("visual"), dict) else {}
        identity = story.get("visual_identity") if isinstance(story.get("visual_identity"), dict) else {}
        compact_identity = StreetStoryLiveAdapter._compact_identity(identity)
        return {
            "story_id": story.get("id"),
            "state": story.get("state"),
            "revision": story.get("revision"),
            "place_name": story.get("place_name"),
            "poi_location": {"latitude": story.get("latitude", story.get("lat")), "longitude": story.get("longitude", story.get("lon"))},
            "draft_text": str(story.get("draft_text") or "")[:5000],
            "text_revision": state["editor"].get("text_revision"),
            "literal_spans": state["editor"].get("literal_spans", []),
            "last_change": state["editor"].get("last_change"),
            "visual_identity": compact_identity,
            "identity_progress": {key: value for key, value in (story.get('identity_progress') or {}).items()
                if key in {'generation', 'updated_at', 'steps', 'attempt', 'finished', 'elapsed_ms',
                           'images_reviewed_count', 'visual_comparison_verified'}},
            "facts": facts,
            "fact_count": len(inventory),
            "facts_preview_truncated": len(inventory) > fact_preview_limit,
            "facts_read_tool": "get_facts",
            "selected_fact_ids": [item.get("fact_id") for item in inventory if item.get("selected")],
            "fact_conflicts": [
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "conflict_id", "left_fact_id", "right_fact_id", "left_text", "right_text",
                            "relation", "detector_confidence", "suggested_resolution", "suggested_fact_id",
                            "detector_rationale", "final_resolution", "final_fact_id",
                            "arbitration_reason", "arbitration_confidence", "arbitrated_by", "times_seen",
                        )
                    },
                    "evidence": {
                        side: {
                            "source_count": (item.get("evidence") or {}).get(side, {}).get("source_count", 0),
                            "domain_count": (item.get("evidence") or {}).get(side, {}).get("domain_count", 0),
                            "official": bool((item.get("evidence") or {}).get(side, {}).get("official")),
                            "source_urls": list((item.get("evidence") or {}).get(side, {}).get("source_urls", []))[:3],
                            "supports": list((item.get("evidence") or {}).get(side, {}).get("supports", []))[:2],
                        }
                        for side in ("left", "right")
                    },
                }
                for item in state.get("fact_conflicts", [])[:12]
            ],
            "source_count": story.get("source_count", 0),
            "research_run": state.get("research_run"),
            "publication_concept": story.get("publication_concept"),
            "publication": story.get("publication"),
            "visual": {
                "content_revision": visual.get("content_revision"),
                "stale": visual.get("stale", False),
                "processed_image_url": story.get("processed_image_url"),
            },
            "scheduled_for": story.get("scheduled_for"),
            "jobs": state.get("jobs", []),
            "confirmation": state.get("confirmation"),
        }

    def _command_replay(
        self, story_id: str, command_id: str, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_digest = digest({"tool": tool_name, "args": args})
        with self.service.store.connection() as db:
            row = db.execute(
                "SELECT tool_name,request_digest,result_json FROM live_commands WHERE story_id=? AND command_id=?",
                (story_id, command_id),
            ).fetchone()
        if not row:
            return None
        if row["tool_name"] != tool_name or row["request_digest"] != request_digest:
            raise ConflictError("live_command_conflict", "Provider call id is bound to different arguments")
        return json.loads(row["result_json"])

    def _store_command(
        self,
        db,
        story_id: str,
        command_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO live_commands(story_id,command_id,tool_name,request_digest,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                story_id,
                command_id,
                tool_name,
                digest({"tool": tool_name, "args": args}),
                canonical(result),
                self.service.store.now(),
            ),
        )

    def _recent_transcript(self, session, owner_context: str) -> str:
        with self.service.store.connection() as db:
            story = self.service._story_row(db, session.resource_id)
            prior = json.loads(story["research_json"] or "{}")
        parts: list[str] = []
        prior_text = str(prior.get("transcript") or "").strip()
        if prior_text:
            parts.append(prior_text)
        recent = " ".join(str(v).strip() for v in session.state["recent_user"] if str(v).strip())
        if recent and recent not in parts:
            parts.append(recent)
        if owner_context and owner_context not in parts:
            parts.append(owner_context)
        return "\n\n".join(parts).strip()[:12000]

    @staticmethod
    def _normalized_place_name(value: Any) -> str:
        return re.sub(r"\\s+", " ", str(value or "").strip()).casefold()

    async def _resolve_place_state(self, session, owner_hint: str) -> dict[str, Any]:
        story_id = session.resource_id
        with self.service.store.connection() as db:
            row = dict(self.service._story_row(db, story_id))
        if (row.get("latitude") is None or row.get("longitude") is None) and owner_hint.strip():
            # Only the author's explicit address, never infer a geocode query
            # from a clipped VAD fragment or substitute current device position.
            resolver = getattr(self.service, "_resolve_place_query", None)
            resolved = await resolver(owner_hint.strip()) if callable(resolver) else None
            if resolved:
                with self.service.store.tx() as db:
                    fresh = self.service._story_row(db, story_id)
                    prior = json.loads(fresh["research_json"] or "{}")
                    prior["identity_generation"] = int(prior.get("identity_generation") or 0) + 1
                    prior["location_provenance"] = {"kind": "owner_live_place_query", "query": owner_hint[:300], "not_device_current_location": True}
                    db.execute("UPDATE stories SET latitude=?,longitude=?,research_json=? WHERE id=?",
                               (float(resolved["lat"]), float(resolved["lon"]), canonical(prior), story_id))
        story = await self.service.resolve_identity(story_id, self._recent_transcript(session, owner_hint))
        identity = story.get("visual_identity") or {}
        with self.service.store.connection() as db:
            saved = json.loads(self.service._story_row(db, story_id)["research_json"] or "{}")
        pages = [{"title": str(page.get("title") or ""), "url": str(page.get("url") or "")}
                 for page in (saved.get("wikipedia") or [])[:12] if isinstance(page, dict)]
        return {"visual_identity": identity, "wikipedia": pages, "story": story}

    async def _reject_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        candidate_id = _bounded_text(args.get("candidate_id"), 300, required=True)
        reason = _bounded_text(args.get("reason"), 500)
        self.service.reject_identity(session.resource_id, candidate_id, reason)
        # Same provider session and same source photo. The service serializes
        # concurrent job/tool work and generation-checks late results.
        result = await self._resolve_place_state(session, "")
        with self.service.store.tx() as db:
            self._store_command(db, session.resource_id, command_id, "reject_place", args, result)
        return result

    async def _resolve_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        owner_hint = _bounded_text(args.get("owner_hint"), 500)
        result = await self._resolve_place_state(session, owner_hint)
        with self.service.store.tx() as db:
            self._store_command(db, session.resource_id, command_id, "resolve_place", args, result)
        return result

    async def _confirm_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        candidate_id = _bounded_text(args.get("candidate_id"), 300)
        candidate_name = _bounded_text(args.get("candidate_name"), 300)
        if not candidate_id and not candidate_name:
            raise ConflictError("live_place_confirmation_required", "candidate_id or candidate_name is required")

        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
        identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
        candidates = identity.get("candidates") if isinstance(identity.get("candidates"), list) else []
        if not candidates:
            await self._resolve_place_state(session, candidate_name)
            with self.service.store.connection() as db:
                row = self.service._story_row(db, story_id)
                research = json.loads(row["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            candidates = identity.get("candidates") if isinstance(identity.get("candidates"), list) else []

        chosen = None
        if candidate_id:
            chosen = next(
                (item for item in candidates if isinstance(item, dict) and str(item.get("candidate_id") or "") == candidate_id),
                None,
            )
        if chosen is None and candidate_name:
            wanted = self._normalized_place_name(candidate_name)
            exact = [
                item for item in candidates
                if isinstance(item, dict) and self._normalized_place_name(item.get("name")) == wanted
            ]
            if len(exact) == 1:
                chosen = exact[0]
            else:
                partial = [
                    item for item in candidates
                    if isinstance(item, dict)
                    and wanted
                    and (
                        wanted in self._normalized_place_name(item.get("name"))
                        or self._normalized_place_name(item.get("name")) in wanted
                    )
                ]
                if len(partial) == 1:
                    chosen = partial[0]
        if chosen is None:
            raise ConflictError(
                "live_place_candidate_unknown",
                "The confirmed place does not uniquely match the current OSM/Wikipedia candidates",
            )

        if not has_place_consent(session, str(chosen.get("name") or "")):
            record_live_diagnostic(self.service, story_id, session.id, "backend", "identity_confirmation_blocked",
                {"candidate_id": chosen.get("candidate_id"), "reason": "no_fresh_explicit_named_author_consent"})
            raise ConflictError("live_place_author_consent_required", "Нужно явное подтверждение автора с названием объекта. Шум, приветствие и догадка модели не являются согласием.")
        approval = consent_receipt(session)
        confirmed = {
            **identity,
            "approval_evidence": approval,
            "status": "owner_confirmed",
            "candidate_id": str(chosen.get("candidate_id") or ""),
            "candidate_name": str(chosen.get("name") or ""),
            "confidence": None,
            "photo_sha256": row["photo_sha256"],
            "candidate_url": chosen.get("url"),
            "source_links": [chosen["url"]] if chosen.get("url") else [],
            "generation": int(research.get("identity_generation") or 0),
            "observations": ["Объект явно подтверждён автором в текущем Live-разговоре."],
            "candidates": candidates,
        }
        research["visual_identity"] = confirmed
        transcript = self._recent_transcript(session, candidate_name)
        if transcript:
            research["transcript"] = transcript
        with self.service.store.tx() as db:
            current = self.service._story_row(db, story_id)
            current_research = json.loads(current["research_json"] or "{}")
            if current["photo_sha256"] != row["photo_sha256"] or int(current_research.get("identity_generation") or 0) != int(research.get("identity_generation") or 0):
                raise ConflictError("identity_candidate_changed", "Объект изменился; проверьте актуальный вариант.")
            from .poi_memory import ensure_poi_identity, hydrate_story_facts
            now = self.service.store.now()
            poi_id = ensure_poi_identity(
                db,
                confirmed,
                latitude=current["latitude"],
                longitude=current["longitude"],
                now=now,
            )
            reused = hydrate_story_facts(db, confirmed, story_id)
            research["poi_id"] = poi_id
            research["poi_reused_fact_count"] = reused
            db.execute(
                "UPDATE stories SET place_name=?,research_json=?,state='identity_ready',"
                "error_code=CASE WHEN error_code IN ('visual_identity_uncertain','identity_location_missing') THEN NULL ELSE error_code END,"
                "error_message=CASE WHEN error_code IN ('visual_identity_uncertain','identity_location_missing') THEN NULL ELSE error_message END,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (
                    confirmed["candidate_name"],
                    canonical(research),
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "visual_identity": confirmed,
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "confirm_place", args, result)
        session.state["author_turn"]["consumed"] = True
        from .identity_telemetry import record_identity_event
        record_identity_event(self.service, story_id, "identity_owner_confirmed", {"generation": confirmed["generation"], "candidate_id": confirmed["candidate_id"]})
        record_live_diagnostic(self.service, story_id, session.id, "backend", "identity_author_confirmed",
            {"candidate_id": confirmed["candidate_id"], **approval})
        logger.info(
            "street_story_live_place_confirmed story_id=%s candidate_id=%s",
            story_id,
            confirmed["candidate_id"],
        )
        return result

    def _emit_research_progress(
        self,
        session,
        *,
        stage: str,
        active: bool,
        query: str,
        source_count: int,
        fact_count: int,
        sources: list[dict[str, Any]] | None = None,
        batch_source_count: int = 0,
    ) -> None:
        visible_sources = []
        for source in (sources or [])[-10:]:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            if not url.startswith("https://"):
                continue
            visible_sources.append({
                "type": str(source.get("type") or "web"),
                "title": str(source.get("title") or url)[:180],
                "url": url,
            })
        self.emit(session, {
            "type": "research_progress",
            "status": "working" if active else "ready",
            "stage": stage,
            "state": {
                "active": active,
                "stage": stage,
                "query": str(query or "")[:300],
                "source_count": max(0, int(source_count)),
                "fact_count": max(0, int(fact_count)),
                "batch_source_count": max(0, int(batch_source_count)),
                "sources": visible_sources,
            },
        })

    async def _search_web(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if getattr(session, "closed", False) or session.state.get("research_cancelled"):
            raise ConflictError("live_research_partial", "This Live research phase is paused. Resume the saved run in a new Live session.")
        query = _bounded_text(args.get("query"), 1000, required=True)
        current_run = str(session.state.get("research_run_id") or "")
        if current_run:
            with self.service.store.connection() as db:
                pending_sources = db.execute("SELECT COUNT(*) FROM research_run_sources WHERE run_id=? AND source_version_id IS NULL", (current_run,)).fetchone()[0]
                pending_chunks = db.execute("SELECT COUNT(*) FROM research_chunk_runs WHERE run_id=? AND status NOT IN ('extracted','no_claims')", (current_run,)).fetchone()[0]
                if pending_sources or pending_chunks:
                    self._research_run_guard(db, session, current_run)
                    redirects = int(session.state.get("pending_discovery_redirects") or 0) + 1
                    session.state["pending_discovery_redirects"] = redirects
                    if redirects >= 3:
                        self._pause_research(session, "discovery_without_checkpoint_budget_exhausted")
                        self._emit_research_progress(session, stage="partial", active=False, query="", source_count=0, fact_count=0)
                        raise ConflictError("live_research_partial", "Read the saved run's pending document pages in a new Live session; repeated discovery produced no checkpoint.")
                    next_args = {"run_id": current_run}
                    for cid, offset in session.state.get("research_pending_page", {}).items():
                        if offset:
                            next_args.update({"chunk_id": cid, "passage_cursor": offset})
                            break
                    return {"research_run_id": current_run, "discovery_only": True, "semantic_status": "live_model_required", "continuation_required": True,
                            "next_tool": "get_research_chunk", "next_args": next_args,
                            "instruction": "Read the existing discovered document and ALL its next_args pages before repeating search. Choose a competent source from the retained addresses and copy its source_ref for the first document. The saved run and goal are unchanged.", "facts": [],
                            "sources": [{"source_ref": _search_source_ref(row["url"]), "url": row["url"], "title": row["title"], "supports": []}
                                        for row in db.execute("SELECT url,title FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url", (current_run,))]}
        coverage_goal = _bounded_text(args.get("coverage_goal") or query, 1600, required=True)
        story_id = session.resource_id

        with self.service.store.connection() as db:
            story = dict(self.service._story_row(db, story_id))
            snapshot_story_revision = int(story.get("revision") or 0)
            research = json.loads(story.get("research_json") or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
            if args.get('confirmed_poi_id') is not None and args['confirmed_poi_id'] != identity.get('candidate_id'):
                raise ConflictError('live_research_identity_mismatch',
                    f"The confirmed POI ID is {identity.get('candidate_id')}; copy it exactly. "
                    "Do not replace it with another source's ID for the same named landmark. Retry search_web for the confirmed object.")
            if args.get('query_matches_poi') is False:
                raise ConflictError('live_research_identity_mismatch', 'Your query/goal targets another object. Correct it using confirmed identity and geography.')
            all_story_facts = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": str(row["semantic_key"] or ""),
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT f.*,a.semantic_key FROM facts f "
                    "LEFT JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? ORDER BY f.rowid",
                    (story_id,),
                )
            ]
            known_facts = [
                {
                    **item,
                    "text": str(item["text"])[:400],
                }
                for item in all_story_facts[:80]
            ]
            from .poi_memory import prior_facts, processed_sources
            poi_history = prior_facts(db, identity, story_id)
            processed_source_history = processed_sources(db, identity)
            reusable_poi = [
                {**item, "selected": False}
                for item in poi_history
                if item.get("origin") == "poi_research"
            ]
            known_facts = merge_model_fact_inventory([*reusable_poi, *known_facts])

        run_id = "research_" + hashlib.sha256(
            f"{story_id}:{command_id}:search".encode("utf-8")
        ).hexdigest()[:24]
        calls_without_save = int(session.state.get("research_discovery_without_save") or 0)
        if calls_without_save >= 3:
            self._pause_research(session, "discovery_without_checkpoint_budget_exhausted")
            self._emit_research_progress(session, stage="partial", active=False, query="", source_count=0, fact_count=len(all_story_facts))
            record_live_diagnostic(self.service, story_id, session.id, "backend", "research_partial", {"reason": "discovery_without_checkpoint_budget_exhausted", "attempts": calls_without_save})
            raise ConflictError("live_research_partial", "Discovery paused after three searches without a saved document batch. Resume a discovered run in a new Live session with get_research_chunk; saved findings are preserved.")
        session.state["research_discovery_without_save"] = calls_without_save + 1
        session.state["research_run_id"] = run_id
        session.state.setdefault("research_run_ids", []).append(run_id)
        save_batch_id = "livebatch_" + hashlib.sha256(
            f"{run_id}:discovery-save".encode("utf-8")
        ).hexdigest()[:24]
        expected_identity_generation = int(research.get("identity_generation") or 0)
        with self.service.store.tx() as db:
            begin_research_run(
                db,
                story_id=story_id,
                poi_key=str(identity.get("candidate_id") or "") or None,
                goal=coverage_goal,
                expected_story_revision=int(story.get("revision") or 0),
                identity_generation=expected_identity_generation,
                run_id=run_id,
                now=self.service.store.now(),
            )
            set_run_state(
                db,
                run_id,
                "discovering",
                detail=query[:500],
                now=self.service.store.now(),
            )

        topic_context = {
            "place_name": story.get("place_name"),
            "latitude": story.get("latitude"),
            "longitude": story.get("longitude"),
            "current_draft": str(story.get("draft_text") or "")[:2500],
            "recent_author_context": self._recent_transcript(session, "")[:6000],
            "known_facts": known_facts,
            "previously_considered_poi_facts": poi_history[:60],
            "previously_processed_sources": processed_source_history,
            "coverage_goal": coverage_goal,
            "research_run_id": run_id,
            "visual_identity": identity,
            "live_first": True,
        }
        prior_progress_sources = [
            source for source in (research.get("grounding_sources") or [])
            if isinstance(source, dict)
        ]
        self._emit_research_progress(
            session,
            stage="searching",
            active=True,
            query=query,
            source_count=len(prior_progress_sources),
            fact_count=len(all_story_facts),
            sources=prior_progress_sources,
        )
        try:
            grounded = await self.service.providers.gemini.search_web(query, topic_context)
        except Exception as exc:
            with self.service.store.tx() as db:
                set_run_state(
                    db,
                    run_id,
                    "failed",
                    detail=type(exc).__name__,
                    now=self.service.store.now(),
                    completed=True,
                )
            raise
        with self.service.store.tx() as db:
            self._research_run_guard(db, session, run_id)
            set_run_state(
                db,
                run_id,
                "reconciling",
                detail="provider_result_received",
                now=self.service.store.now(),
            )
        search_provider = str(grounded.payload.get("search_provider") or "google_grounding")
        semantic_completion = str(grounded.payload.get("semantic_completion") or "").strip()
        semantic_status = str(grounded.payload.get("semantic_status") or "").strip()
        discovery_only = (
            search_provider in {"duckduckgo_html_fallback", "poi_cache_fallback"}
            and not semantic_completion
        )
        grounding_sources: list[dict[str, Any]] = []
        for source in grounded.grounding_sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            if not url.startswith("https://"):
                continue
            projected = dict(source)
            projected["source_ref"] = _search_source_ref(url)
            if discovery_only:
                evidence_ref_fn = getattr(
                    self.service.providers.gemini,
                    "_support_evidence_ref",
                    None,
                )
                projected_supports: list[dict[str, Any]] = []
                for raw_support in projected.get("supports") or []:
                    if not isinstance(raw_support, dict):
                        continue
                    support_text = str(raw_support.get("text") or "").strip()
                    if not support_text:
                        continue
                    support = dict(raw_support)
                    support["source_url"] = str(
                        support.get("source_url") or url
                    ).rstrip("/")
                    if not str(support.get("evidence_ref") or "").strip() and callable(evidence_ref_fn):
                        support["evidence_ref"] = evidence_ref_fn(url, support)
                    projected_supports.append(support)
                projected["supports"] = projected_supports
            grounding_sources.append(projected)

        source_objects = {
            str(source["url"]).rstrip("/"): source
            for source in grounding_sources
        }
        prior_decisions = {
            str(item.get("fact_id") or ""): bool(item.get("selected"))
            for item in all_story_facts
            if str(item.get("fact_id") or "").strip()
        }
        known_by_id = {
            str(item.get("fact_id") or ""): item
            for item in all_story_facts
            if str(item.get("fact_id") or "").strip()
        }

        raw_grounded_facts = [
            item for item in (grounded.payload.get("facts") or [])
            if isinstance(item, dict)
        ]
        reconciliation_matches: dict[int, str] = {
            index: str(item.get("existing_fact_id") or "")
            for index, item in enumerate(raw_grounded_facts)
            if str(item.get("existing_fact_id") or "") in known_by_id
        }
        reconciliation_decisions: list[dict[str, Any]] = [
            {
                "incoming_index": index,
                "relation": "equivalent",
                "existing_fact_id": fact_id,
                "rationale": "Upstream extraction explicitly referenced this durable fact ID.",
                "model_name": "upstream_existing_fact_id",
                "prompt_version": "fact-identity-reconciliation-v1",
            }
            for index, fact_id in sorted(reconciliation_matches.items())
        ]
        reconciliation_meta: dict[str, Any] = {
            "status": "not_needed",
            "pages_reviewed": 0,
            "existing_fact_count": len(all_story_facts),
            "incoming_fact_count": len(raw_grounded_facts),
        }
        reconciler = getattr(self.service.providers.gemini, "reconcile_fact_identities", None)
        if raw_grounded_facts and all_story_facts and callable(reconciler):
            try:
                reconciliation = await reconciler(raw_grounded_facts, all_story_facts)
                reconciliation_matches = {
                    int(index): str(fact_id)
                    for index, fact_id in (reconciliation.get("matches") or {}).items()
                    if str(fact_id) in known_by_id
                }
                reconciliation_decisions = [
                    dict(item)
                    for item in (reconciliation.get("decisions") or [])
                    if isinstance(item, dict)
                ]
                reconciliation_meta = {
                    "status": "complete" if reconciliation.get("complete") is True else "partial",
                    "pages_reviewed": int(reconciliation.get("pages_reviewed") or 0),
                    "existing_fact_count": int(reconciliation.get("existing_fact_count") or len(all_story_facts)),
                    "incoming_fact_count": int(reconciliation.get("incoming_fact_count") or len(raw_grounded_facts)),
                    "matched_count": len(reconciliation_matches),
                    "unmatched_count": int(reconciliation.get("unmatched_count") or 0),
                }
            except (GeminiUnavailable, MalformedProviderResponse, PermanentProviderError, RetryableProviderError) as exc:
                reconciliation_meta = {
                    **reconciliation_meta,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
        elif raw_grounded_facts and all_story_facts:
            reconciliation_meta["status"] = "compatibility_unavailable"

        normalized: list[dict[str, Any]] = []
        for item_index, item in enumerate(raw_grounded_facts):
            if not isinstance(item, dict):
                continue
            text = validated_model_fact_text(item.get("text"))
            if text is None:
                continue
            claim_key = normalized_claim_key(item.get("claim_key"))
            if claim_key is None:
                claim_key = "exact-text:" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:24]
            sources: list[dict[str, Any]] = []
            evidence_refs = {
                str(value)
                for value in (item.get("evidence_refs") or [])
                if str(value).strip()
            }
            for raw_url in item.get("source_urls", []) or []:
                candidate = source_objects.get(str(raw_url).rstrip("/"))
                if not candidate:
                    continue
                projected = dict(candidate)
                candidate_supports = [
                    support
                    for support in (candidate.get("supports") or [])
                    if isinstance(support, dict)
                    and str(support.get("text") or "").strip()
                ]
                if evidence_refs:
                    supports = [
                        support
                        for support in candidate_supports
                        if str(support.get("evidence_ref") or "") in evidence_refs
                    ]
                    if not supports:
                        continue
                    projected["supports"] = supports
                elif candidate_supports:
                    projected["supports"] = candidate_supports
                else:
                    continue
                if projected not in sources:
                    sources.append(projected)
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
            provided_existing_fact_id = str(item.get("existing_fact_id") or "").strip()
            existing_fact_id = reconciliation_matches.get(item_index) or (
                provided_existing_fact_id
                if provided_existing_fact_id in known_by_id
                else ""
            )
            fact_id = (
                existing_fact_id
                if existing_fact_id
                else candidate_assertion_id(claim_key, text)
            )
            normalized.append(
                {
                    "fact_id": fact_id,
                    "existing_fact_id": existing_fact_id or None,
                    "claim_key": claim_key,
                    "text": text,
                    "confidence": confidence,
                    "evidence_supported": bool(sources),
                    "selected": bool(sources) and prior_decisions.get(fact_id, True),
                    "sources": sources,
                }
            )
        new_fact_candidates = [
            fact
            for fact in normalized
            if fact["evidence_supported"] and not fact["existing_fact_id"]
        ]
        source_read_required = bool(grounding_sources) and not new_fact_candidates

        with self.service.store.connection() as db:
            current_story = dict(self.service._story_row(db, story_id))
            current_research = json.loads(current_story.get("research_json") or "{}")
        if (
            int(current_story.get("revision") or 0) != snapshot_story_revision
            or int(current_research.get("identity_generation") or 0)
            != expected_identity_generation
        ):
            with self.service.store.tx() as db:
                set_run_state(
                    db,
                    run_id,
                    "cancelled",
                    detail="story_or_identity_changed_after_model_await",
                    now=self.service.store.now(),
                    completed=True,
                )
            raise ConflictError(
                "research_result_stale",
                "Story or object identity changed while research was running; stale result was not applied.",
            )

        # Live Mira owns the semantic review after facts are durable. Do not call
        # the same helper detector again here when that helper may be the failed
        # dependency that caused the Live fallback.
        detected_conflicts: list[dict[str, Any]] = []

        with self.service.store.tx() as db:
            story_row = self.service._story_row(db, story_id)
            self._research_run_guard(db, session, run_id)
            commit_research = json.loads(story_row["research_json"] or "{}")
            if (
                int(story_row["revision"] or 0) != snapshot_story_revision
                or int(commit_research.get("identity_generation") or 0)
                != expected_identity_generation
            ):
                raise ConflictError(
                    "research_result_stale",
                    "Story or object identity changed before research commit; stale result was not applied.",
                )
            now = self.service.store.now()
            persist_fact_relation_events(
                db,
                story_id=story_id,
                run_id=run_id,
                incoming_facts=raw_grounded_facts,
                decisions=reconciliation_decisions,
                now=now,
            )
            persist_fact_candidates(
                db,
                story_id=story_id,
                poi_key=str(identity.get("candidate_id") or "") or None,
                facts=normalized,
                run_id=run_id,
                batch_id=save_batch_id,
                model_name=str(session.model),
                prompt_version="live-search-ledger-v1",
                now=now,
            )
            refresh_review_status(db, story_id, now)
            fact_count = db.execute(
                "SELECT COUNT(*) FROM facts WHERE story_id=?",
                (story_id,),
            ).fetchone()[0]
            review_required = bool(new_fact_candidates)
            set_run_state(
                db,
                run_id,
                "extracting" if discovery_only or source_read_required else "verifying",
                detail=(
                    "awaiting_live_fact_save"
                    if discovery_only
                    else (
                        "awaiting_live_source_read"
                        if source_read_required
                        else "awaiting_live_semantic_review"
                    )
                ),
                now=self.service.store.now(),
                completed=False,
            )
            for source in grounding_sources:
                if not db.execute("SELECT 1 FROM research_run_sources WHERE run_id=? AND url=?", (run_id, source["url"])).fetchone():
                    register_discovered_source(db, run_id=run_id, url=source["url"], title=str(source.get("title") or ""), status="snippet_only", now=now)
            research_manifest = run_manifest(db, run_id)

            research = json.loads(story_row["research_json"] or "{}")
            prior_sources = research.get("grounding_sources")
            all_sources: dict[str, dict[str, str]] = {}
            if isinstance(prior_sources, list):
                for source in prior_sources:
                    if isinstance(source, dict) and str(source.get("url") or "").startswith("https://"):
                        all_sources[str(source["url"]).rstrip("/")] = source
            for source in grounding_sources:
                all_sources[str(source["url"]).rstrip("/")] = source

            from .poi_memory import persist_research_memory
            persist_research_memory(
                db,
                identity,
                normalized,
                grounding_sources,
                query,
                self.service.store.now(),
                research_run_id=run_id,
            )

            history = research.get("live_web_searches")
            history = list(history) if isinstance(history, list) else []
            history.append(
                {
                    "query": query,
                    "coverage_goal": coverage_goal,
                    "research_run_id": run_id,
                    "save_batch_id": save_batch_id,
                    "review_required": review_required,
                    "summary": str(grounded.payload.get("summary") or "")[:2000],
                    "source_urls": [source["url"] for source in grounding_sources[:20]],
                    "source_refs": [
                        source["source_ref"]
                        for source in grounding_sources[:20]
                        if isinstance(source.get("source_ref"), str)
                    ],
                    "search_provider": search_provider,
                    "discovery_only": discovery_only,
                    "semantic_completion": semantic_completion or None,
                    "semantic_status": semantic_status or None,
                    "coverage_satisfied": bool(grounded.payload.get("coverage_satisfied")),
                    "missing_aspects": list(grounded.payload.get("missing_aspects") or [])[:20],
                    "extraction_complete": grounded.payload.get("extraction_complete") is not False,
                    "continuation_reason": str(grounded.payload.get("continuation_reason") or "")[:500],
                    "extraction_audit": grounded.payload.get("extraction_audit"),
                    "fact_reconciliation": reconciliation_meta,
                }
            )
            research["grounding_sources"] = list(all_sources.values())[:80]
            research["live_web_searches"] = history[-12:]
            db.execute(
                "UPDATE stories SET research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.service.store.now(), story_id),
            )
            result = {
                "query": query,
                "coverage_goal": coverage_goal,
                "sources_skipped_completed": int(grounded.payload.get("sources_skipped_completed") or 0),
                "research_run_id": run_id,
                "save_batch_id": save_batch_id,
                "review_required": review_required,
                "continuation_required": bool(source_read_required or review_required),
                "next_tool": "save_research_facts" if discovery_only else "get_research_chunk" if source_read_required else None,
                "next_args": {"run_id": run_id, "batch_id": save_batch_id} if discovery_only else {"run_id": run_id} if source_read_required else None,
                "instruction": (
                    "First review snippets: sufficient snippet -> save_research_facts immediately with exact source_ref/evidence_ref, batch_reviewed=true, verdict=supported, atomic/support_complete/qualifiers_preserved=true, review_reason and selected=false. Never speak unsaved snippets as facts. Preserve planned/future wording: стоимость составит is not actual cost. If insufficient, choose one competent "
                    "source from this result and call get_research_chunk with run_id plus "
                    "that exact source_ref before giving a factual answer."
                    if source_read_required
                    else None
                ),
                "research_manifest": research_manifest,
                "summary": str(grounded.payload.get("summary") or "")[:2000],
                "search_provider": search_provider,
                "discovery_only": discovery_only,
                "semantic_completion": semantic_completion or None,
                "semantic_status": semantic_status or None,
                "coverage_satisfied": bool(grounded.payload.get("coverage_satisfied")),
                "missing_aspects": list(grounded.payload.get("missing_aspects") or [])[:20],
                "extraction_complete": grounded.payload.get("extraction_complete") is not False,
                "continuation_reason": str(grounded.payload.get("continuation_reason") or "")[:500],
                "extraction_audit": grounded.payload.get("extraction_audit"),
                "fact_reconciliation": reconciliation_meta,
                "facts": normalized,
                "sources": grounding_sources[:20],
                "fact_conflicts": detected_conflicts[:12],
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "search_web", args, result)
            self._emit_research_progress(
                session,
                stage="extracting" if discovery_only else "review",
                active=bool(discovery_only or review_required),
                query=query,
                source_count=len(all_sources),
                fact_count=int(fact_count),
                sources=list(all_sources.values()),
                batch_source_count=len(grounding_sources),
            )
            logger.info(
                "street_story_live_web_search story_id=%s run_id=%s facts=%s sources=%s skipped_completed=%s",
                story_id,
                run_id,
                len(normalized),
                len(grounding_sources),
                int(grounded.payload.get('sources_skipped_completed') or 0),
            )
            return result

    def _research_run_guard(self, db, session, run_id: str):
        story = self.service._story_row(db, session.resource_id)
        run = db.execute("SELECT * FROM research_runs WHERE run_id=? AND story_id=?", (run_id, session.resource_id)).fetchone()
        research = json.loads(story["research_json"] or "{}")
        if run is None:
            raise ConflictError("live_research_run_unknown", "Research run does not belong to this story.")
        if getattr(session, "closed", False) or session.state.get("research_cancelled") or str(run["state"]) in {"cancelled", "failed", "completed"}:
            raise ConflictError("live_research_run_terminal", "Research is no longer writable in this session.")
        if int(research.get("identity_generation") or 0) != int(run["identity_generation"] or 0):
            raise ConflictError("live_research_run_stale", "Object identity changed.")
        return story, run

    @staticmethod
    def _core_passages(chunk_id, core, *, contextual=False, source_text=None, core_start=0):
        """Address literal paragraphs; this makes no semantic fact decisions."""
        passages, cursor = [], 0
        # HTML navigation often produces dozens of tiny lines. Address fixed
        # consecutive windows in the normal flow so pagination advances through
        # the document rather than repeating its menu with every context span.
        lines = [core[i:i + 900] for i in range(0, len(core), 900)] if contextual else core.splitlines(keepends=True)
        for line in lines:
            for start in range(0, len(line), 900):
                raw = line[start:start + 900]
                text = raw.strip()
                if text:
                    offset = cursor + start + len(raw) - len(raw.lstrip())
                    ref = "evref_" + hashlib.sha256(f"{chunk_id}:{offset}:{text}".encode()).hexdigest()[:24]
                    passages.append({"passage_id": len(passages), "evidence_ref": ref, "text": text, "core_offset": offset})
            cursor += len(line)
        if contextual:
            for passage in passages:
                document = core if source_text is None else source_text
                origin = 0 if source_text is None else core_start
                start = max(0, origin + passage['core_offset'] - 450)
                end = min(len(document), origin + passage['core_offset'] + len(passage['text']) + 250)
                # Keep literal source context across a core boundary.
                text = document[start:end]
                offset = start - origin
                passage.update(text=text, core_offset=offset,
                               evidence_ref='evref_' + hashlib.sha256(f'{chunk_id}:{offset}:{text}'.encode()).hexdigest()[:24])
        return passages

    async def _get_research_chunk(self, session, args):
        run_id = _bounded_text(args.get("run_id"), 160, required=True)
        session.state["research_run_id"] = run_id
        source_url = str(args.get("source_url") or "").rstrip("/")
        source_ref = str(args.get('source_ref') or '')
        chunk_id = str(args.get("chunk_id") or "")
        with self.service.store.connection() as db:
            story, run = self._research_run_guard(db, session, run_id)
            snapshot_revision = int(story["revision"] or 0)
            sources = [dict(row) for row in db.execute("SELECT * FROM research_run_sources WHERE run_id=? ORDER BY discovered_at,url", (run_id,))]
            if source_ref:
                chosen = next((row['url'] for row in sources if _search_source_ref(row['url']) == source_ref), None)
                if chosen is None:
                    raise ConflictError('live_research_source_unknown', 'Copy an exact short source_ref from the saved discovery of this run; do not invent a URL or silently choose another source.')
                if source_url and unquote(source_url) != unquote(chosen):
                    raise ConflictError('live_research_source_unknown', 'source_ref and source_url disagree; pass only the chosen discovery source_ref.')
                source_url = chosen
            if source_url:
                matching = next((row["url"] for row in sources if unquote(row["url"]) == unquote(source_url)), None)
                if matching is None:
                    raise ConflictError("live_research_source_unknown", "The URL does not match this run's saved discovery. Copy the chosen short source_ref instead; do not repeat search or switch to an arbitrary source.")
                source_url = matching  # Fetch only the stored discovered URL.
            elif session.state.get('live_first_research') and len(sources) > 1 and not any(row['source_version_id'] or row['status'] == 'failed' for row in sources):
                raise ConflictError('live_research_source_choice_required', 'Choose a competent source from the saved discovery by title/provenance and pass its exact source_ref. The server does not rank sources semantically.')

            candidate = db.execute(
                "SELECT c.*,v.normalized_text,v.final_url,"
                "(SELECT s.url FROM research_run_sources s WHERE s.run_id=r.run_id AND s.source_version_id=c.source_version_id "
                "ORDER BY s.url LIMIT 1) AS requested_url FROM research_chunk_runs r "
                "JOIN source_chunks c ON c.chunk_id=r.chunk_id JOIN source_versions v ON v.source_version_id=c.source_version_id "
                "WHERE r.run_id=? AND (?='' OR r.chunk_id=?) AND (?='' OR EXISTS "
                "(SELECT 1 FROM research_run_sources s WHERE s.run_id=r.run_id AND s.source_version_id=c.source_version_id AND s.url=?)) "
                "AND (?<>'' OR r.status NOT IN ('extracted','no_claims')) ORDER BY c.source_version_id,c.ordinal LIMIT 1",
                (run_id, chunk_id, chunk_id, source_url, source_url, chunk_id),
            ).fetchone()
            candidate = dict(candidate) if candidate else None
        if candidate is None:
            if chunk_id:
                raise ConflictError("live_research_chunk_unknown", "Retry get_research_chunk with run_id only. Omit chunk_id to read the next chunk; never invent chunk IDs.")
            with self.service.store.connection() as db:
                progress = self._live_research_progress(db, session.resource_id, run_id)
            if source_url and progress['remaining'] and any(row['url'] == source_url and row['source_version_id'] for row in sources):
                return await self._get_research_chunk(session, {'run_id': run_id})
            bounded_stop = session.state.get('live_first_research') and progress['attempts'] >= 3
            source = None if bounded_stop else next((row for row in sources if (not source_url or row["url"] == source_url) and not row["source_version_id"] and row['status'] != 'failed'), None)
            if source is None:
                if session.state.get('live_first_research'):
                    with self.service.store.tx() as db:
                        self._research_run_guard(db, session, run_id)
                        manifest = run_manifest(db, run_id)
                        unreviewed = db.execute("SELECT COUNT(DISTINCT a.assertion_id) FROM fact_assertions a JOIN fact_observations o ON o.story_id=a.story_id AND o.assertion_id=a.assertion_id WHERE a.story_id=? AND o.run_id=? AND a.eligibility='unreviewed'", (session.resource_id, run_id)).fetchone()[0]
                        complete = (manifest_complete(manifest) or bounded_stop) and not unreviewed
                        set_run_state(db, run_id, 'completed' if complete else 'partial', detail='live_no_new_confirmed_facts' if not progress['observations'] else 'live_batches_complete' if complete else 'saved_findings_need_review', now=self.service.store.now(), completed=complete)
                    self._emit_research_progress(session, stage='completed' if complete else 'partial', active=False, query='', source_count=len(sources), fact_count=len(self._get_facts(session.resource_id, {})['facts']))
                    return {'research_run_id': run_id, 'all_chunks_processed': True, 'completed': complete, 'partial': not complete,
                            'next_tool': None, 'full_source_attempts': progress['attempts'],
                            'instruction': 'Report only saved supported facts; if none, say новых подтверждённых фактов не нашла. Never report unsaved snippets.', 'state': self._compact_context(self._topic_state(session.resource_id))}
                with self.service.store.connection() as db:
                    return {"research_run_id": run_id, "all_chunks_processed": True, "research_manifest": run_manifest(db, run_id), "next_tool": "get_review_packet", "next_args": {"run_id": run_id}, "final_tool": "finalize_fact_review"}
            fetch = getattr(self.service.providers.gemini, "_fetch_page_documents", None)
            if not callable(fetch):
                raise ConflictError("live_research_fetch_unavailable", "Document reader is unavailable; saved evidence is preserved.")
            self._emit_research_progress(session, stage="extracting", active=True, query=str(run["goal"]), source_count=len(sources), fact_count=0)
            documents = await fetch([source["url"]], {"research_run_id": run_id, "research_sources": sources})
            with self.service.store.tx() as db:
                current, _ = self._research_run_guard(db, session, run_id)
                if int(current["revision"] or 0) != snapshot_revision:
                    raise ConflictError("live_research_run_stale", "Story changed while reading evidence.")
            if not documents:
                if session.state.get('live_first_research'):
                    # One unreachable document must not terminate discovery of
                    # the other saved sources. The failed receipt stays durable.
                    with self.service.store.tx() as db:
                        self._research_run_guard(db, session, run_id)
                        db.execute("UPDATE research_run_sources SET status='failed',error_code=COALESCE(error_code,'fetch_empty'),updated_at=? WHERE run_id=? AND url=? AND source_version_id IS NULL", (self.service.store.now(), run_id, source['url']))
                    logger.warning('street_story_source_skipped run=%s reason=fetch_failed remaining=%s', run_id,
                                   sum(not s['source_version_id'] and s['status'] != 'failed' and s['url'] != source['url'] for s in sources))
                    return await self._get_research_chunk(session, {'run_id': run_id})
                with self.service.store.tx() as db:
                    self._research_run_guard(db, session, run_id)
                    set_run_state(db, run_id, "partial", detail="source_fetch_failed_resume_required", now=self.service.store.now(), completed=False)
                    saved_count = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=?", (session.resource_id,)).fetchone()[0]
                session.state["research_cancelled"] = True
                self._emit_research_progress(session, stage="partial", active=False, query="", source_count=len(sources), fact_count=saved_count)
                return {"research_run_id": run_id, "partial": True, "reason": "source_fetch_failed", "next_tool": None, "resume_tool": "get_research_chunk", "saved_fact_count": saved_count}
            return await self._get_research_chunk(session, {**args, "source_url": source["url"]})
        with self.service.store.connection() as db:
            checkpoint = chunk_checkpoint(db, run_id, candidate["chunk_id"])
            if checkpoint["terminal"]:
                raise ConflictError("live_research_chunk_completed", "This core is already saved. Call get_research_chunk with run_id ONLY: omit chunk_id and passage_cursor to read the next unfinished core.")
        batch_index = int(checkpoint["next_batch_index"])
        batch_id = "batch_" + hashlib.sha256(f"{run_id}:{candidate['chunk_id']}:{batch_index}".encode()).hexdigest()[:24]
        core = candidate["normalized_text"][candidate["core_start"]:candidate["core_end"]]
        result = {
            "research_run_id": run_id, "source_url": candidate["requested_url"],
            "source_ref": _search_source_ref(candidate["requested_url"]),
            "source_version_id": candidate["source_version_id"], "chunk_id": candidate["chunk_id"],
            "ordinal": candidate["ordinal"], "core_start": candidate["core_start"], "core_end": candidate["core_end"],
            "core_text": core, "context_text": candidate["chunk_text"],
            "evidence_passages": self._core_passages(candidate["chunk_id"], core, contextual=bool(session.state.get('live_first_research')), source_text=candidate['normalized_text'], core_start=candidate['core_start']),
            "context_before": candidate["normalized_text"][candidate["context_start"]:candidate["core_start"]],
            "context_after": candidate["normalized_text"][candidate["core_end"]:candidate["context_end"]],
            "batch_id": batch_id, "batch_index": batch_index, "expected_story_revision": snapshot_revision,
            "checkpoint": checkpoint, "next_tool": "save_research_facts",
        }
        passages = result["evidence_passages"]
        seen = session.state.setdefault("research_passages_seen", {}).setdefault(candidate["chunk_id"], set())
        seen.update(checkpoint.get('read_passage_ids') or [])
        recipe = {key: result[key] for key in ("chunk_id", "batch_id", "batch_index", "expected_story_revision")}
        recipe["run_id"] = run_id
        session.state.setdefault("research_chunk_receipts", {})[candidate["chunk_id"]] = recipe
        session.state['research_current_chunk_id'] = candidate['chunk_id']
        if "passage_cursor" not in args and len(seen) == len(passages):
            session.state.setdefault('research_page_passage_ids', {})[candidate['chunk_id']] = set(seen)
            return {**result, "core_text": "", "context_text": "", "evidence_passages": [], "context_before": "", "context_after": "",
                    "checkpoint": {"next_batch_index": batch_index, "saved_fact_count": len(checkpoint.get("facts", [])), "facts": checkpoint.get("facts", [])[:3]},
                    "ready_to_save": True, "has_more_passages": False, "next_passage_cursor": None,
                    "next_tool": "save_research_facts", "next_args": recipe,
                    "instruction": "ALL passages of this core have been read. Save findings using next_args, or explicitly save facts=[] if no new claim. This checkpoint is required before review. Do not reread this core."}
        offset = max(0, int(args["passage_cursor"])) if "passage_cursor" in args else next((p["passage_id"] for p in passages if p["passage_id"] not in seen), 0)
        if offset >= len(passages) and passages:
            raise ConflictError("live_research_cursor_invalid", "passage_cursor is a passage number, not core_start/core_end. Use exact next_passage_cursor or call with run_id only to resume the pending core.")
        result["evidence_passages"] = []
        if offset < len(passages):
            start = passages[offset]["core_offset"]
            result["context_before"] = core[max(0, start - 100):start] if start else result["context_before"]
        result["context_before"] = result["context_before"][-100:]
        result["context_after"] = result["context_after"][:100]
        # Saved payload remains durable; read its inventory separately instead
        # of repeating every previously saved claim on each document page.
        result["checkpoint"] = {"next_batch_index": checkpoint["next_batch_index"], "saved_fact_count": len(checkpoint.get("facts", [])), "facts": checkpoint.get("facts", [])[:3], "terminal": checkpoint["terminal"]}
        for passage in passages[offset:]:
            if session.state.get('live_first_research') and len(result['evidence_passages']) >= 2:
                break
            trial = {**result, "evidence_passages": [*result["evidence_passages"], passage], "has_more_passages": True, "next_passage_cursor": passage["passage_id"] + 1}
            end = passage["core_offset"] + len(passage["text"])
            trial["context_after"] = core[end:end + 100] if end < len(core) else result["context_after"]
            trial["next_args"] = {"run_id": run_id, "chunk_id": candidate["chunk_id"], "passage_cursor": passage["passage_id"] + 1}
            trial["instruction"] = "The target may be in the unread tail. Read next_args before another search; do not assume missing facts from this first page. You may checkpoint this page with continuation_needed=true."
            if response_units("get_research_chunk", self._model_result("get_research_chunk", trial)) > PAGE_UNITS:
                break
            result = trial
        next_offset = offset + len(result["evidence_passages"])
        if not result["evidence_passages"] and offset < len(passages):
            raise ConflictError("live_research_page_oversize", "Read inventory separately; one page exceeds the ceiling.")
        if result["evidence_passages"]:
            last = result["evidence_passages"][-1]
            end = last["core_offset"] + len(last["text"])
            if end < len(core):
                result["context_after"] = core[end:end + 100]
        result["has_more_passages"] = next_offset < len(passages)
        result["next_passage_cursor"] = next_offset if next_offset < len(passages) else None
        if not result["has_more_passages"]:
            result.pop("next_args", None)
            result.pop("instruction", None)
        if result["has_more_passages"]:
            result["next_tool"] = "get_research_chunk"
            result["next_args"] = {"run_id": run_id, "chunk_id": candidate["chunk_id"], "passage_cursor": next_offset}
            result["instruction"] = "The target may be in the unread tail. Read next_args before another search; do not assume missing facts from this first page. You may checkpoint this page with continuation_needed=true."
        seen = session.state.setdefault("research_passages_seen", {}).setdefault(candidate["chunk_id"], set())
        seen.update(p["passage_id"] for p in result["evidence_passages"])
        session.state.setdefault('research_page_passage_ids', {})[candidate['chunk_id']] = {
            p['passage_id'] for p in result['evidence_passages']
        }
        session.state.setdefault("research_pending_page", {})[candidate["chunk_id"]] = next_offset if next_offset < len(passages) else 0
        if session.state.get('live_first_research'):
            result['next_tool'] = 'save_research_facts'
            recipe['continuation_needed'] = next_offset < len(passages)
            result['next_args'] = {'batch_reviewed': True, 'source_matches_poi': True}
            result['instruction'] = 'Extract and check assertions in THIS small page, then save facts with own passage_ids and explicit batch_reviewed/source_matches_poi. Copy the exact passage_id values shown here; these are stable chunk IDs, not zero-based indexes of this page. Do not use IDs from earlier pages. The server binds this frozen read checkpoint; do not copy batch hashes or revisions. Save facts=[] for a page without findings, then follow the save receipt to the next unread page.'
        return result


    async def _save_research_facts(
        self,
        session,
        command_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        story_id = session.resource_id
        canonical_args = args
        if session.state.get('live_first_research') and not args.get('chunk_id'):
            current_chunk = session.state.get('research_current_chunk_id')
            recipe = session.state.get('research_chunk_receipts', {}).get(current_chunk)
            if recipe and (not args.get('batch_id') or args['batch_id'] == recipe['batch_id']):
                if args.get('run_id') and args['run_id'] != recipe['run_id']:
                    raise ConflictError('live_research_run_unknown', 'The findings do not belong to the current read checkpoint.')
                args = {**recipe, **args}
        raw_facts = args.get("facts")
        explicit_batch_id = str(args.get("batch_id") or "")
        if explicit_batch_id:
            replay = self._command_replay(story_id, explicit_batch_id, "save_research_facts", canonical_args)
            if replay is not None:
                return replay
        if args.get('source_matches_poi') is False and raw_facts:
            raise ConflictError('live_research_identity_mismatch', 'Do not import claims from another object. Checkpoint its read core with facts=[].')
        if args.get('source_content_valid') is False and raw_facts:
            raise ConflictError('live_research_source_content_invalid', 'Menu/challenge/fetch fragments cannot support article claims. Save facts=[].')
        chunk_id = str(args.get("chunk_id") or "")
        chunk_row = None
        batch_index = 0
        continuation_needed = args.get("continuation_needed") is True
        if not isinstance(raw_facts, list) or not (0 if chunk_id else 1) <= len(raw_facts) <= 32:
            raise ConflictError(
                "live_research_facts_invalid",
                "Provide between 1 and 32 facts from the latest search evidence",
            )
        expanded = []
        for group in raw_facts:
            if isinstance(group, dict) and 'claims' in group:
                claims = group['claims']
                if not isinstance(claims, list) or not 1 <= len(claims) <= 32 or any(not isinstance(c, dict) for c in claims):
                    raise ConflictError('live_research_facts_invalid', 'An evidence group needs explicit bounded claim objects.')
                expanded.extend({**{k: v for k, v in group.items() if k != 'claims'}, **claim} for claim in claims)
            else:
                expanded.append(group)  # Legacy receipts remain replayable.
        if len(expanded) > 32:
            raise ConflictError('live_research_facts_invalid', 'Save at most 32 claims per batch and continue the same core.')
        raw_facts = expanded

        # Phase 1 is read-only. Never hold a SQLite write transaction while the
        # semantic reconciler is making a provider call.
        with self.service.store.connection() as db:
            story = self.service._story_row(db, story_id)
            snapshot_revision = int(story["revision"] or 0)
            if chunk_id and args.get("expected_story_revision") != snapshot_revision:
                raise ConflictError("live_research_save_stale", "Provide the exact story revision from get_research_chunk.")
            research = json.loads(story["research_json"] or "{}")
            snapshot_identity_generation = int(research.get("identity_generation") or 0)
            identity = (
                research.get("visual_identity")
                if isinstance(research.get("visual_identity"), dict)
                else {}
            )
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )

            history = research.get("live_web_searches")
            if not isinstance(history, list) or not history:
                raise InvalidStateError(
                    "live_search_required",
                    "Сначала выполните search_web в этой теме.",
                )
            requested_run_id = str(args.get("run_id") or "").strip()
            discovery_candidates = [
                (index, dict(item))
                for index, item in enumerate(history)
                if isinstance(item, dict)
                and (bool(item.get("discovery_only")) or bool(chunk_id))
                and (
                    not requested_run_id
                    or str(item.get("research_run_id") or "").strip()
                    == requested_run_id
                )
            ]
            if requested_run_id:
                if len(discovery_candidates) != 1:
                    raise ConflictError(
                        "live_research_run_unknown",
                        "The requested discovery research run is not current in this story.",
                    )
            else:
                pending = [
                    pair
                    for pair in discovery_candidates
                    if not pair[1].get("mira_saved_fact_ids")
                ]
                if len(pending) != 1:
                    raise ConflictError(
                        "live_research_run_ambiguous",
                        "Provide the exact research_run_id returned by search_web.",
                    )
                discovery_candidates = pending
            history_index, latest_search = discovery_candidates[0]
            snapshot_search_run_id = str(
                latest_search.get("research_run_id") or ""
            ).strip()
            if not snapshot_search_run_id:
                raise ConflictError(
                    "live_research_run_missing",
                    "Discovery search did not provide a durable research run id.",
                )
            run_id = snapshot_search_run_id
            expected_save_batch_id = str(
                latest_search.get("save_batch_id") or ""
            ).strip() or (
                "livebatch_"
                + hashlib.sha256(
                    f"{run_id}:discovery-save".encode("utf-8")
                ).hexdigest()[:24]
            )
            self._research_run_guard(db, session, run_id)
            if chunk_id:
                chunk_row = db.execute(
                    "SELECT c.*,v.normalized_text,v.requested_url FROM research_chunk_runs r "
                    "JOIN source_chunks c ON c.chunk_id=r.chunk_id JOIN source_versions v ON v.source_version_id=c.source_version_id "
                    "WHERE r.run_id=? AND r.chunk_id=?", (run_id, chunk_id),
                ).fetchone()
                if chunk_row is None:
                    raise ConflictError("live_research_chunk_unknown", "Chunk is outside this run.")
                try:
                    batch_index = int(args.get("batch_index"))
                except (TypeError, ValueError):
                    raise ConflictError("live_research_batch_invalid", "Exact batch index is required.") from None
                all_passages = self._core_passages(chunk_id, chunk_row["normalized_text"][chunk_row["core_start"]:chunk_row["core_end"]], contextual=bool(session.state.get('live_first_research')), source_text=chunk_row['normalized_text'], core_start=chunk_row['core_start'])
                seen = session.state.get("research_passages_seen", {}).get(chunk_id, set())
                if len(seen) < len(all_passages):
                    continuation_needed = True
                checkpoint = chunk_checkpoint(db, run_id, chunk_id)
                expected_save_batch_id = "batch_" + hashlib.sha256(f"{run_id}:{chunk_id}:{batch_index}".encode()).hexdigest()[:24]
                # A batch owns its receipt across different provider call IDs.
                prior = self._command_replay(story_id, expected_save_batch_id, "save_research_facts", canonical_args)
                if prior is not None:
                    return prior
                if checkpoint["terminal"] or batch_index != checkpoint["next_batch_index"]:
                    raise ConflictError("live_research_batch_stale", "Use the next batch returned by get_research_chunk.")
                if not args.get("batch_id"):
                    raise ConflictError("live_research_batch_required", "Exact batch ID is required for page findings.")
            requested_batch_id = str(args.get("batch_id") or "").strip()
            if requested_batch_id and requested_batch_id != expected_save_batch_id:
                raise ConflictError(
                    "live_research_batch_mismatch",
                    "save_research_facts batch_id does not belong to this research run.",
                )
            save_batch_id = requested_batch_id or expected_save_batch_id
            if not latest_search.get("discovery_only") and not args.get("chunk_id"):
                raise ConflictError(
                    "live_research_facts_not_discovery",
                    "save_research_facts is only for the discovery-only search fallback",
                )

            allowed_refs = {
                str(ref)
                for ref in latest_search.get("source_refs", [])
                if re.fullmatch(r"websrc_[0-9a-f]{20}", str(ref))
            }
            source_map: dict[str, dict[str, Any]] = {}
            evidence_map: dict[str, tuple[str, dict[str, Any]]] = {}
            for source in research.get("grounding_sources") or []:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").rstrip("/")
                source_ref = str(source.get("source_ref") or "")
                supports = source.get("supports")
                if (
                    source_ref not in allowed_refs
                    or not url.startswith("https://")
                    or not isinstance(supports, list)
                ):
                    continue
                valid_supports = [
                    support
                    for support in supports
                    if isinstance(support, dict)
                    and str(support.get("text") or "").strip()
                    and str(support.get("source_url") or "").rstrip("/") == url
                ]
                if valid_supports:
                    source_map[source_ref] = {
                        **source,
                        "supports": valid_supports,
                    }
                    for support in valid_supports:
                        evidence_ref = str(
                            support.get("evidence_ref") or ""
                        ).strip()
                        if re.fullmatch(r"evref_[0-9a-f]{24}", evidence_ref):
                            evidence_map[evidence_ref] = (
                                source_ref,
                                support,
                            )

            if chunk_row is not None:
                url = str(chunk_row["requested_url"]).rstrip("/")
                source_ref = _search_source_ref(url)
                core = chunk_row["normalized_text"][chunk_row["core_start"]:chunk_row["core_end"]]
                passages = self._core_passages(chunk_id, core, contextual=bool(session.state.get('live_first_research')), source_text=chunk_row['normalized_text'], core_start=chunk_row['core_start'])
                addressed = {p["evidence_ref"]: p["text"] for p in passages}
                numbered = {p["passage_id"]: p["text"] for p in passages}
                current_passage_ids = session.state.get('research_page_passage_ids', {}).get(chunk_id, set(numbered))
                quoted_facts = []
                for raw in raw_facts:
                    if not isinstance(raw, dict):
                        raise ConflictError("live_research_fact_invalid", "Each fact must be an object.")
                    quotes = raw.get("evidence_quotes")
                    if quotes is None or quotes == []:
                        passage_ids = raw.get("passage_ids")
                        if passage_ids is not None:
                            if not isinstance(passage_ids, list) or not 1 <= len(passage_ids) <= 8 or any(type(pid) is not int or pid not in numbered for pid in passage_ids):
                                raise ConflictError("live_research_passage_unknown", "Choose numeric passage_ids from this frozen chunk.")
                            if session.state.get('live_first_research') and any(pid not in current_passage_ids for pid in passage_ids):
                                raise ConflictError('live_research_passage_stale',
                                    f"Copy the exact passage_id values from the CURRENT read page: {sorted(current_passage_ids)}. "
                                    "Do not renumber them from zero or reuse an earlier page ID. No findings were saved; retry the same page with its own supporting passages.")
                            quotes = [numbered[pid] for pid in passage_ids]
                    if quotes is None or quotes == []:
                        supplied_refs = raw.get("evidence_refs")
                        if not isinstance(supplied_refs, list) or not supplied_refs or any(ref not in addressed for ref in supplied_refs):
                            raise ConflictError("live_research_evidence_unknown", "Prefer numeric passage_ids from this chunk's evidence_passages, or use exact evidence_refs/verbatim evidence_quotes.")
                        quotes = [addressed[ref] for ref in supplied_refs]
                    if not isinstance(quotes, list) or not 1 <= len(quotes) <= 8:
                        raise ConflictError("live_research_quotes_required", "Each page fact requires exact core passages.")
                    refs = []
                    supports = []
                    for value in quotes:
                        quote = str(value or "")
                        offset = core.find(quote)
                        if session.state.get('live_first_research') and quote in addressed.values():
                            offset = next(p['core_offset'] for p in passages if p['text'] == quote)
                        if not quote.strip() or len(quote) > 1600 or (offset < 0 and quote not in addressed.values()):
                            raise ConflictError("live_research_quote_invalid", "Quote is not a verbatim passage of this frozen core.")
                        evidence_ref = "evref_" + hashlib.sha256(f"{chunk_id}:{offset}:{quote}".encode()).hexdigest()[:24]
                        support = {"kind": "verified_page_span", "source_url": url, "source_version_id": chunk_row["source_version_id"],
                                   "chunk_id": chunk_id, "evidence_ref": evidence_ref, "text": quote,
                                   "span_start": chunk_row["core_start"] + offset, "span_end": chunk_row["core_start"] + offset + len(quote)}
                        refs.append(evidence_ref)
                        supports.append(support)
                        evidence_map[evidence_ref] = (source_ref, support)
                    source_map.setdefault(source_ref, {"url": url, "source_ref": source_ref, "type": "web", "supports": []})["supports"].extend(supports)
                    quoted_facts.append({**raw, "source_refs": [source_ref], "evidence_refs": refs})
                raw_facts = quoted_facts

            known_facts = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": "",
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["owner_selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT f.*,a.owner_selected FROM facts f JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? ORDER BY f.rowid",
                    (story_id,),
                )
            ]

        known_by_id = {
            str(item["fact_id"]): item
            for item in known_facts
        }
        prior_decisions = {
            str(item["fact_id"]): bool(item["selected"])
            for item in known_facts
        }
        old_selected = {
            str(item["fact_id"])
            for item in known_facts
            if item["selected"] and item["evidence_supported"]
        }

        normalized_candidates: list[dict[str, Any]] = []
        for item in raw_facts:
            if not isinstance(item, dict):
                raise ConflictError(
                    "live_research_fact_invalid",
                    "Each fact must be an object",
                )
            fact_text = validated_model_fact_text(item.get("text"))
            if fact_text is None:
                raise ConflictError(
                    "live_research_fact_invalid",
                    "Fact text is required and must stay within the safety bound",
                )
            claim_key = normalized_claim_key(item.get("claim_key"))
            if claim_key is None:
                claim_key = (
                    "exact-text:"
                    + hashlib.sha256(
                        fact_text.casefold().encode("utf-8")
                    ).hexdigest()[:24]
                )
            try:
                confidence = float(item.get("confidence"))
            except (TypeError, ValueError):
                raise ConflictError(
                    "live_research_fact_confidence_invalid",
                    "Fact confidence must be between 0 and 1",
                ) from None
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ConflictError(
                    "live_research_fact_confidence_invalid",
                    "Fact confidence must be between 0 and 1",
                )

            source_refs = item.get("source_refs")
            if not isinstance(source_refs, list) or not source_refs:
                raise ConflictError(
                    "live_research_fact_sources_required",
                    "Each saved fact needs source_refs from the latest search",
                )
            refs: list[str] = []
            for raw_ref in source_refs[:8]:
                source_ref = str(raw_ref or "")
                if source_ref not in source_map:
                    raise ConflictError(
                        "live_research_fact_source_unknown",
                        "A fact referenced a source_ref without evidence in the latest search result",
                    )
                if source_ref not in refs:
                    refs.append(source_ref)

            raw_evidence_refs = item.get("evidence_refs")
            if (
                not isinstance(raw_evidence_refs, list)
                or not raw_evidence_refs
            ):
                raise ConflictError(
                    "live_research_fact_evidence_required",
                    "Each saved fact needs exact evidence_refs from the latest search",
                )
            selected_supports: dict[str, list[dict[str, Any]]] = {}
            selected_evidence_refs: list[str] = []
            for raw_evidence_ref in raw_evidence_refs[:24]:
                evidence_ref = str(raw_evidence_ref or "").strip()
                bound = evidence_map.get(evidence_ref)
                if bound is None:
                    raise ConflictError(
                        "live_research_fact_evidence_unknown",
                        "A fact referenced an evidence_ref that was not returned by the latest search",
                    )
                source_ref, support = bound
                if source_ref not in refs:
                    raise ConflictError(
                        "live_research_fact_evidence_source_mismatch",
                        "Every evidence_ref must belong to one of the fact's source_refs",
                    )
                if evidence_ref not in selected_evidence_refs:
                    selected_evidence_refs.append(evidence_ref)
                    selected_supports.setdefault(
                        source_ref,
                        [],
                    ).append(support)

            if any(
                source_ref not in selected_supports
                for source_ref in refs
            ):
                raise ConflictError(
                    "live_research_fact_source_without_evidence",
                    "Every fact source_ref must have at least one selected evidence_ref",
                )

            explicit_existing = str(
                item.get("existing_fact_id") or ""
            ).strip()
            provisional_fact_id = (
                explicit_existing
                if explicit_existing in known_by_id
                else candidate_assertion_id(
                    claim_key,
                    fact_text,
                )
            )
            normalized_candidates.append(
                {
                    "fact_id": provisional_fact_id,
                    "existing_fact_id": (
                        explicit_existing
                        if explicit_existing in known_by_id
                        else None
                    ),
                    "claim_key": claim_key,
                    "text": fact_text,
                    "confidence": confidence,
                    "evidence_supported": True,
                    "live_review": {
                        key: item.get(key) for key in ('verdict', 'atomic', 'support_complete', 'qualifiers_preserved', 'review_reason')
                    } if args.get('batch_reviewed') is True else None,
                    "selected": bool(item.get("selected")),
                    "evidence_refs": selected_evidence_refs,
                    "sources": [
                        {
                            **source_map[source_ref],
                            "supports": selected_supports[source_ref],
                        }
                        for source_ref in refs
                    ],
                }
            )

        save_audit: dict[str, Any] = {
            "raw_candidate_count": len(raw_facts),
            "structurally_valid_count": len(normalized_candidates),
            "normalized_fact_count": 0,
            "reconciled_match_count": 0,
            "rejected": {},
        }
        if not normalized_candidates and not chunk_id:
            raise ConflictError(
                "live_research_facts_empty",
                "No valid facts were supplied",
            )

        reconciliation_matches: dict[int, str] = {
            index: str(item.get("existing_fact_id") or "")
            for index, item in enumerate(normalized_candidates)
            if str(item.get("existing_fact_id") or "") in known_by_id
        }
        reconciliation_decisions: list[dict[str, Any]] = [
            {
                "incoming_index": index,
                "relation": "equivalent",
                "existing_fact_id": fact_id,
                "rationale": (
                    "Live extraction explicitly referenced this durable fact ID."
                ),
                "model_name": "upstream_existing_fact_id",
                "prompt_version": "fact-identity-reconciliation-v1",
            }
            for index, fact_id in sorted(
                reconciliation_matches.items()
            )
        ]
        reconciliation_meta: dict[str, Any] = {
            "status": "not_needed",
            "pages_reviewed": 0,
            "existing_fact_count": len(known_facts),
            "incoming_fact_count": len(normalized_candidates),
            "matched_count": len(reconciliation_matches),
            "unmatched_count": (
                len(normalized_candidates)
                - len(reconciliation_matches)
            ),
        }

        reconciler = getattr(
            self.service.providers.gemini,
            "reconcile_fact_identities",
            None,
        )
        if (
            normalized_candidates
            and known_facts
            and callable(reconciler)
            and args.get("inventory_reviewed") is not True
            and not session.state.get('live_first_research')
        ):
            try:
                reconciliation = await reconciler(
                    normalized_candidates,
                    known_facts,
                )
                reconciliation_matches = {
                    int(index): str(fact_id)
                    for index, fact_id in (
                        reconciliation.get("matches") or {}
                    ).items()
                    if str(fact_id) in known_by_id
                }
                reconciliation_decisions = [
                    dict(item)
                    for item in (
                        reconciliation.get("decisions") or []
                    )
                    if isinstance(item, dict)
                ]
                reconciliation_meta = {
                    "status": (
                        "complete"
                        if reconciliation.get("complete") is True
                        else "partial"
                    ),
                    "pages_reviewed": int(
                        reconciliation.get("pages_reviewed") or 0
                    ),
                    "existing_fact_count": int(
                        reconciliation.get(
                            "existing_fact_count"
                        )
                        or len(known_facts)
                    ),
                    "incoming_fact_count": int(
                        reconciliation.get(
                            "incoming_fact_count"
                        )
                        or len(normalized_candidates)
                    ),
                    "matched_count": len(
                        reconciliation_matches
                    ),
                    "unmatched_count": int(
                        reconciliation.get("unmatched_count") or 0
                    ),
                }
            except (
                GeminiUnavailable,
                MalformedProviderResponse,
                PermanentProviderError,
                RetryableProviderError,
            ) as exc:
                reconciliation_meta = {
                    **reconciliation_meta,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
        elif normalized_candidates and known_facts and args.get("inventory_reviewed") is True:
            reconciliation_meta["status"] = "mira_live_inventory_review"
        elif normalized_candidates and known_facts:
            reconciliation_meta["status"] = (
                "compatibility_unavailable"
            )

        reconciled_candidates: list[dict[str, Any]] = []
        for index, candidate in enumerate(
            normalized_candidates
        ):
            matched_id = reconciliation_matches.get(index)
            if matched_id in known_by_id:
                fact_id = str(matched_id)
                existing_fact_id = fact_id
            else:
                existing_fact_id = str(
                    candidate.get("existing_fact_id") or ""
                ).strip()
                if existing_fact_id not in known_by_id:
                    existing_fact_id = ""
                fact_id = (
                    existing_fact_id
                    if existing_fact_id
                    else candidate_assertion_id(
                        candidate.get("claim_key"),
                        candidate.get("text"),
                    )
                )
            reconciled_candidates.append(
                {
                    **candidate,
                    "fact_id": fact_id,
                    "existing_fact_id": (
                        existing_fact_id or None
                    ),
                    "selected": (
                        bool(candidate.get("selected"))
                        and prior_decisions.get(
                            fact_id,
                            True,
                        )
                    ),
                }
            )

        normalized = merge_model_fact_inventory(
            reconciled_candidates
        )
        if not normalized and not chunk_id:
            raise ConflictError(
                "live_research_facts_empty",
                "No valid facts were supplied",
            )
        save_audit["normalized_fact_count"] = len(normalized)
        save_audit["reconciled_match_count"] = len(
            reconciliation_matches
        )
        save_audit["collapsed_in_batch_count"] = max(
            0,
            len(normalized_candidates) - len(normalized),
        )

        # Phase 2 commits only if the story/search snapshot has not changed while
        # semantic reconciliation was in flight.
        with self.service.store.tx() as db:
            current_story = self.service._story_row(
                db,
                story_id,
            )
            current_research = json.loads(
                current_story["research_json"] or "{}"
            )
            current_history = current_research.get(
                "live_web_searches"
            )
            current_match = [
                (index, dict(item))
                for index, item in enumerate(current_history or [])
                if isinstance(item, dict)
                and str(item.get("research_run_id") or "").strip()
                == snapshot_search_run_id
            ]
            if len(current_match) != 1:
                raise ConflictError(
                    "live_research_save_stale",
                    "Research run changed while semantic reconciliation was running.",
                )
            current_history_index, current_latest = current_match[0]
            current_search_run_id = str(
                current_latest.get("research_run_id") or ""
            ).strip()
            current_batch_id = str(
                current_latest.get("save_batch_id") or ""
            ).strip() or expected_save_batch_id
            if (
                int(current_story["revision"] or 0)
                != snapshot_revision
                or int(
                    current_research.get(
                        "identity_generation"
                    )
                    or 0
                )
                != snapshot_identity_generation
                or current_search_run_id
                != snapshot_search_run_id
                or (not chunk_id and current_batch_id != save_batch_id)
            ):
                raise ConflictError(
                    "live_research_save_stale",
                    "Story, identity or research result changed while semantic reconciliation was running",
                )

            commit_run = db.execute(
                "SELECT state,identity_generation FROM research_runs WHERE run_id=? AND story_id=?",
                (run_id, story_id),
            ).fetchone()
            if commit_run is None or str(commit_run["state"]) in {"cancelled", "failed", "completed"}:
                raise ConflictError("live_research_run_terminal", "Research is no longer writable.")

            self._research_run_guard(db, session, run_id)
            history = current_history
            history_index = current_history_index
            latest_search = current_latest
            research = current_research
            story = current_story
            now = self.service.store.now()
            from .poi_memory import memory_keys
            keys = memory_keys(db, identity)
            placeholders = ','.join('?' for _ in keys)
            prior_poi_ids = {str(row[0]) for row in db.execute(
                f'SELECT assertion_id FROM poi_research_assertions WHERE poi_key IN ({placeholders})', tuple(keys))}
            prior_poi_ids.update(str(row[0]) for row in db.execute('SELECT assertion_id FROM fact_assertions WHERE story_id=?', (story_id,)))
            def support_keys(sources):
                return {(str(span.get('source_url') or source.get('url') or '').rstrip('/'),
                         str(span.get('source_version_id') or source.get('source_version_id') or ''),
                         str(span.get('kind') or 'support'), str(span.get('text') or '').strip())
                        for source in sources for span in source.get('supports') or []
                        if isinstance(span, dict) and str(span.get('text') or '').strip()}

            prior_supports: dict[str, set] = {}
            for fact in normalized:
                fact_id = fact['fact_id']
                rows = db.execute(
                    f'SELECT sources_json FROM poi_research_assertions WHERE poi_key IN ({placeholders}) AND assertion_id=? '
                    'UNION ALL SELECT sources_json FROM facts WHERE story_id=? AND fact_id=?',
                    (*keys, fact_id, story_id, fact_id))
                prior_supports[fact_id] = set().union(*(support_keys(json.loads(row[0])) for row in rows))

            persist_fact_candidates(
                db,
                story_id=story_id,
                poi_key=(
                    str(identity.get("candidate_id") or "")
                    or None
                ),
                facts=normalized,
                run_id=run_id,
                batch_id=save_batch_id,
                model_name=str(session.model),
                prompt_version=(
                    "live-discovery-save-ledger-v2"
                ),
                now=now,
            )
            batch_verified = session.state.get('live_first_research') and args.get('batch_reviewed') is True
            if batch_verified:
                accepted_bundle = {}
                for fact in normalized:
                    review = fact.get('live_review') or {}
                    if review.get('verdict') not in {'supported', 'insufficient', 'contradicted', 'possible_conflict'} or any(type(review.get(flag)) is not bool for flag in ('atomic', 'support_complete', 'qualifiers_preserved')) or not isinstance(review.get('review_reason'), str) or not 1 <= len(review['review_reason']) <= 500:
                        raise ConflictError('live_research_review_invalid', 'Each finding needs a scoped model verdict, three boolean checks and a brief reason.')
                    positive = review['verdict'] == 'supported' and all(review[flag] for flag in ('atomic', 'support_complete', 'qualifiers_preserved'))
                    if positive:
                        assertion = db.execute('SELECT revision_digest FROM fact_assertions WHERE story_id=? AND assertion_id=?', (story_id, fact['fact_id'])).fetchone()
                        accepted_bundle[fact['fact_id']] = assertion['revision_digest']
                    else:
                        db.execute("UPDATE fact_assertions SET eligibility='withheld',review_status='withheld',owner_selected=0 WHERE story_id=? AND assertion_id=? AND review_status<>'quarantined'", (story_id, fact['fact_id']))
                        db.execute('UPDATE facts SET selected=0 WHERE story_id=? AND fact_id=?', (story_id, fact['fact_id']))
                if accepted_bundle:
                    record_fact_review_scan(self.service, story_id, str(identity.get('candidate_id') or '') or None,
                                            detector='mira_live_batch', run_id=run_id, revision_bundle=accepted_bundle,
                                            conflict_ids=[], coverage_complete=True, missing_aspects=[], connection=db)
                refresh_review_status(db, story_id, now)
            if chunk_id:
                record_chunk_batch(db, run_id=run_id, chunk_id=chunk_id, batch_index=batch_index,
                    status="continuation" if continuation_needed else "completed", raw_fact_count=len(raw_facts),
                    accepted_fact_count=len(normalized), continuation_needed=continuation_needed,
                    continuation_reason="mira_live_remaining_findings" if continuation_needed else "",
                    model_name=str(session.model), prompt_version="live-chunk-findings-v1", now=now,
                    payload={"facts": normalized, "no_claims": not normalized, "official_source_urls": [],
                             "source_content_valid": args.get('source_content_valid') is not False,
                             "next_passage_cursor": session.state.get('research_pending_page', {}).get(chunk_id, 0) if continuation_needed else 0,
                             "read_passage_ids": sorted(session.state.get('research_passages_seen', {}).get(chunk_id, set()))})
                mark_chunk(db, run_id=run_id, chunk_id=chunk_id,
                    status="deferred" if continuation_needed else "extracted" if normalized else "no_claims",
                    observation_count=len(chunk_checkpoint(db, run_id, chunk_id)["facts"]),
                    model_name=str(session.model), prompt_version="live-chunk-findings-v1", now=now,
                    error_code='not_article_text' if args.get('source_content_valid') is False else None)
                if args.get('source_content_valid') is False:
                    db.execute("UPDATE research_run_sources SET status='deferred',error_code='not_article_text' WHERE run_id=? AND source_version_id=?",
                               (run_id, chunk_row['source_version_id']))
            if reconciliation_decisions:
                persist_fact_relation_events(
                    db,
                    story_id=story_id,
                    run_id=run_id,
                    incoming_facts=normalized_candidates,
                    decisions=reconciliation_decisions,
                    now=now,
                )

            from .poi_memory import persist_research_memory, sync_poi_review_from_story
            persist_research_memory(
                db,
                identity,
                normalized,
                list(source_map.values()),
                str(latest_search.get("query") or ""),
                now,
                research_run_id=run_id,
            )
            if batch_verified:
                sync_poi_review_from_story(db, story_id, now)
            eligible_ids = {str(row[0]) for row in db.execute(
                'SELECT assertion_id FROM fact_assertions WHERE story_id=? AND eligibility=\'eligible\'', (story_id,))}
            saved_ids = {str(f['fact_id']) for f in normalized}
            added_supports = {f['fact_id']: support_keys(f.get('sources') or []) - prior_supports[f['fact_id']]
                              for f in normalized if f['fact_id'] in prior_poi_ids}
            save_audit.update(new_eligible_claim_count=len((saved_ids - prior_poi_ids) & eligible_ids),
                              evidence_to_existing_claim_count=sum(bool(spans) for spans in added_supports.values()),
                              new_evidence_span_count=sum(len(spans) for spans in added_supports.values()),
                              reused_fact_count=sum(not spans for spans in added_supports.values()),
                              withheld_or_insufficient_count=len(saved_ids - eligible_ids))
            manifest_counts = run_manifest(db, run_id)['counts']
            save_audit.update(skipped_completed_chunks=manifest_counts['chunks_skipped_completed'],
                              resumed_chunks=manifest_counts['chunks_resumed_partial'])
            fact_count = db.execute(
                "SELECT COUNT(*) FROM facts WHERE story_id=?",
                (story_id,),
            ).fetchone()[0]

            selected_ids = [
                row["fact_id"]
                for row in db.execute(
                    "SELECT a.assertion_id AS fact_id FROM fact_assertions a "
                    "JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id "
                    "WHERE a.story_id=? AND a.owner_selected=1 AND f.evidence_supported=1 "
                    "ORDER BY f.rowid",
                    (story_id,),
                )
            ]
            research["claim_decisions"] = {
                row["fact_id"]: bool(row["owner_selected"])
                for row in db.execute(
                    "SELECT assertion_id AS fact_id,owner_selected FROM fact_assertions "
                    "WHERE story_id=? ORDER BY rowid",
                    (story_id,),
                )
            }
            research["image_notes"] = "\n".join(
                str(row["text"])
                for row in db.execute(
                    "SELECT f.text FROM facts f JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? AND a.owner_selected=1 AND a.eligibility='eligible' "
                    "AND f.evidence_supported=1 ORDER BY f.rowid LIMIT 6",
                    (story_id,),
                )
            )

            new_selected = set(selected_ids)
            if new_selected != old_selected:
                research["draft_needs_refresh"] = True
                self.service._mark_visual_stale(
                    db,
                    story,
                    selected_ids,
                )

            latest_search["semantic_completion"] = "mira_live"
            latest_search["mira_saved_fact_ids"] = [
                item["fact_id"]
                for item in normalized
            ]
            latest_search["save_research_audit"] = save_audit
            latest_search["fact_reconciliation"] = (
                reconciliation_meta
            )
            history[history_index] = latest_search
            research["live_web_searches"] = history[-12:]
            progress = self._live_research_progress(db, story_id, run_id)
            more_sources_required = not progress['observations'] and progress['remaining'] and progress['attempts'] < 3
            batches_complete = bool(batch_verified and manifest_complete(run_manifest(db, run_id)) and not more_sources_required and (not progress['remaining'] or progress['attempts'] >= 3))
            set_run_state(
                db,
                run_id,
                "completed" if batches_complete else "extracting" if batch_verified else "verifying",
                detail="live_batches_complete" if batches_complete else "live_batches_in_progress" if batch_verified else "awaiting_live_semantic_review",
                now=now,
                completed=batches_complete,
            )
            db.execute(
                "UPDATE stories SET research_json=?,error_code=NULL,error_message=NULL,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (
                    canonical(research),
                    self.service.store.now(),
                    story_id,
                ),
            )
            for fact in normalized:
                assertion = db.execute("SELECT revision_digest FROM fact_assertions WHERE story_id=? AND assertion_id=?", (story_id, fact["fact_id"])).fetchone()
                fact["revision_digest"] = assertion["revision_digest"] if assertion else None
                fact["supporting_evidence_ids"] = [row["evidence_id"] for row in db.execute("SELECT e.evidence_id FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted'", (story_id, fact["fact_id"]))]
            result = {
                "research_run_id": run_id,
                "save_batch_id": save_batch_id,
                "review_required": not bool(batch_verified),
                "completed": batches_complete,
                "continuation_required": not batches_complete,
                "next_tool": None if batches_complete else "get_research_chunk" if chunk_id or batch_verified else "get_review_packet",
                "next_args": {"run_id": run_id},
                "chunk_id": chunk_id or None,
                "payload_saved": True,
                "facts": normalized,
                "selected_fact_ids": selected_ids,
                "save_research_audit": save_audit,
                "fact_reconciliation": reconciliation_meta,
                "story": self.service._story_repr(
                    db,
                    self.service._story_row(
                        db,
                        story_id,
                    ),
                ),
            }
            self._store_command(
                db,
                story_id,
                command_id,
                "save_research_facts",
                canonical_args,
                result,
            )
            if command_id != save_batch_id:
                self._store_command(db, story_id, save_batch_id, "save_research_facts", canonical_args, result)
            all_research_sources = [
                source
                for source in (
                    research.get("grounding_sources") or []
                )
                if isinstance(source, dict)
            ]

        session.state["pending_discovery_redirects"] = 0
        if chunk_id:
            session.state.setdefault("research_page_cursor", {})[chunk_id] = session.state.get("research_pending_page", {}).get(chunk_id, 0)
        record_live_diagnostic(
            self.service,
            story_id,
            session.id,
            "backend",
            "fact_save_audit",
            {
                **save_audit,
                "reconciliation_status": (
                    reconciliation_meta.get("status")
                ),
                "pages_reviewed": int(
                    reconciliation_meta.get(
                        "pages_reviewed"
                    )
                    or 0
                ),
            },
        )
        self._emit_research_progress(
            session,
            stage="completed" if batches_complete else "extracting" if batch_verified else "review",
            active=not batches_complete,
            query=str(latest_search.get("query") or ""),
            source_count=len(all_research_sources),
            fact_count=int(fact_count),
            sources=all_research_sources,
        )
        return result

    def _finalize_fact_review(
        self,
        session,
        command_id: str,
        args: dict[str, Any],
        *, packet_rejected=None,
    ) -> dict[str, Any]:
        story_id = session.resource_id
        run_id = _bounded_text(args.get("run_id"), 160, required=True)
        raw_reviewed = args.get("reviewed_assertions")
        raw_conflicts = args.get("conflicts")
        raw_missing = args.get("missing_aspects")
        if not isinstance(raw_reviewed, list) or (packet_rejected is None and len(raw_reviewed) > 240):
            raise ConflictError(
                "live_fact_review_bundle_invalid",
                "reviewed_assertions must be a bounded array of exact fact revisions",
            )
        if not isinstance(raw_conflicts, list) or len(raw_conflicts) > 80:
            raise ConflictError(
                "live_fact_review_conflicts_invalid",
                "conflicts must be an array; an empty array is valid after a full review",
            )
        if not isinstance(raw_missing, list) or len(raw_missing) > 40:
            raise ConflictError(
                "live_fact_review_coverage_invalid",
                "missing_aspects must be a bounded array",
            )
        coverage_complete = args.get("coverage_complete")
        if not isinstance(coverage_complete, bool):
            raise ConflictError(
                "live_fact_review_coverage_invalid",
                "coverage_complete must be boolean",
            )
        missing_aspects = [
            _bounded_text(value, 500, required=True)
            for value in raw_missing
        ]

        supplied_bundle: dict[str, str] = {}
        supporting_ids: dict[str, list[str]] = {}
        for item in raw_reviewed:
            if not isinstance(item, dict):
                raise ConflictError(
                    "live_fact_review_bundle_invalid",
                    "Every reviewed assertion must include fact_id and revision_digest",
                )
            fact_id = _bounded_text(item.get("fact_id"), 160, required=True)
            revision_digest = _bounded_text(
                item.get("revision_digest"),
                200,
                required=True,
            )
            if fact_id in supplied_bundle:
                raise ConflictError(
                    "live_fact_review_bundle_invalid",
                    "reviewed_assertions contains a duplicate fact_id",
                )
            supplied_bundle[fact_id] = revision_digest
            raw_evidence = item.get("supporting_evidence_ids")
            minimum_evidence = 0 if packet_rejected is not None and fact_id in packet_rejected else 1
            if not isinstance(raw_evidence, list) or not minimum_evidence <= len(raw_evidence) <= 32:
                raise ConflictError("live_fact_review_evidence_required", "Each reviewed assertion needs exact supporting evidence IDs from get_evidence.")
            supporting_ids[fact_id] = [_bounded_text(value, 160, required=True) for value in raw_evidence]

        with self.service.store.connection() as db:
            run = db.execute(
                "SELECT * FROM research_runs WHERE run_id=? AND story_id=?",
                (run_id, story_id),
            ).fetchone()
            if run is None:
                raise ConflictError(
                    "live_research_run_unknown",
                    "Research run does not belong to the current story.",
                )
            if str(run["state"]) in {"cancelled", "failed"}:
                raise ConflictError(
                    "live_research_run_terminal",
                    "Cancelled or failed research cannot be finalized.",
                )
            story = self.service._story_row(db, story_id)
            snapshot_revision = int(story["revision"] or 0)
            research = json.loads(story["research_json"] or "{}")
            identity_generation = int(
                research.get("identity_generation") or 0
            )
            if identity_generation != int(run["identity_generation"] or 0):
                raise ConflictError(
                    "live_fact_review_stale",
                    "Object identity changed; this research review is stale.",
                )
            current_bundle = review_packets.bundle(db, story_id)
            current_items = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": "",
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM facts WHERE story_id=? "
                    "AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
            identity = (
                research.get("visual_identity")
                if isinstance(research.get("visual_identity"), dict)
                else {}
            )

        if supplied_bundle != current_bundle:
            changed = sorted(
                set(supplied_bundle) | set(current_bundle)
            )
            issues = [
                {
                    "fact_id": fact_id,
                    "expected_revision": supplied_bundle.get(fact_id),
                    "actual_revision": current_bundle.get(fact_id),
                }
                for fact_id in changed
                if supplied_bundle.get(fact_id) != current_bundle.get(fact_id)
            ]
            raise ConflictError(
                "live_fact_review_stale",
                "Call get_facts and get_evidence, then retry with their EXACT IDs/digests. Never invent them. Reviewed inventory is stale or incomplete: "
                + json.dumps(issues[:20], ensure_ascii=False),
            )

        model_items = conflict_scan_items(current_items)
        normalized_input = []
        for item in raw_conflicts:
            if not isinstance(item, dict):
                raise ConflictError(
                    "live_fact_review_conflicts_invalid",
                    "Each conflict must be an object",
                )
            normalized_input.append(
                {
                    **item,
                    "suggested_resolution": item.get("resolution"),
                }
            )
        records = normalize_model_conflict_records(
            model_items,
            {"conflicts": normalized_input},
        )
        if len(records) != len(raw_conflicts):
            raise ConflictError(
                "live_fact_review_conflicts_invalid",
                "Conflicts must reference distinct facts from the exact reviewed bundle",
            )

        with self.service.store.tx() as db:
            run = db.execute(
                "SELECT * FROM research_runs WHERE run_id=? AND story_id=?",
                (run_id, story_id),
            ).fetchone()
            if coverage_complete:
                pending = db.execute("SELECT COUNT(*) FROM research_chunk_runs WHERE run_id=? AND status NOT IN ('extracted','no_claims')", (run_id,)).fetchone()[0]
                if pending:
                    raise ConflictError("live_research_chunks_incomplete", "Read and save ALL remaining chunks via get_research_chunk before claiming complete coverage. Or explicitly return partial coverage with missing_aspects.")
            story = self.service._story_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            if (
                run is None
                or str(run["state"]) in {"cancelled", "failed"}
                or int(story["revision"] or 0) != snapshot_revision
                or int(research.get("identity_generation") or 0)
                != int(run["identity_generation"] or 0)
            ):
                raise ConflictError(
                    "live_fact_review_stale",
                    "Research identity changed before review commit.",
                )
            current_after = review_packets.bundle(db, story_id)
            if current_after != current_bundle:
                raise ConflictError(
                    "live_fact_review_stale",
                    "Fact revisions changed before review commit.",
                )
            for fact_id, evidence_ids in supporting_ids.items():
                valid_ids = {str(row["evidence_id"]) for row in db.execute(
                    "SELECT e.evidence_id FROM fact_evidence_spans e JOIN fact_observations o "
                    "ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted'",
                    (story_id, fact_id),
                )}
                if not set(evidence_ids).issubset(valid_ids):
                    raise ConflictError("live_fact_review_evidence_invalid", "Evidence does not support this exact assertion scope.")
            if packet_rejected is not None:
                review_packets.load(self, session, db, str(args["packet_ref"]))
                db.execute("UPDATE live_review_attempts SET state='finished' WHERE packet_ref=?", (str(args["packet_ref"]),))
                for fact_id in packet_rejected:
                    db.execute("UPDATE fact_assertions SET review_status='withheld',eligibility='withheld' WHERE story_id=? AND assertion_id=? AND review_status<>'quarantined'", (story_id, fact_id))
            poi_key = str(identity.get("candidate_id") or "") or None
            if records:
                persist_fact_conflicts(
                    self.service,
                    story_id,
                    poi_key,
                    records,
                    detector="mira_live_review",
                    connection=db,
                )
                for record in records:
                    resolve_fact_conflict(
                        self.service,
                        story_id,
                        str(record["conflict_id"]),
                        str(record["suggested_resolution"]),
                        str(record["detector_rationale"] or "Mira semantic review"),
                        float(record["detector_confidence"]),
                        arbitrated_by="mira_live_review",
                        connection=db,
                    )

            conflict_ids = [
                str(record["conflict_id"])
                for record in records
            ]
            record_fact_review_scan(
                self.service,
                story_id,
                poi_key,
                detector="mira_live_review",
                connection=db,
                run_id=run_id,
                revision_bundle=current_bundle,
                conflict_ids=conflict_ids,
                # The exact assertion bundle was reviewed even when the broader
                # research goal still has missing aspects. Keep those independent.
                coverage_complete=True,
                missing_aspects=missing_aspects,
            )

            self._research_run_guard(db, session, run_id)
            now = self.service.store.now()
            refresh_review_status(db, story_id, now)
            manifest = run_manifest(db, run_id)
            complete = bool(
                coverage_complete
                and not missing_aspects
                and manifest_complete(manifest)
            )
            set_run_state(
                db,
                run_id,
                "completed" if complete else "partial",
                detail=(
                    "live_review_complete"
                    if complete
                    else "live_review_partial"
                ),
                now=now,
                completed=complete,
            )
            eligible_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM fact_assertions "
                    "WHERE story_id=? AND eligibility='eligible'",
                    (story_id,),
                ).fetchone()[0]
            )
            withheld_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM fact_assertions "
                    "WHERE story_id=? AND eligibility='withheld'",
                    (story_id,),
                ).fetchone()[0]
            )
            unreviewed_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM fact_assertions "
                    "WHERE story_id=? AND eligibility='unreviewed'",
                    (story_id,),
                ).fetchone()[0]
            )
            history = (
                list(research.get("live_web_searches"))
                if isinstance(research.get("live_web_searches"), list)
                else []
            )
            for index, item in enumerate(history):
                if (
                    isinstance(item, dict)
                    and str(item.get("research_run_id") or "") == run_id
                ):
                    history[index] = {
                        **item,
                        "review_complete": complete,
                        "review_coverage_complete": coverage_complete,
                        "review_missing_aspects": missing_aspects,
                        "review_conflict_ids": conflict_ids,
                    }
            research["live_web_searches"] = history[-12:]
            research["fact_review"] = {
                "run_id": run_id,
                "complete": complete,
                "coverage_complete": coverage_complete,
                "missing_aspects": missing_aspects,
                "conflict_ids": conflict_ids,
                "reviewed_assertion_count": len(current_bundle),
                "eligible_count": eligible_count,
                "withheld_count": withheld_count,
                "unreviewed_count": unreviewed_count,
            }
            db.execute(
                "UPDATE stories SET research_json=?,revision=revision+1,updated_at=? "
                "WHERE id=?",
                (canonical(research), now, story_id),
            )
            result = {
                "research_run_id": run_id,
                "complete": complete,
                "coverage_complete": coverage_complete,
                "missing_aspects": missing_aspects,
                "reviewed_assertion_count": len(current_bundle),
                "conflict_ids": conflict_ids,
                "eligible_count": eligible_count,
                "withheld_count": withheld_count,
                "unreviewed_count": unreviewed_count,
                "research_manifest": manifest,
                "story": self.service._story_repr(
                    db,
                    self.service._story_row(db, story_id),
                ),
            }
            if packet_rejected is not None:
                db.execute("UPDATE live_review_packets SET result_json=?,request_json=? WHERE packet_ref=?", (canonical(result), canonical(args["_packet_request"]), args["packet_ref"]))
            self._store_command(
                db,
                story_id,
                command_id,
                "finalize_fact_review",
                args,
                result,
            )

        record_live_diagnostic(self.service, story_id, session.id, "backend", "fact_review_committed", {
            "run_id": run_id, "complete": complete, "eligible_count": eligible_count,
            "withheld_count": withheld_count, "unreviewed_count": unreviewed_count,
        })
        logger.info("street_story_fact_review_committed %s", canonical({
            "story_id": story_id, "session_id": session.id, "run_id": run_id,
            "model": "mira_live_review", "complete": complete,
            "eligible_count": eligible_count, "withheld_count": withheld_count,
        }))
        self._emit_research_progress(
            session,
            stage="facts",
            active=False,
            query="",
            source_count=int(
                result["story"].get("source_count") or 0
            ),
            fact_count=(
                int(result["eligible_count"])
                + int(result["withheld_count"])
                + int(result["unreviewed_count"])
            ),
            sources=[],
        )
        return result


    def _record_fact_conflicts(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        raw_conflicts = args.get("conflicts")
        if not isinstance(raw_conflicts, list) or not 1 <= len(raw_conflicts) <= 12:
            raise ConflictError("live_fact_conflicts_invalid", "Provide between 1 and 12 model-detected conflicts")

        with self.service.store.connection() as db:
            research = json.loads(self.service._story_row(db, story_id)["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            items = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": "",
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM facts WHERE story_id=? AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
        model_items = conflict_scan_items(items)
        records = normalize_model_conflict_records(model_items, {"conflicts": raw_conflicts})
        if len(records) != len(raw_conflicts):
            raise ConflictError(
                "live_fact_conflicts_invalid",
                "Conflict rows must reference distinct current evidence-backed fact IDs and valid relations",
            )
        durable = persist_fact_conflicts(
            self.service,
            story_id,
            str(identity.get("candidate_id") or "") or None,
            records,
            detector="mira_live",
        )
        record_ids = {record["conflict_id"] for record in records}
        result = {
            "fact_conflicts": [item for item in durable if item.get("conflict_id") in record_ids],
            "recorded_count": len(records),
        }
        with self.service.store.tx() as db:
            refresh_review_status(db, story_id, self.service.store.now())
            self._store_command(db, story_id, command_id, "record_fact_conflicts", args, result)
        return result

    def _resolve_fact_conflict(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        conflict_id = _bounded_text(args.get("conflict_id"), 120, required=True)
        resolution = _bounded_text(args.get("resolution"), 40, required=True)
        reason = _bounded_text(args.get("reason"), 1000, required=True)
        try:
            confidence = float(args.get("confidence", 0.0))
        except (TypeError, ValueError):
            raise ConflictError(
                "fact_conflict_confidence_invalid", "Confidence must be between 0 and 1"
            ) from None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ConflictError(
                "fact_conflict_confidence_invalid", "Confidence must be between 0 and 1"
            )
        try:
            resolved = resolve_fact_conflict(
                self.service,
                story_id,
                conflict_id,
                resolution,
                reason,
                confidence,
                arbitrated_by="mira",
            )
        except KeyError:
            raise ConflictError(
                "fact_conflict_unknown", "Conflict is not present in the current topic"
            ) from None
        except ValueError as exc:
            raise ConflictError(
                "fact_conflict_resolution_invalid", str(exc)
            ) from None
        result = {
            "conflict": resolved,
            "resolution": resolved.get("final_resolution"),
            "preferred_fact_id": resolved.get("final_fact_id"),
            "fact_conflicts": self._topic_state(story_id).get("fact_conflicts", [])[:12],
        }
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "resolve_fact_conflict", args, result)
        # The shared arbitration ledger emits the durable fact_conflict_arbitrated
        # telemetry event exactly once. Avoid duplicating it in the Live adapter.
        return result

    def _select_facts(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        ids = [str(v) for v in args.get("fact_ids", [])]
        if len(ids) > 40 or len(set(ids)) != len(ids):
            raise ConflictError("live_fact_selection_invalid", "Fact selection is invalid")
        with self.service.store.tx() as db:
            story, _editor = self._editor_row(db, story_id)
            facts = {row["fact_id"]: row for row in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))}
            unknown = set(ids) - set(facts)
            if unknown:
                raise ConflictError("fact_id_unknown", f"Unknown fact ids: {sorted(unknown)}")
            set_owner_selection(db, story_id, ids, self.service.store.now())
            research = json.loads(story["research_json"] or "{}")
            research["claim_decisions"] = {
                row["fact_id"]: bool(row["selected"])
                for row in db.execute("SELECT fact_id,selected FROM facts WHERE story_id=?", (story_id,))
            }
            selected_text = [
                str(row["text"])
                for row in db.execute(
                    "SELECT f.text FROM facts f JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? AND a.owner_selected=1 AND a.eligibility='eligible' "
                    "AND f.evidence_supported=1 ORDER BY f.rowid",
                    (story_id,),
                )
            ]
            research["image_notes"] = "\n".join(selected_text[:6])
            research["draft_needs_refresh"] = True
            self.service._mark_visual_stale(db, story, ids)
            db.execute(
                "UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.service.store.now(), story_id),
            )
            result = {
                "selected_fact_ids": [
                    row["fact_id"]
                    for row in db.execute(
                        "SELECT assertion_id AS fact_id FROM fact_assertions "
                        "WHERE story_id=? AND owner_selected=1 ORDER BY rowid",
                        (story_id,),
                    )
                ],
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "select_facts", args, result)
            return result

    def _set_concept(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        concept = _bounded_text(args.get("concept"), 1200, required=True)
        with self.service.store.tx() as db:
            story, _editor = self._editor_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            previous = str(research.get("publication_concept") or "")
            research["publication_concept"] = concept
            if concept != previous:
                research["draft_needs_refresh"] = True
            context = json.loads(story["visual_context_json"] or "{}")
            state = str(story["state"] or "")
            clear_visual = bool(context) and concept != previous and state not in {"scheduled", "published"}
            if clear_visual:
                context["stale"] = True
                context["stale_reason"] = "publication_concept_changed"
            db.execute(
                "UPDATE stories SET research_json=?,visual_context_json=?,"
                "vibepublish_asset_ref=CASE WHEN ? THEN NULL ELSE vibepublish_asset_ref END,"
                "processed_image_url=CASE WHEN ? THEN NULL ELSE processed_image_url END,"
                "state=CASE WHEN ? THEN 'needs_review' ELSE state END,"
                "error_code=CASE WHEN ? THEN 'visual_stale' ELSE error_code END,"
                "error_message=CASE WHEN ? THEN 'Концепция публикации изменилась; изображение нужно обновить.' ELSE error_message END,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (
                    canonical(research),
                    canonical(context),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "publication_concept": concept,
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "set_concept", args, result)
            return result

    @staticmethod
    def _literal_spans(raw: str) -> list[dict[str, Any]]:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []

    def _edit_text(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        expected = int(args.get("expected_text_revision", -1))
        new_text = _bounded_text(args.get("new_text"), 5000, required=True)
        summary = _bounded_text(args.get("change_summary"), 240, required=True)
        allow_literals = bool(args.get("allow_literal_changes", False))
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            current_revision = int(editor["text_revision"])
            if expected != current_revision:
                raise ConflictError(
                    "live_text_revision_conflict",
                    f"Expected text revision {expected}, current is {current_revision}",
                )
            issues = selected_eligibility_issues(db, story_id)
            if issues:
                raise ConflictError("fact_review_required", "Review selected facts before composing a draft: " + canonical(issues[:12]))
            current_text = str(story["draft_text"] or "")
            literals = self._literal_spans(editor["literal_json"])
            if not allow_literals:
                missing = [
                    str(item.get("literal_id") or "")
                    for item in literals
                    if str(item.get("text") or "") and str(item.get("text")) not in new_text
                ]
                if missing:
                    raise ConflictError(
                        "literal_span_protected",
                        "The edit would change protected verbatim text without explicit author permission",
                    )
            research = json.loads(story["research_json"] or "{}")
            if research.get("content_identity_changed"):
                research["content_identity_changed"] = False
            research["draft_needs_refresh"] = False
            research["draft_stale_reason"] = None
            research["draft_composed_by"] = "mira_live"
            current_fact_ids = eligible_selected_fact_ids(db, story_id)
            research["draft_fact_revisions"] = fact_revision_bundle(
                db,
                story_id,
                current_fact_ids,
            )
            db.execute("UPDATE stories SET research_json=? WHERE id=?", (canonical(research), story_id))
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "text": current_text,
                    "literal_spans": literals,
                    "from_text_revision": current_revision,
                    "summary": editor["last_change"],
                }
            )
            history = history[-20:]
            kept_literals = [] if allow_literals else literals
            next_revision = current_revision + 1
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (new_text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(kept_literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": new_text,
                "literal_spans": kept_literals,
                "change_summary": summary,
                "revision": self.service._story_row(db, story_id)["revision"],
            }
            self._store_command(db, story_id, command_id, "edit_text", args, result)
            return result

    def _literal_begin(self, session, args: dict[str, Any]) -> dict[str, Any]:
        if session.state.get("literal") is not None:
            raise InvalidStateError("literal_already_active", "Verbatim dictation is already active")
        position = str(args.get("position") or "")
        if position not in {"start", "end", "replace_all"}:
            raise ConflictError("literal_position_invalid", "Literal position must be start, end or replace_all")
        session.state["literal"] = {"position": position, "buffer": []}
        self.emit(session, {"type": "literal_mode", "active": True, "position": position})
        return {"ok": True, "literal_mode": True, "position": position}

    @staticmethod
    def _finish_marker(text: str) -> bool:
        normalized = text.lower().replace("ё", "е")
        return bool(
            re.search(
                r"\b(заверш(и|ить|аю)|законч(и|ить|ил)|конец\s+диктов|стоп\s+диктов)\b",
                normalized,
            )
        )

    def _literal_finish(self, session, command_id: str) -> dict[str, Any]:
        literal = session.state.get("literal")
        if not isinstance(literal, dict):
            raise InvalidStateError("literal_not_active", "Verbatim dictation is not active")
        parts = [str(v).strip() for v in literal.get("buffer", []) if str(v).strip()]
        while parts and self._finish_marker(parts[-1]):
            parts.pop()
        text = " ".join(parts).strip()
        if not text:
            raise InvalidStateError("literal_empty", "No verbatim dictation was captured")
        args = {"position": literal["position"], "captured_text": text}
        replay = self._command_replay(session.resource_id, command_id, "literal_finish", args)
        if replay is not None:
            session.state["literal"] = None
            self.emit(session, {"type": "literal_mode", "active": False})
            return replay
        story_id = session.resource_id
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            current_text = str(story["draft_text"] or "")
            current_revision = int(editor["text_revision"])
            literals = self._literal_spans(editor["literal_json"])
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "text": current_text,
                    "literal_spans": literals,
                    "from_text_revision": current_revision,
                    "summary": editor["last_change"],
                }
            )
            history = history[-20:]
            position = literal["position"]
            if position == "replace_all":
                new_text = text
            elif position == "start":
                new_text = text if not current_text else text + "\n\n" + current_text
            else:
                new_text = text if not current_text else current_text + "\n\n" + text
            literal_id = "literal_" + uuid.uuid4().hex[:16]
            literals = [*literals, {"literal_id": literal_id, "text": text, "position": position}]
            next_revision = current_revision + 1
            summary = "Применил дословную диктовку"
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (new_text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": new_text,
                "literal_id": literal_id,
                "literal_text": text,
                "literal_spans": literals,
                "change_summary": summary,
            }
            self._store_command(db, story_id, command_id, "literal_finish", args, result)
        session.state["literal"] = None
        self.emit(session, {"type": "literal_mode", "active": False})
        self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(story_id))})
        return result

    def _undo(self, story_id: str, command_id: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list) or not history:
                raise InvalidStateError("undo_empty", "There is no text edit to undo")
            prior = history.pop()
            next_revision = int(editor["text_revision"]) + 1
            text = str(prior.get("text") or "")
            literals = prior.get("literal_spans") if isinstance(prior.get("literal_spans"), list) else []
            summary = "Отменил последнюю правку"
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": text,
                "literal_spans": literals,
                "change_summary": summary,
            }
            self._store_command(db, story_id, command_id, "undo", args, result)
            return result

    def _generate_visual(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
        instruction = _bounded_text(args.get("visual_instruction"), 600)
        supplied = args.get("fact_ids")
        if supplied is None:
            with self.service.store.connection() as db:
                issues = selected_eligibility_issues(db, story_id)
                if issues:
                    raise InvalidStateError(
                        "fact_review_required",
                        "Selected facts still need semantic review before final visual generation.",
                    )
                ids = eligible_selected_fact_ids(db, story_id)
        else:
            ids = [str(v) for v in supplied]
            with self.service.store.connection() as db:
                issues = eligibility_issues_for_ids(db, story_id, ids)
                if issues:
                    raise InvalidStateError(
                        "fact_review_required",
                        "Requested facts still need semantic review before final visual generation.",
                    )
        body = {"selected_fact_ids": ids, "visual_instruction": instruction}
        key = "ss-live-visual-" + hashlib.sha256(f"{story_id}:{command_id}".encode()).hexdigest()[:48]
        story = self.service.mutate_visual(story_id, key, body)
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='visual' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {"accepted": True, "operation_id": job["id"] if job else None, "story": story}
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "generate_visual", args, result)
        return result

    async def _prepare_publication(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
            if research.get("content_identity_changed"):
                raise InvalidStateError("identity_content_review_required", "После смены объекта нужно проверить и обновить текст публикации.")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
            issues = selected_eligibility_issues(db, story_id)
            if issues:
                raise InvalidStateError(
                    "fact_review_required",
                    "Selected facts still need semantic review or arbitration before publication.",
                )
        destinations = [str(v).strip() for v in args.get("destinations", []) if str(v).strip()]
        if not destinations or len(destinations) > 8:
            raise ConflictError("publish_destinations_required", "At least one bounded destination is required")
        if len(set(destinations)) != len(destinations):
            raise ConflictError("publish_destination_duplicate", "Publication destinations must be unique")

        capabilities = await self.service.capabilities()
        available = {
            str(item.get("alias") or "").strip()
            for item in capabilities.get("destinations", [])
            if isinstance(item, dict) and str(item.get("alias") or "").strip()
        }
        if not available:
            raise ConflictError(
                "publish_destinations_unavailable",
                "No publication destination is currently available",
            )
        invalid = [alias for alias in destinations if alias not in available]
        if invalid:
            raise ConflictError(
                "publish_destination_invalid",
                "Publication destination must exactly match an available destination alias",
            )

        scheduled_for = _bounded_text(args.get("scheduled_for"), 80, required=True)
        tz_name = _bounded_text(args.get("timezone"), 80, required=True)
        try:
            parsed = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        except ValueError:
            raise ConflictError("publish_time_invalid", "scheduled_for must be ISO-8601") from None
        if parsed.tzinfo is None:
            raise ConflictError("publish_time_invalid", "scheduled_for must include an offset")
        if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ConflictError("publish_time_past", "scheduled_for must be in the future")

        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            visual = json.loads(story["visual_context_json"] or "{}")
            research = json.loads(story["research_json"] or "{}")
            if research.get("draft_needs_refresh"):
                raise InvalidStateError(
                    "publication_text_stale",
                    "Выбор фактов, evidence или концепция изменились; Мире нужно обновить текст публикации.",
                )
            selected_ids = eligible_selected_fact_ids(db, story_id)
            draft_revision_issues = revision_bundle_issues(
                db,
                story_id,
                research.get("draft_fact_revisions"),
                expected_fact_ids=selected_ids,
            )
            if draft_revision_issues:
                raise InvalidStateError(
                    "publication_text_stale",
                    "Evidence revisions used by the publication text changed; refresh the draft.",
                )
            visual_revision_issues = revision_bundle_issues(
                db,
                story_id,
                visual.get("fact_revision_bundle"),
                expected_fact_ids=[
                    str(item.get("fact_id") or "")
                    for item in (visual.get("selected_facts") or [])
                    if isinstance(item, dict) and str(item.get("fact_id") or "")
                ],
            )
            if visual_revision_issues:
                raise InvalidStateError(
                    "visual_not_ready",
                    "Evidence revisions used by the visual changed; regenerate the visual.",
                )
            asset_ref = str(story["vibepublish_asset_ref"] or "")
            visual_revision = str(visual.get("content_revision") or "")
            text_value = str(story["draft_text"] or "")
            if not text_value:
                raise InvalidStateError("publish_text_missing", "Publication text is empty")
            if len(text_value) > 1024:
                raise ConflictError("publish_text_too_long", "Telegram photo caption exceeds 1024 characters")
            if not asset_ref or not visual_revision or visual.get("stale"):
                raise InvalidStateError("visual_not_ready", "A current verified visual is required")
            confirmation_id = "confirm_" + uuid.uuid4().hex[:24]
            now = self.service.store.now()
            db.execute(
                "INSERT INTO live_publication_confirmations(id,story_id,text_revision,text_value,visual_revision,asset_ref,destinations_json,scheduled_for,timezone,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    confirmation_id,
                    story_id,
                    int(editor["text_revision"]),
                    text_value,
                    visual_revision,
                    asset_ref,
                    canonical(destinations),
                    scheduled_for,
                    tz_name,
                    now,
                    now,
                ),
            )
            result = {
                "confirmation": {
                    "confirmation_id": confirmation_id,
                    "text_revision": int(editor["text_revision"]),
                    "text": text_value,
                    "visual_revision": visual_revision,
                    "asset_ref": asset_ref,
                    "image_url": story["processed_image_url"],
                    "destinations": destinations,
                    "scheduled_for": scheduled_for,
                    "timezone": tz_name,
                    "state": "prepared",
                }
            }
            self._store_command(db, story_id, command_id, "prepare_publication", args, result)
            return result

    def _confirm_publication(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        confirmation_id = _bounded_text(args.get("confirmation_id"), 80, required=True)
        with self.service.store.connection() as db:
            confirmation = db.execute(
                "SELECT * FROM live_publication_confirmations WHERE id=? AND story_id=?",
                (confirmation_id, story_id),
            ).fetchone()
            if not confirmation:
                raise InvalidStateError("publication_confirmation_missing", "Publication confirmation does not exist")
            if confirmation["state"] not in {"prepared", "confirmed"}:
                raise InvalidStateError("publication_confirmation_invalid", "Publication confirmation is not usable")
            story = self.service._story_row(db, story_id)
            editor = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
            visual = json.loads(story["visual_context_json"] or "{}")
            issues = selected_eligibility_issues(db, story_id)
            if issues:
                raise ConflictError(
                    "publication_confirmation_stale",
                    "Fact eligibility changed after publication confirmation was prepared",
                )
            if (
                not editor
                or int(editor["text_revision"]) != int(confirmation["text_revision"])
                or str(story["draft_text"] or "") != str(confirmation["text_value"])
                or str(story["vibepublish_asset_ref"] or "") != str(confirmation["asset_ref"])
                or str(visual.get("content_revision") or "") != str(confirmation["visual_revision"])
                or bool(visual.get("stale"))
            ):
                raise ConflictError(
                    "publication_confirmation_stale",
                    "Visible text/image changed after confirmation was prepared",
                )
            destinations = json.loads(confirmation["destinations_json"])
            scheduled_for = confirmation["scheduled_for"]
            tz_name = confirmation["timezone"]

        key = "ss-live-publish-" + hashlib.sha256(f"{story_id}:{confirmation_id}".encode()).hexdigest()[:48]
        story_result = self.service.mutate_publish(
            story_id,
            key,
            {
                "destinations": destinations,
                "scheduled_for": scheduled_for,
                "timezone": tz_name,
                "text_override": confirmation["text_value"],
            },
        )
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='publish' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {
            "accepted": True,
            "confirmation_id": confirmation_id,
            "operation_id": job["id"] if job else None,
            "story": story_result,
        }
        with self.service.store.tx() as db:
            db.execute(
                "UPDATE live_publication_confirmations SET state='confirmed',updated_at=? WHERE id=?",
                (self.service.store.now(), confirmation_id),
            )
            self._store_command(db, story_id, command_id, "confirm_publication", args, result)
        return result

    def _cancel_publication(self, story_id: str, command_id: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        key = "ss-live-cancel-" + hashlib.sha256(f"{story_id}:{command_id}".encode()).hexdigest()[:48]
        story = self.service.mutate_cancel(story_id, key, {})
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='cancel' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {"accepted": True, "operation_id": job["id"] if job else None, "story": story}
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "cancel_publication", args, result)
        return result


def _live_resource_environment(settings: Settings) -> dict[str, str]:
    del settings
    url = (
        os.getenv("AI_RESOURCE_CONTROL_URL", "").strip()
        or os.getenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "").strip()
    )
    service_key = (
        os.getenv("AI_RESOURCE_CONTROL_SERVICE_KEY", "").strip()
        or os.getenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "").strip()
    )
    environment: dict[str, str] = {
        "AI_RESOURCE_CONTROL_URL": url,
        "AI_RESOURCE_CONTROL_SERVICE_KEY": service_key,
    }
    ledger_id = os.getenv("AI_RESOURCE_LEDGER_ID", "").strip()
    if ledger_id:
        environment["AI_RESOURCE_LEDGER_ID"] = ledger_id
    fallback_key = os.getenv("GOOGLE_API_KEY3", "").strip()
    if fallback_key:
        environment["AI_RESOURCE_CONTROL_FALLBACK_KEY"] = fallback_key
    return environment


def _forward_committed_output(service, session, event, on_event):
    if event.get("type") == "turn_complete" and session.state.pop('research_turn_output_withheld', False):
        # Withheld audio never reaches the shared host's audio callback, which
        # normally clears this post-tool wait. The provider has finished this
        # turn; release only that wait so the adapter can continue unread proof.
        session.awaiting_audio = False
    if session.state.get("research_output_pending") and event.get("type") in {"audio", "output_transcript", "text"}:
        session.state['research_turn_output_withheld'] = True
        if event.get("type") == "output_transcript":
            session.state["research_continuation_queued"] = False
        # Product evidence policy at the provider boundary; transport,
        # tool execution and progress events still use the shared host.
        if not session.state.get("research_output_suppressed"):
            session.state["research_output_suppressed"] = True
            record_live_diagnostic(service, session.resource_id, session.id, "backend", "research_output_suppressed", {"run_id": session.state.get("research_run_id"), "reason": "uncommitted_evidence"})
        return
    session.state["research_output_suppressed"] = False
    on_event(event)


def create_live_host(service: StreetStoryService, settings: Settings) -> LiveSessionHost:
    ensure_live_schema(service)

    def adapter_factory(**kwargs):
        return StreetStoryLiveAdapter(service, kwargs["emit"], kwargs["write"])

    async def managed_runner(*, session, reader, on_event):
        environment = _live_resource_environment(settings)
        control = None
        def committed_output(event):
            _forward_committed_output(service, session, event, on_event)
        try:
            try:
                from ai_resource_control import run_guarded
                from ai_resource_control.client import Config, Control, estimate_input_tokens
                from ai_resource_control.live import PrependReader
            except ImportError:
                on_event(
                    {
                        "type": "error",
                        "code": "RESOURCE_PACKAGE_MISSING",
                        "message": "RESOURCE_PACKAGE_MISSING",
                    }
                )
                return
            # Acquire must select a scope capable of admitting the actual setup,
            # rather than choosing it for 1024 units and denying setup afterward.
            # The shared controller still owns selection, leases and every send.
            from dataclasses import replace
            from live_interaction.provider import setup_config
            first = await reader.readline()
            start = json.loads(first)
            configuration = start.get('configuration') or {}
            setup = setup_config(start['model'], start.get('context') or {}, start.get('history'),
                configuration=configuration, search=bool(configuration.get('search_enabled')))
            requested = estimate_input_tokens(setup)
            config = replace(Config.from_env('street-story', environment), grant_tokens=requested)
            control = Control(config)
            logger.info('street_story_live_setup_admission %s', canonical({
                'session_id': session.id, 'estimated_units': requested, 'model': start['model']}))
            await run_guarded(
                consumer="street-story",
                environment=environment,
                reader=PrependReader(first, reader),
                control=control,
                on_event=committed_output,
                binding=f"street-story:{session.id}",
            )
        finally:
            if control is not None:
                await control.close()
            environment.clear()

    def transport_diagnostic(record: dict[str, Any]) -> None:
        story_id = str(record.get("resource_id") or "")
        session_id = str(record.get("session_id") or "")
        event_type = str(record.get("event") or "transport")
        record_live_diagnostic(service, story_id, session_id, "transport", event_type, record)
        logger.info(
            "street_story_live_transport %s",
            canonical({
                key: value
                for key, value in record.items()
                if key not in {"ticket", "text", "data", "audio", "credentials"}
            }),
        )

    return LiveSessionHost(
        adapter_factory=adapter_factory,
        managed_runner=managed_runner,
        models=("gemini-3.8-live",),
        ready_timeout_ms=30_000,
        max_sessions=3,
        diagnostic=transport_diagnostic,
    )
