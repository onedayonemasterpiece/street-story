#!/usr/bin/env python3
"""Bounded real Live acceptance with controlled, attributed document retrieval.

Fixture identity, discovery and helper outage are controlled. The production
adapter, fetch/chunk pipeline, ledger and shared Live provider runner are used.
No phone, microphone, or live Internet retrieval success is claimed here.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import importlib.metadata
import time
from dataclasses import replace
from pathlib import Path

import httpx

from deploy.devcoveer_install import parse_dotenv
from tools.devcoveer_live_search_canary import tiny_png
from street_story.config import Settings
from street_story.gemini import GeminiUnavailable
from street_story.live import StreetStoryLiveAdapter, create_live_host
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_runs import run_manifest

FIXTURES = Path(__file__).parent / "fixtures" / "facts-research"
CASES = {
    "broad": ("Королевские ворота", "royal-gates", "Исследуй подтверждённый объект и сохрани полезные атомарные факты для рассказа."),
    "targeted": ("Королевские ворота", "royal-gates", "Выясни, какие исторические личности изображены на фасаде подтверждённого объекта; сохрани отдельный факт о каждой фигуре."),
    "holdout": ("Бранденбургские ворота", "brandenburg", "Исследуй подтверждённый объект: его историю, архитектуру и сохранившиеся функции."),
}


def classify_failure(event):
    code = str(event.get("code") or event.get("error_code") or "")
    if code in {"RESOURCE_TOKEN_BUDGET", "RESOURCE_CAPACITY", "RESOURCE_START_BUDGET"}:
        return "BLOCKED_RESOURCE"
    if code.startswith("RESOURCE_CONTROL") or code == "RESOURCE_LEDGER_MISMATCH":
        return "BLOCKED_AUTHORITY"
    if code.startswith("RESOURCE_"):
        return "FAIL_CONTRACT"
    if event.get("status_code") in {429, 503} or code in {"provider_429", "provider_503"}:
        return "BLOCKED_PROVIDER"
    return "FAIL_CONTRACT"


def audit_inventory(adapter, story_id):
    facts, spans, cursor = [], [], 0
    while True:
        page = adapter._get_facts(story_id, {"eligibility": "all", "limit": 50, "cursor": cursor})
        facts.extend(page["facts"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    for start in range(0, len(facts), 20):
        ids, cursor = [f["fact_id"] for f in facts[start:start + 20]], 0
        while True:
            page = adapter._get_evidence(story_id, {"fact_ids": ids, "limit": 50, "cursor": cursor})
            spans.extend(page["evidence"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
    return facts, {"evidence": spans}


def assess_gold(result, assessments):
    """Inspectably human-reviewed predicate/value and evidence, bound to exact text.

    No name substring, negation regex, or role heuristic can grant PASS here.
    """
    inventory = {f["fact_id"]: f for f in result["facts"] if f.get("eligibility") == "eligible"}
    spans = {e["evidence_id"]: e for e in result["evidence"]["evidence"]}
    approved = []
    for assessment in assessments:
        f = inventory.get(assessment.get("fact_id"))
        if not f or assessment.get("supported_gold_relation") is not True:
            return False
        if assessment.get("text_sha256") != hashlib.sha256(f["text"].encode()).hexdigest():
            return False
        evidence_ids = assessment.get("evidence_ids") or []
        if not evidence_ids or any(e not in spans or spans[e]["fact_id"] != f["fact_id"] for e in evidence_ids):
            return False
        approved.append((assessment.get("gold_relation"), f["fact_id"]))
    expected = {"facade:otakar_ii", "facade:frederick_i", "facade:albert"} if result.get("case") != "holdout" else {"built:1657", "portrait:boyen", "portrait:aster"}
    return len(approved) == 3 and {x[0] for x in approved} == expected and len({x[1] for x in approved}) == 3 and result["state_ok"]


async def run_case(output, case, budget, guided=False, real_retrieval=False, helpers='unavailable', wrong_poi_source=False):
    name, filename, prompt = CASES[case]
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    tracked_diff = subprocess.check_output(["git", "diff", "HEAD", "--", "backend"], text=True)
    case_dir = output / (case + "-" + str(time.time_ns()))
    case_dir.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.from_env(), data_dir=case_dir)
    svc = MvpLocationStreetStoryService(settings)
    photo = tiny_png()
    story = svc.create_story(key="fixture-create", client_story_id="acceptance-" + case, photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type="image/png", photo_bytes=photo, voice_protocol="voice-chunks-v2", lat=54.7, lon=20.5)
    story_id = story["id"]
    adapter = StreetStoryLiveAdapter(svc, lambda *args: None, lambda *args: None)
    with svc.store.tx() as db:
        canonical_name = name + ' (Калининград)'
        research = {"identity_generation": 1, "visual_identity": {"status": "owner_confirmed", "candidate_id": "fixture:" + filename, "candidate_name": canonical_name, "canonical_name": canonical_name, "aliases": [name], "locality": "Калининград", "country": "Россия", "generation": 1, "observations": ["Controlled fixture identity"], "candidates": []}}
        db.execute("UPDATE stories SET state='identity_ready',place_name=?,research_json=? WHERE id=?", (name, json.dumps(research), story_id))
    url = "https://ru.wikipedia.org/wiki/" + ("Королевские_ворота" if filename == "royal-gates" else "Бранденбургские_ворота_(Калининград)")
    body = (FIXTURES / f"{filename}-wikipedia-20261003.txt").read_text()
    wrong_url = 'https://negative-fixture.example/brandenburg-berlin'
    wrong_body = 'Синтетический отрицательный контроль, не исторический источник. Бранденбургские ворота в Берлине, Германия. Эти сведения относятся к объекту в Берлине, а не к воротам в Калининграде.'
    documents = {str(httpx.URL(url)): body}
    if wrong_poi_source:
        documents[str(httpx.URL(wrong_url))] = wrong_body
    reader = GeminiClient(svc.settings, svc.store)
    fetch_trace, discovery_trace = [], []
    def snapshot_response(request):
        requested = str(request.url)
        fetch_trace.append(requested)
        # Unknown URLs must never silently receive the confirmed object's body.
        if requested not in documents:
            return httpx.Response(404, text='No controlled snapshot for this URL')
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>" + documents[requested].replace("\n", "<br>") + "</main>")
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(snapshot_response))

    async def discovery(query, context):
        discovery_trace.append({'query': query, 'identity': context.get('visual_identity'), 'fixture_discovery': 'fixed candidate, not a query relevance oracle'})
        result = GroundedResearch(payload={"facts": [], "search_provider": "duckduckgo_html_fallback", "retrieval_mode": "controlled_snapshot", "semantic_status": "live_model_required", "coverage_satisfied": False}, grounding_sources=[{"url": url, "title": canonical_name, "supports": [{"kind": "search_snippet", "source_url": url, "text": "Документ об объекте. Прочитайте полный сохранённый текст.", "evidence_ref": "evref_" + "1" * 24}]}])
        if wrong_poi_source:
            result.grounding_sources.insert(0, {'url': wrong_url, 'title': 'Бранденбургские ворота (Берлин), synthetic negative fixture', 'supports': [{'kind': 'search_snippet', 'source_url': wrong_url, 'text': wrong_body, 'evidence_ref': 'evref_' + '2' * 24}]})
        return await svc.providers.gemini._semantic_complete_discovery(query, context, result) if helpers == 'configured' else result

    async def unavailable(*args, **kwargs):
        raise GeminiUnavailable(None, "acceptance_controlled_helper_outage")

    if not real_retrieval:
        svc.providers.gemini.search_web = discovery
        svc.providers.gemini._fetch_page_documents = reader._fetch_page_documents
    if helpers == 'unavailable':
        svc.providers.gemini.detect_fact_conflicts = unavailable
        svc.providers.gemini.reconcile_fact_identities = unavailable
    host = create_live_host(svc, svc.settings)
    events, started, session_id, cursor = [], time.monotonic(), "", 0
    tool_trace = []
    status, stage, error = "FAIL", "bootstrap", None
    try:
        receipt = await host.start(resource_id=story_id, actor=None, model="gemini-3.8-live")
        session_id = receipt["session_id"]
        owned_adapter = host.adapter
        execute = owned_adapter.execute_tool
        async def traced(session, call):
            entry = {"name": call.get("name"), "args": call.get("args")}
            tool_trace.append(entry)
            try:
                reply = await execute(session, call)
                entry["response"] = reply
                return reply
            except Exception as exc:
                entry["error"] = getattr(exc, "code", type(exc).__name__)
                raise
        owned_adapter.execute_tool = traced
        stage = "research"
        if guided:
            prompt += " Прочитай все chunks и страницы, сохрани атомарные batches, затем get_review_packet и явный review по коротким refs."
        await host.input(session_id=session_id, resource_id=story_id, message={"text": prompt})
        deadline = started + budget
        while time.monotonic() < deadline:
            page = host.events(session_id=session_id, resource_id=story_id, after=cursor)
            cursor = page["cursor"]
            events.extend(page["events"])
            if any(e.get("type") == "error" for e in page["events"]):
                status, stage = classify_failure(next(e for e in page["events"] if e.get("type") == "error")), "live_stream"
                break
            with svc.store.connection() as db:
                terminal = db.execute("SELECT state FROM research_runs WHERE story_id=? ORDER BY created_at DESC LIMIT 1", (story_id,)).fetchone()
            if terminal and terminal["state"] in {"completed", "partial"}:
                stage = "terminal_state"
                break
            if page["closed"]:
                status, stage = "FAIL_CONTRACT", "live_closed"
                break
            await asyncio.sleep(.25)
    except Exception as exc:
        status = classify_failure({"code": getattr(exc, "code", ""), "error_type": type(exc).__name__})
        error = {"type": type(exc).__name__, "code": str(getattr(exc, "code", ""))}
        if not session_id:
            for owned in host.sessions.values():
                if owned.resource_id == story_id:
                    events.extend(list(owned.events))
    finally:
        if session_id:
            await host.stop(session_id=session_id, resource_id=story_id)
        await reader.search_http.aclose()
    inventory, evidence = audit_inventory(adapter, story_id)
    with svc.store.connection() as db:
        runs = [run_manifest(db, r["run_id"]) for r in db.execute("SELECT run_id FROM research_runs WHERE story_id=?", (story_id,))]
    eligible = [f for f in inventory if f.get("eligibility") == "eligible"]
    # Preserve the first causal error. A recovered denial is only incidental.
    pending = None
    for event in events:
        if event.get("type") == "resource_budget" and event.get("modality") == "tool_response":
            if event.get("status") == "denied":
                pending = event
            elif event.get("status") == "granted":
                pending = None
    if pending and stage == "research":
        status, stage = "BLOCKED_RESOURCE", "local_admission_timeout"
        error = {"type": "ResourceAdmission", "code": pending.get("code")}
    state_ok = bool(runs) and all(r["run"]["state"] == "completed" for r in runs)
    ids = []
    gold_ok = False  # Strings/names alone cannot establish subject, role or support.
    if status == "FAIL":
        contract_failures = [e for e in events if e.get("type") == "tool_result" and e.get("status") == "error"]
        status = "REVIEW_REQUIRED" if state_ok and eligible else "FAIL_CONTRACT" if contract_failures else "FAIL_SEMANTIC"
    # PCM is discarded; keep bounded state/tool evidence, never credentials/audio.
    clean_events = [{k: v for k, v in e.items() if k not in {"data", "audio", "audio_base64"}} for e in events if e.get("type") != "audio"]
    result = {"case": case, "status": status, "stage": stage, "model": "gemini-3.8-live", "route": "shared_run_guarded", "source_mode": "real_retrieval" if real_retrieval else "controlled_licensed_snapshot", "prompt_mode": "guided_diagnostic" if guided else "ordinary_request", "semantic_review": "pending_manual_gold_assessment", "source_sha": source_sha, "dependencies": {name: importlib.metadata.version(name) for name in ("ai-resource-control", "live-interaction")}, "corpus_sha256": hashlib.sha256(body.encode()).hexdigest(), "cold_store": True, "baseline_facts": 0, "elapsed_seconds": round(time.monotonic() - started, 2), "gold_fact_ids": ids, "gold_ok": bool(gold_ok), "state_ok": state_ok, "eligible_count": len(eligible), "facts": inventory, "evidence": evidence, "runs": runs, "events": clean_events, "tool_trace": tool_trace, "error": error}
    result.update(helper_mode=helpers,
                  tracked_diff_sha256=hashlib.sha256(tracked_diff.encode()).hexdigest(),
                  tracked_tree_clean=not bool(tracked_diff), discovery_trace=discovery_trace, fetch_trace=fetch_trace,
                  wrong_poi_negative_fixture=wrong_poi_source,
                  documents_sha256={key: hashlib.sha256(value.encode()).hexdigest() for key, value in documents.items()})
    (case_dir / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ["case", "status", "stage", "elapsed_seconds", "eligible_count", "gold_ok", "error"]}, ensure_ascii=False), flush=True)
    return status


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assess-receipt", type=Path)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--budget", type=int, default=180)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--guided", action="store_true")
    parser.add_argument("--real-retrieval", action="store_true")
    parser.add_argument('--helpers', choices=['configured', 'unavailable'], default='unavailable', help='Declare configured production helpers or a controlled Live-only helper outage.')
    parser.add_argument('--wrong-poi-source', action='store_true', help='Add a clearly synthetic Berlin source before the correct controlled snapshot; no query relevance oracle.')
    args = parser.parse_args()
    if args.real_retrieval and args.wrong_poi_source:
        parser.error('wrong-POI synthetic fixture is controlled retrieval only')
    if args.assess_receipt:
        if not args.assessment or not args.assess_receipt.is_relative_to(Path("/home/dev/artifacts")):
            parser.error("assessment and managed receipt required")
        result = json.loads(args.assess_receipt.read_text())
        assessments = json.loads(args.assessment.read_text())
        result["gold_ok"] = assess_gold(result, assessments)
        result["semantic_review"] = str(args.assessment)
        if not result["status"].startswith("BLOCKED_"):
            result["status"] = "PASS" if result["gold_ok"] else "FAIL_SEMANTIC"
        # The original online receipt remains immutable.
        destination = args.assess_receipt.with_name("acceptance-assessed.json")
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": result["status"], "gold_ok": result["gold_ok"]}))
        return 0 if result["status"] == "PASS" else 1
    if not args.output or not args.output.is_relative_to(Path("/home/dev/artifacts")) or not (args.output / ".artifact.json").exists():
        parser.error("output must be a managed artifact directory")
    for path in [Path("/home/dev/.local/state/street-story/providers.env"), Path("/home/dev/.local/state/street-story/service.env")]:
        os.environ.update(parse_dotenv(path))
    for case in ([args.case] if args.case else CASES):
        status = await run_case(args.output, case, args.budget, args.guided, args.real_retrieval, args.helpers, args.wrong_poi_source)
        if status != "PASS":
            return 2 if status.startswith("BLOCKED_") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
