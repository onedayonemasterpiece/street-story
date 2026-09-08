from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .product import ProductStreetStoryService, VibePublishBoundary
from .providers import PermanentProviderError


class ReplayCheckingVibePublishBoundary(VibePublishBoundary):
    """Use the documented asset replay contract as a runtime reliability check."""

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str) -> dict[str, Any]:
        first = await super().ingress_asset(data, mime_type, request_key)
        replay = await super().ingress_asset(data, mime_type, request_key)
        first_identity = (
            str(first.get("asset_id") or ""),
            str(first.get("source_sha256") or "").lower(),
        )
        replay_identity = (
            str(replay.get("asset_id") or ""),
            str(replay.get("source_sha256") or "").lower(),
        )
        if first_identity != replay_identity:
            raise PermanentProviderError(
                "VibePublish same-key asset ingress replay changed immutable asset identity"
            )
        return first


class RuntimeStreetStoryService(ProductStreetStoryService):
    """Product service plus bounded live/readback evidence; durable core stays unchanged."""

    def __init__(self, settings: Settings, providers=None):
        super().__init__(settings, providers)
        if providers is None:
            self.providers.vibepublish = ReplayCheckingVibePublishBoundary(settings)

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        research = json.loads(row["research_json"] or "{}")
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
