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
async def test_live_batch_exposes_good_facts_without_global_review_and_withholds_one(tmp_path):
    svc, adapter, session, events, run_id, helper_calls, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    args = findings(chunk, QUOTES)
    args['batch_reviewed'] = True
    for n, fact in enumerate(args['facts']):
        fact.update(verdict='supported' if n < 2 else 'insufficient', atomic=True,
                    support_complete=n < 2, qualifiers_preserved=True,
                    review_reason='Controlled model verdict for this own passage.', selected=False)
    args['facts'] = [{'source_refs': [], 'evidence_refs': [], 'evidence_quotes': QUOTES,
                      'claims': [{k: v for k, v in fact.items() if k not in {'source_refs', 'evidence_refs', 'evidence_quotes'}} for fact in args['facts']]}]
    saved = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'normal-batch', 'args': args})
    assert saved['review_required'] is False
    inventory = adapter._get_facts(session.resource_id, {})['facts']
    good = [f for f in inventory if f['eligibility'] == 'eligible']
    bad = [f for f in inventory if f['eligibility'] == 'withheld']
    assert len(good) == 2 and len(bad) == 1 and not any(f['owner_selected'] for f in inventory)
    assert helper_calls == []
    assert await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'same-batch', 'args': args}) == saved
    assert saved['completed'] and saved['next_tool'] is None
    assert any(e.get('type') == 'research_progress' and not e['state']['active'] for e in events)
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_packets').fetchone()[0] == 0
    adapter._select_facts(session.resource_id, 'select-normal', {'fact_ids': [f['fact_id'] for f in good]})
    draft = adapter._edit_text(session.resource_id, 'draft-normal', {'expected_text_revision': 0, 'new_text': ' '.join(f['text'] for f in good), 'change_summary': 'Selected checked findings.'})
    assert bad[0]['text'] not in draft['draft_text']
    adapter._select_facts(session.resource_id, 'select-withheld', {'fact_ids': [bad[0]['fact_id']]})
    with pytest.raises(ConflictError):
        adapter._edit_text(session.resource_id, 'bad-draft', {'expected_text_revision': 1, 'new_text': bad[0]['text'], 'change_summary': 'Must reject withheld selection.'})
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_live_first_skips_failed_source_and_reads_another_saved_source(tmp_path):
    import httpx
    from street_story.research_runs import register_discovered_source
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    bad_url = 'https://a-unreachable.example/page'
    with svc.store.tx() as db:
        register_discovered_source(db, run_id=run_id, url=bad_url, title='Unavailable', status='snippet_only', now=0)
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        502 if str(request.url) == bad_url else 200,
        headers={'content-type': 'text/html'}, text='<main><p>' + ' '.join(QUOTES) + '</p></main>')))
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    assert chunk['source_url'] == URL and chunk['evidence_passages']
    with svc.store.connection() as db:
        failed = db.execute('SELECT status,error_code FROM research_run_sources WHERE run_id=? AND url=?', (run_id, bad_url)).fetchone()
        assert tuple(failed) == ('failed', 'http_502')
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (run_id,)).fetchone()[0] != 'partial'
    await reader.search_http.aclose()


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


