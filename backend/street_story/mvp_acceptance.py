from __future__ import annotations

import json
from typing import Any

from .mvp_research import MvpResearchStreetStoryService, _excerpt_supports
from .service import canonical


class MvpAcceptanceStreetStoryService(MvpResearchStreetStoryService):
    """Final narrow invariants needed by the first end-to-end MVP."""

    def complete_voice(
        self,
        story_id: str,
        session_id: str,
        key: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        with self.store.connection() as db:
            previous_state = self._story_row(db, story_id)["state"]
        receipt = super().complete_voice(story_id, session_id, key, body)
        if previous_state in {"scheduling", "scheduled", "published"}:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE stories SET state=?,updated_at=? WHERE id=?",
                    (previous_state, self.store.now(), story_id),
                )
        return receipt

    def _enforce_claim_support_and_decisions(
        self,
        story_id: str,
        prior_decisions: dict[str, bool],
    ) -> None:
        with self.store.tx() as db:
            story = self._story_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            if story["state"] != "review":
                if prior_decisions:
                    merged = dict(prior_decisions)
                    current = research.get("claim_decisions")
                    if isinstance(current, dict):
                        merged.update({str(k): bool(v) for k, v in current.items()})
                    research["claim_decisions"] = merged
                    db.execute(
                        "UPDATE stories SET research_json=?,updated_at=? WHERE id=?",
                        (canonical(research), self.store.now(), story_id),
                    )
                return

            current_decisions: dict[str, bool] = {}
            for row in list(db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (story_id,))):
                sources = json.loads(row["sources_json"] or "[]")
                valid_sources: list[dict[str, Any]] = []
                for source in sources if isinstance(sources, list) else []:
                    if not isinstance(source, dict):
                        continue
                    support_rows = source.get("supports")
                    if not isinstance(support_rows, list):
                        support_rows = []
                    valid_supports = [
                        support
                        for support in support_rows
                        if isinstance(support, dict)
                        and _excerpt_supports(str(row["text"]), str(support.get("text") or ""))
                    ]
                    if valid_supports:
                        valid_sources.append({**source, "supports": valid_supports})
                supported = bool(valid_sources)
                prior = prior_decisions.get(str(row["fact_id"]))
                selected = supported and bool(row["selected"])
                if prior is False:
                    selected = False
                current_decisions[str(row["fact_id"])] = selected
                db.execute(
                    "UPDATE facts SET evidence_supported=?,selected=?,sources_json=? WHERE story_id=? AND fact_id=?",
                    (
                        int(supported),
                        int(selected),
                        canonical(valid_sources),
                        story_id,
                        row["fact_id"],
                    ),
                )

            merged_decisions = dict(prior_decisions)
            merged_decisions.update(current_decisions)
            research["claim_decisions"] = merged_decisions
            draft, image_notes = self._selected_outputs(
                db,
                story_id,
                story["place_name"],
                str(research.get("author_note") or ""),
            )
            research["image_notes"] = image_notes
            db.execute(
                "UPDATE stories SET draft_text=?,research_json=?,updated_at=? WHERE id=?",
                (draft, canonical(research), self.store.now(), story_id),
            )

    async def _run_research(self, job: dict[str, Any]) -> None:
        with self.store.connection() as db:
            story = self._story_row(db, job["story_id"])
            prior = json.loads(story["research_json"] or "{}")
            decisions = prior.get("claim_decisions")
            prior_decisions = (
                {str(key): bool(value) for key, value in decisions.items()}
                if isinstance(decisions, dict)
                else {}
            )
        await super()._run_research(job)
        self._enforce_claim_support_and_decisions(job["story_id"], prior_decisions)

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        research = json.loads(row["research_json"] or "{}")
        if research.get("image_notes") is not None:
            result["image_notes"] = str(research.get("image_notes") or "")
        ordered = research.get("ordered_voice_ids")
        result["research_voice_ids"] = (
            [str(value) for value in ordered if str(value)]
            if isinstance(ordered, list)
            else []
        )
        return result
