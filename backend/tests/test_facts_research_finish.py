"""Product-state acceptance for the complete Live-owned research fallback.

Model semantics are controlled findings here, not a claim of real-model quality.
"""
from __future__ import annotations

import pytest

from test_live_editor import make_service, mark_identity_ready
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_runs import run_manifest
from street_story.service import ConflictError

URL = "https://archive.example/full-document"
QUOTES = ["Первый подтверждённый атомарный тезис.", "Второй подтверждённый атомарный тезис.", "Третий подтверждённый атомарный тезис."]


async def fallback(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    helper_calls = []

    async def unavailable(*args, **kwargs):
        helper_calls.append(True)
        raise GeminiUnavailable(None, "controlled_helper_outage")

    async def discovery(query, context):
        return GroundedResearch(payload={"facts": [], "search_provider": "duckduckgo_html_fallback", "semantic_status": "live_model_required", "coverage_satisfied": False},
                                grounding_sources=[{"url": URL, "title": "Full document", "supports": [{"kind": "search_snippet", "source_url": URL, "text": "Документ об объекте; требуется прочитать полный текст.", "evidence_ref": "evref_" + "1" * 24}]}])
    svc.providers.gemini.search_web = discovery
    svc.providers.gemini.detect_fact_conflicts = unavailable
    svc.providers.gemini.reconcile_fact_identities = unavailable
    result = await adapter.execute_tool(session, {"name": "search_web", "id": "search", "args": {"query": "Исследуй объект без подсказок ответа."}})
    run_id = result["research_run_id"]
    # The same production fetch/chunk pipeline; network is controlled, not bypassed.
    import httpx
    reader = GeminiClient(svc.settings, svc.store)
    body = "<main><p>" + " ".join(QUOTES) + "</p></main>"
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=body)))
    svc.providers.gemini._fetch_page_documents = reader._fetch_page_documents
    return svc, adapter, session, events, run_id, helper_calls, reader


def findings(chunk, quotes, continuation=False):
    return {"run_id": chunk["research_run_id"], "chunk_id": chunk["chunk_id"], "batch_id": chunk["batch_id"], "batch_index": chunk["batch_index"],
            "expected_story_revision": chunk["expected_story_revision"], "inventory_reviewed": True, "continuation_needed": continuation,
            "facts": [{"claim_key": "claim-" + str(QUOTES.index(quote)), "text": quote, "confidence": .95, "selected": True,
                       "source_refs": [], "evidence_refs": [], "evidence_quotes": [quote]} for quote in quotes]}


def review_args(adapter, story_id, run_id, coverage=True):
    inventory = adapter._get_facts(story_id, {"eligibility": "all", "limit": 50})
    return {"run_id": run_id, "reviewed_assertions": [{"fact_id": item["fact_id"], "revision_digest": item["revision_digest"], "supporting_evidence_ids": [e["evidence_id"] for e in adapter._get_evidence(story_id, {"fact_ids": [item["fact_id"]]})["evidence"]]} for item in inventory["facts"]],
            "conflicts": [], "coverage_complete": coverage, "missing_aspects": [] if coverage else ["unanswered_aspect"]}


