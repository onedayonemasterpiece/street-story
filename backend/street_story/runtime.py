from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .mvp_location import MvpLocationStreetStoryService
from .product import VibePublishBoundary
from .agent_evidence import (
    AgentEvidenceMixin, CONTRACT, NETWORK_CONTRACT, confirmation_consistency_error, confirmation_excerpt,
)
from .fact_ledger import accepted_basis_valid, subject_reads


def confirmed_fact_operation(db, story_id, assertion):
    """Project only an accepted original confirmation, including closed v1 proofs."""
    if not assertion or not accepted_basis_valid(db, story_id, assertion):
        return {}
    basis = json.loads(assertion['accepted_basis_json'])
    operation = basis.get('operation') or {}
    inherited = operation.get('canonical_admission')
    source_story_id = story_id
    if isinstance(inherited, dict):
        operation = inherited.get('operation') or {}
        source_story_id = inherited.get('source_story_id') or story_id
    if operation.get('contract') not in {CONTRACT, NETWORK_CONTRACT}:
        return {}
    if operation['contract'] == NETWORK_CONTRACT:
        result = operation.get('confirmation_result')
        if result is None:
            saved = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND attempt_id=?',
                (source_story_id, operation.get('confirmation_attempt_id'))).fetchone()
            receipt = json.loads(saved['receipt_json']) if saved else {}
            if (receipt.get('phase') != 'completed' or receipt.get('contract') != NETWORK_CONTRACT
                    or receipt.get('provider_send_state') not in {None, 'response_closed'}):
                return {}
            result = receipt.get('result')
        if not isinstance(result, dict) or result.get('verdict') != 'supported':
            return {}
        evidence_ids = {item['evidence_id'] for item in basis['evidence']}
        chunk_ids = {row['chunk_id'] for row in db.execute(
            'SELECT e.evidence_id,e.chunk_id FROM fact_evidence_spans e '
            'JOIN fact_observations o ON o.observation_id=e.observation_id '
            'WHERE o.story_id=? AND o.assertion_id=?', (story_id, assertion['assertion_id']))
            if row['evidence_id'] in evidence_ids}
        if (confirmation_consistency_error(result, operation, chunk_ids)
                or result['checks'] != operation.get('confirmation_checks')
                or result['subject_scope'] != operation.get('subject_scope')):
            return {}
    return operation


class ReplayCheckingVibePublishBoundary(VibePublishBoundary):
    """Normal idempotent ingress; no duplicate upload for image comparison."""

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str) -> dict[str, Any]:
        return await super().ingress_asset(data, mime_type, request_key)


class RuntimeStreetStoryService(AgentEvidenceMixin, MvpLocationStreetStoryService):
    """MVP product service plus bounded live/readback evidence; durable core stays unchanged."""

    def __init__(self, settings: Settings, providers=None):
        super().__init__(settings, providers)
        if providers is None:
            self.providers.vibepublish = ReplayCheckingVibePublishBoundary(settings)

    @subject_reads()
    def _story_repr(self, db, row, *, research=None) -> dict[str, Any]:
        if research is None:
            research = json.loads(row["research_json"] or "{}")
        result = super()._story_repr(db, row, research=research)
        for fact in result.get('facts', []):
            assertion = db.execute('SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id=?',
                                   (row['id'], fact['fact_id'])).fetchone()
            operation = confirmed_fact_operation(db, row['id'], assertion)
            if operation:
                fact['subject_scope'] = operation.get('subject_scope')
                fact['confirmation'] = {key: operation.get(key) for key in
                    ('confirmation_attempt_id', 'confirmation_model', 'confirmed_fragment', 'source_date', 'reason', 'agent')}
                excerpt = ((operation.get('excerpt_projection') or {}).get('confirmed_fragment')
                           or confirmation_excerpt(operation.get('confirmed_fragment') or ''))
                fact['confirmation'].update(confirmed_fragment=excerpt['text'],
                    confirmed_fragment_kind=excerpt['kind'], confirmed_fragment_truncated=excerpt['truncated'],
                    confirmed_fragment_original_sha256=excerpt['original_sha256'])
        if research:
            wikipedia = research.get("wikipedia") if isinstance(research.get("wikipedia"), list) else []
            grounding = (
                research.get("grounding_sources")
                if isinstance(research.get("grounding_sources"), list)
                else []
            )
            result["research_provenance"] = {
                "osm_present": bool(research.get("osm")),
                "wikipedia_page_count": len(wikipedia),
                "grounded_source_count": len(grounding),
            }

        processing = result.get("processing")
        if isinstance(processing, dict):
            pending = db.execute(
                "SELECT kind,state,attempts,available_at,last_error FROM jobs "
                "WHERE story_id=? AND kind IN ('research','refinement') "
                "AND state IN ('ready','running','retry') ORDER BY created_at LIMIT 1",
                (row["id"],),
            ).fetchone()
            if pending:
                processing["job_kind"] = pending["kind"]
                processing["job_state"] = pending["state"]
                processing["attempts"] = pending["attempts"]
                processing["available_at"] = pending["available_at"]
                if pending["last_error"]:
                    processing["last_error"] = self.settings.redact(str(pending["last_error"]))
        return result