@pytest.mark.asyncio
async def test_numeric_passages_and_bounded_invalid_batch_resume(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id}})
    args = findings(chunk, QUOTES[:1], continuation=True)
    args["facts"][0].pop("evidence_quotes")
    args["facts"][0]["passage_ids"] = [chunk["evidence_passages"][0]["passage_id"]]
    saved = await adapter.execute_tool(session, {"name": "save_research_facts", "id": "numeric", "args": args})
    assert saved["payload_saved"] and saved["facts"][0]["revision_digest"]
    chunk = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id}})
    bad = findings(chunk, QUOTES[1:])
    for f in bad["facts"]:
        f.pop("evidence_quotes")
        f["passage_ids"] = [999]
    for index in range(3):
        with pytest.raises(ConflictError) as failure:
            await adapter.execute_tool(session, {"name": "save_research_facts", "id": "bad-" + str(index), "args": bad})
    assert failure.value.code == "live_research_partial"
    adapter.on_stopped(session)
    with svc.store.connection() as db:
        assert run_manifest(db, run_id)["run"]["state"] == "partial"
        assert run_manifest(db, run_id)["run"]["status_detail"] == "invalid_batch_budget_exhausted"
        assert run_manifest(db, run_id)["counts"]["chunk_batches_total"] == 1
    # A new Live session resumes the frozen cursor and existing payload.
    from types import SimpleNamespace
    resumed = SimpleNamespace(id="live_resumed", resource_id=session.resource_id, state={})
    chunk = await adapter._get_research_chunk(resumed, {"run_id": run_id})
    assert chunk["batch_index"] == 1 and len(chunk["checkpoint"]["facts"]) == 1
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_actual_task_cancellation_preserves_first_batch_for_resume(tmp_path):
    import asyncio
    from types import SimpleNamespace
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    await adapter._save_research_facts(session, "first", findings(chunk, QUOTES[:1], continuation=True))
    chunk = await adapter._get_research_chunk(session, {"run_id": run_id})
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    svc.providers.gemini.reconcile_fact_identities = blocked
    args = findings(chunk, QUOTES[1:])
    args["inventory_reviewed"] = False
    task = asyncio.create_task(adapter._save_research_facts(session, "interrupted", args))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    adapter.on_stopped(session)
    resumed = SimpleNamespace(id="live_after_cancel", resource_id=session.resource_id, model="gemini-3.8-live", state={})
    next_chunk = await adapter._get_research_chunk(resumed, {"run_id": run_id})
    assert next_chunk["batch_index"] == 1 and next_chunk["source_version_id"] == chunk["source_version_id"]
    assert len(next_chunk["checkpoint"]["facts"]) == 1
    await adapter._save_research_facts(resumed, "resumed", findings(next_chunk, QUOTES[1:]))
    final = adapter._finalize_fact_review(resumed, "review", review_args(adapter, session.resource_id, run_id))
    assert final["complete"] and final["eligible_count"] == 3
    with svc.store.connection() as db:
        assert run_manifest(db, run_id)["counts"]["chunk_batches_total"] == 2
        assert db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 3
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_failed_document_fetch_hands_back_control_and_can_resume(tmp_path):
    from types import SimpleNamespace
    svc, adapter, session, events, run_id, _, reader = await fallback(tmp_path)
    original = svc.providers.gemini._fetch_page_documents

    async def failed(*args):
        return []

    svc.providers.gemini._fetch_page_documents = failed
    partial = await adapter.execute_tool(session, {"name": "get_research_chunk", "args": {"run_id": run_id}})
    assert partial["partial"] and partial["next_tool"] is None
    assert events[-1]["state"]["active"] is False
    with svc.store.connection() as db:
        assert run_manifest(db, run_id)["run"]["state"] == "partial"
    svc.providers.gemini._fetch_page_documents = original
    resumed = SimpleNamespace(id="live_fetch_resume", resource_id=session.resource_id, model="gemini-3.8-live", state={})
    chunk = await adapter._get_research_chunk(resumed, {"run_id": run_id})
    await adapter._save_research_facts(resumed, "recovered", findings(chunk, QUOTES))
    assert adapter._finalize_fact_review(resumed, "review", review_args(adapter, session.resource_id, run_id))["complete"]
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_discovery_without_checkpoint_is_bounded_and_all_owned_runs_pause(tmp_path):
    svc, adapter, session, events, _, _, reader = await fallback(tmp_path)
    for index in range(2):
        await adapter.execute_tool(session, {"name": "search_web", "id": "search-" + str(index), "args": {"query": "Another aspect " + str(index)}})
    with pytest.raises(ConflictError) as stopped:
        await adapter.execute_tool(session, {"name": "search_web", "id": "search-over-budget", "args": {"query": "No repeated discovery without checkpoint"}})
    assert stopped.value.code == "live_research_partial"
    assert events[-1]["state"]["active"] is False
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_runs WHERE story_id=?", (session.resource_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM research_runs WHERE story_id=? AND state<>'partial'", (session.resource_id,)).fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_model_declared_wrong_poi_query_and_source_are_withheld_without_semantic_regex(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    session.state.pop('research_run_id', None)
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'search_web', 'id': 'wrong-query', 'args': {'query': 'Other city and object', 'query_matches_poi': False}})
    assert error.value.code == 'live_research_identity_mismatch'
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    args = {**findings(chunk, QUOTES), 'source_matches_poi': False}
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-negative-test', 'args': args})
    assert error.value.code == 'live_research_identity_mismatch'
    assert not adapter._get_facts(session.resource_id, {})['facts']
    result = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-negative-test', 'args': {**args, 'facts': []}})
    assert not result.get('facts')
    await reader.search_http.aclose()