@pytest.mark.asyncio
async def test_document_fallback_checkpoint_replay_review_selection_and_draft(tmp_path):
    svc, adapter, session, events, run_id, helper_calls, reader = await fallback(tmp_path)
    first = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id, "source_url": URL}})
    first_args = findings(first, QUOTES[:1], continuation=True)
    saved = await adapter.execute_tool(session, {"name": "save_research_facts", "id": "batch-first", "args": first_args})
    assert saved["payload_saved"] is True
    assert events[-2]["type"] == "research_progress" or any(e.get("type") == "research_progress" and e["state"]["active"] for e in events)
    replay = await adapter.execute_tool(session, {"name": "save_research_facts", "id": "batch-first-replay-new-call", "args": first_args})
    assert replay == saved
    second = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id}})
    assert second["source_version_id"] == first["source_version_id"]
    assert second["batch_index"] == 1
    assert len(second["checkpoint"]["facts"]) == 1
    await adapter.execute_tool(session, {"name": "save_research_facts", "id": "batch-second", "args": findings(second, QUOTES[1:])})
    done = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id}})
    assert done["all_chunks_processed"]
    bundle = review_args(adapter, session.resource_id, run_id)
    inventory = adapter._get_facts(session.resource_id, {"eligibility": "all"})["facts"]
    assert len(inventory) == 3 and len({item["fact_id"] for item in inventory}) == 3
    assert all(item["eligibility"] == "unreviewed" for item in inventory)
    with pytest.raises(ConflictError):
        adapter._edit_text(session.resource_id, "premature-draft", {"expected_text_revision": 0, "new_text": "Черновик до review.", "change_summary": "Проверка gate"})
    final = await adapter.execute_tool(session, {"name": "finalize_fact_review", "id": "final-review", "args": bundle})
    assert final["complete"] and final["eligible_count"] == 3 and final["unreviewed_count"] == 0
    assert not helper_calls
    selected = [item["fact_id"] for item in inventory]
    adapter._select_facts(session.resource_id, "select", {"fact_ids": selected})
    draft = adapter._edit_text(session.resource_id, "draft", {"expected_text_revision": 0, "new_text": "Связный черновик на основе выбранных проверенных тезисов.", "change_summary": "Проверенная подборка"})
    assert draft["draft_text"]
    with svc.store.connection() as db:
        manifest = run_manifest(db, run_id)
        assert manifest["run"]["state"] == "completed"
        assert manifest["counts"]["chunk_batches_total"] == 2
        assert all(item["payload_saved"] for item in manifest["chunk_batches"])
        assert db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 3
    assert any(e.get("type") == "research_progress" and e["state"]["stage"] == "review" and e["state"]["active"] for e in events)
    assert events[-1]["type"] == "product_state"
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_partial_coverage_keeps_reviewed_facts_and_zero_claim_chunk_receipt(tmp_path):
    svc, adapter, session, events, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    await adapter._save_research_facts(session, "save", findings(chunk, QUOTES[:1]))
    final = adapter._finalize_fact_review(session, "review", review_args(adapter, session.resource_id, run_id, coverage=False))
    assert not final["complete"] and final["eligible_count"] == 1
    assert not events[-1]["state"]["active"]
    with svc.store.connection() as db:
        assert run_manifest(db, run_id)["run"]["state"] == "partial"
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_page_batch_unknown_quote_and_cancelled_last_await_are_rejected(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    args = findings(chunk, QUOTES[:1])
    args["facts"][0]["evidence_quotes"] = ["Вымышленная цитата."]
    with pytest.raises(ConflictError, match="verbatim"):
        await adapter._save_research_facts(session, "bad-quote", args)
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 0
    session.state["research_cancelled"] = True
    with pytest.raises(ConflictError):
        await adapter._save_research_facts(session, "late", findings(chunk, QUOTES[:1]))
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_final_review_transaction_rolls_back_scan_on_late_failure(tmp_path, monkeypatch):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    await adapter._save_research_facts(session, "save", findings(chunk, QUOTES[:1]))
    original = adapter._store_command
    def fail_receipt(db, story_id, command_id, tool_name, args, result):
        if tool_name == "finalize_fact_review":
            raise RuntimeError("controlled_receipt_failure")
        return original(db, story_id, command_id, tool_name, args, result)
    monkeypatch.setattr(adapter, "_store_command", fail_receipt)
    with pytest.raises(RuntimeError):
        adapter._finalize_fact_review(session, "review", review_args(adapter, session.resource_id, run_id))
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_conflict_scans WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 0
        assert db.execute("SELECT eligibility FROM fact_assertions WHERE story_id=?", (session.resource_id,)).fetchone()[0] == "unreviewed"
        assert run_manifest(db, run_id)["run"]["state"] != "completed"
    await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "identity", "revision"])
async def test_save_rejects_changes_during_last_semantic_await(tmp_path, change):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    await adapter._save_research_facts(session, "first", findings(chunk, QUOTES[:1], continuation=True))
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})

    async def mutate_during_reconciliation(*args):
        if change == "cancel":
            session.state["research_cancelled"] = True
        else:
            import json
            with svc.store.tx() as db:
                row = svc._story_row(db, session.resource_id)
                research = json.loads(row["research_json"])
                if change == "identity":
                    research["identity_generation"] = int(research.get("identity_generation") or 0) + 1
                db.execute("UPDATE stories SET revision=revision+1,research_json=? WHERE id=?", (json.dumps(research), session.resource_id))
        return {"matches": {}, "decisions": [], "metadata": {}}

    svc.providers.gemini.reconcile_fact_identities = mutate_during_reconciliation
    args = findings(chunk, QUOTES[1:])
    args["inventory_reviewed"] = False
    with pytest.raises(ConflictError):
        await adapter._save_research_facts(session, "late-batch", args)
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 1
        assert run_manifest(db, run_id)["counts"]["chunk_batches_total"] == 1
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_no_claims_chunk_has_durable_terminal_payload(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    args = findings(chunk, [])
    saved = await adapter.execute_tool(session, {"name": "save_research_facts", "id": "no-claims", "args": args})
    assert saved["payload_saved"]
    with svc.store.connection() as db:
        manifest = run_manifest(db, run_id)
        assert manifest["chunks"][0]["status"] == "no_claims"
        assert manifest["counts"]["terminal_chunks_payload_missing"] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_addressed_passages_and_pending_chunk_complete_guard(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    args = findings(chunk, QUOTES[:1], continuation=True)
    args["facts"][0].pop("evidence_quotes")
    args["facts"][0]["evidence_refs"] = [chunk["evidence_passages"][0]["evidence_ref"]]
    await adapter._save_research_facts(session, "addressed", args)
    with pytest.raises(ConflictError, match="ALL remaining chunks"):
        adapter._finalize_fact_review(session, "premature", review_args(adapter, session.resource_id, run_id))
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_conflict_scans WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 0
    partial = adapter._finalize_fact_review(session, "partial", review_args(adapter, session.resource_id, run_id, coverage=False))
    assert partial["eligible_count"] == 1 and not partial["complete"]
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_review_rejects_another_assertions_evidence(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    await adapter._save_research_facts(session, "save-all", findings(chunk, QUOTES))
    args = review_args(adapter, session.resource_id, run_id)
    args["reviewed_assertions"][0]["supporting_evidence_ids"] = args["reviewed_assertions"][1]["supporting_evidence_ids"]
    with pytest.raises(ConflictError, match="exact assertion scope"):
        adapter._finalize_fact_review(session, "wrong-evidence", args)
    assert all(f["eligibility"] == "unreviewed" for f in adapter._get_facts(session.resource_id, {"eligibility": "all"})["facts"])
    await reader.search_http.aclose()
