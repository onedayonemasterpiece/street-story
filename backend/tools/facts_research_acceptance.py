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


async def run_case(output, case, budget):
    name, filename, prompt = CASES[case]
    case_dir = output / (case + "-" + str(time.time_ns()))
    case_dir.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.from_env(), data_dir=case_dir)
    svc = MvpLocationStreetStoryService(settings)
    photo = tiny_png()
    story = svc.create_story(key="fixture-create", client_story_id="acceptance-" + case, photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type="image/png", photo_bytes=photo, voice_protocol="voice-chunks-v2", lat=54.7, lon=20.5)
    story_id = story["id"]
    adapter = StreetStoryLiveAdapter(svc, lambda *args: None, lambda *args: None)
    with svc.store.tx() as db:
        research = {"visual_identity": {"status": "owner_confirmed", "candidate_id": "fixture:" + case, "candidate_name": name, "observations": ["Controlled fixture identity"], "candidates": []}}
        db.execute("UPDATE stories SET state='identity_ready',place_name=?,research_json=? WHERE id=?", (name, json.dumps(research), story_id))
    url = "https://ru.wikipedia.org/wiki/" + ("Королевские_ворота" if filename == "royal-gates" else "Бранденбургские_ворота_(Калининград)")
    body = (FIXTURES / f"{filename}-wikipedia-20261003.txt").read_text()
    reader = GeminiClient(svc.settings, svc.store)
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text="<main>" + body.replace("\n", "<br>") + "</main>")))

    async def discovery(query, context):
        return GroundedResearch(payload={"facts": [], "search_provider": "controlled_snapshot", "semantic_status": "live_model_required", "coverage_satisfied": False}, grounding_sources=[{"url": url, "title": name, "supports": [{"kind": "search_snippet", "source_url": url, "text": "Документ об объекте. Прочитайте полный сохранённый текст.", "evidence_ref": "evref_" + "1" * 24}]}])

    async def unavailable(*args, **kwargs):
        raise GeminiUnavailable(None, "acceptance_controlled_helper_outage")

    svc.providers.gemini.search_web = discovery
    svc.providers.gemini._fetch_page_documents = reader._fetch_page_documents
    svc.providers.gemini.detect_fact_conflicts = unavailable
    svc.providers.gemini.reconcile_fact_identities = unavailable
    host = create_live_host(svc, svc.settings)
    events, started, session_id, cursor = [], time.monotonic(), "", 0
    status, stage, error = "FAIL", "bootstrap", None
    try:
        receipt = await host.start(resource_id=story_id, actor=None, model="gemini-3.8-live")
        session_id = receipt["session_id"]
        stage = "research"
        await host.input(session_id=session_id, resource_id=story_id, message={"text": prompt + " Используй один search_web, затем прочитай ВСЕ chunks через get_research_chunk и сохрани batches с короткими числовыми passage_ids из evidence_passages, source_refs=[] и evidence_refs=[]. Изучи get_facts inventory; equivalence решай самостоятельно с inventory_reviewed=true. После чтения всех chunks изучи get_facts и get_evidence, выполни finalize_fact_review с точными revision_digest и supporting_evidence_ids. Не ограничивайся snippets. Не задавай дополнительных вопросов."})
        deadline = started + budget
        while time.monotonic() < deadline:
            page = host.events(session_id=session_id, resource_id=story_id, after=cursor)
            cursor = page["cursor"]
            events.extend(page["events"])
            if any(e.get("type") == "error" for e in page["events"]):
                status, stage = "BLOCKED_PROVIDER", "live_stream"
                break
            with svc.store.connection() as db:
                terminal = db.execute("SELECT state FROM research_runs WHERE story_id=? ORDER BY created_at DESC LIMIT 1", (story_id,)).fetchone()
            if terminal and terminal["state"] in {"completed", "partial"}:
                stage = "terminal_state"
                break
            if page["closed"]:
                status, stage = "BLOCKED_PROVIDER", "live_closed"
                break
            await asyncio.sleep(.25)
    except Exception as exc:
        status = "BLOCKED_PROVIDER" if stage == "bootstrap" else "FAIL"
        error = {"type": type(exc).__name__, "code": str(getattr(exc, "code", ""))}
        if not session_id:
            for owned in host.sessions.values():
                if owned.resource_id == story_id:
                    events.extend(list(owned.events))
    finally:
        if session_id:
            await host.stop(session_id=session_id, resource_id=story_id)
        await reader.search_http.aclose()
    inventory = adapter._get_facts(story_id, {"eligibility": "all", "limit": 100})["facts"]
    evidence = adapter._get_evidence(story_id, {"fact_ids": [f["fact_id"] for f in inventory][:20], "limit": 100}) if inventory else {"evidence": []}
    with svc.store.connection() as db:
        runs = [run_manifest(db, r["run_id"]) for r in db.execute("SELECT run_id FROM research_runs WHERE story_id=?", (story_id,))]
    eligible = [f for f in inventory if f.get("eligibility") == "eligible"]
    budget_denials = [e for e in events if e.get("type") == "resource_budget" and e.get("status") == "denied"]
    if budget_denials and stage == "research":
        status, stage = "BLOCKED_PROVIDER", "tool_response_budget"
        error = {"type": "ResourceBudget", "code": budget_denials[-1].get("code")}
    # Gold exists only in the evaluator; prompts above contain no expected names.
    groups = [["Отакар", "Оттокар", "Оттокар"], ["Фридрих I"], ["Альбрехт"]] if case != "holdout" else [["1657"], ["Бойен"], ["Астер"]]
    ids = [next((f["fact_id"] for f in eligible if any(alias in f["text"] for alias in group)), None) for group in groups]
    gold_ok = all(ids) and len(set(ids)) == 3
    state_ok = bool(runs) and all(r["run"]["state"] == "completed" for r in runs)
    if status != "BLOCKED_PROVIDER":
        status = "PASS" if gold_ok and state_ok and evidence.get("evidence") else "FAIL"
    # PCM is discarded; keep bounded state/tool evidence, never credentials/audio.
    clean_events = [{k: v for k, v in e.items() if k not in {"data", "audio", "audio_base64"}} for e in events if e.get("type") != "audio"]
    result = {"case": case, "status": status, "stage": stage, "model": "gemini-3.8-live", "route": "shared_run_guarded", "source_mode": "controlled_licensed_snapshot", "cold_store": True, "baseline_facts": 0, "elapsed_seconds": round(time.monotonic() - started, 2), "gold_fact_ids": ids, "gold_ok": bool(gold_ok), "state_ok": state_ok, "eligible_count": len(eligible), "facts": inventory, "evidence": evidence, "runs": runs, "events": clean_events, "error": error}
    (case_dir / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ["case", "status", "stage", "elapsed_seconds", "eligible_count", "gold_ok", "error"]}, ensure_ascii=False), flush=True)
    return status


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=180)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    if not args.output.is_relative_to(Path("/home/dev/artifacts")) or not (args.output / ".artifact.json").exists():
        parser.error("output must be a managed artifact directory")
    for path in [Path("/home/dev/.local/state/street-story/providers.env"), Path("/home/dev/.local/state/street-story/service.env")]:
        os.environ.update(parse_dotenv(path))
    for case in ([args.case] if args.case else CASES):
        status = await run_case(args.output, case, args.budget)
        if status != "PASS":
            return 2 if status == "BLOCKED_PROVIDER" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
