"""Cheap real-Mira editorial acceptance over an already identified, frozen corpus.

Uses the existing product test-store and shared guarded Live host. Reads only the
addressed production story's identity/fact ledger, never auth tables or media.
No research, worker loop, image generation or Telegram dispatch is started.
Semantic diversity and draft support require an inspectable human assessment;
counts/IDs alone cannot prove them. full_social remains a separate smoke.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from deploy.devcoveer_install import parse_dotenv
from street_story.config import Settings
from street_story.live import create_live_host, ensure_live_schema
from street_story.mvp_location import MvpLocationStreetStoryService

RICH_REQUEST = (
    'Выбери несколько содержательно разных фактов, больше двух, которые вместе '
    'дадут интересный городской рассказ с иным углом, чем в предыдущих постах. '
    'Сам предложи и сохрани содержательную концепцию и выбранные факты, '
    'затем сохрани текст поста только по этому выбору. Перед выбором прочитай '
    'весь доступный подтверждённый список; зачитывать его мне не нужно. Новое исследование, '
    'картинка и публикация сейчас не нужны.'
)
SECOND_REQUEST = (
    'Сделай другой пост об этом же объекте; не повторяй прежний угол без необходимости. '
    'Прочитай полный подтверждённый список и контекст предыдущих постов, '
    'сам выбери несколько содержательно разных фактов, больше двух, '
    'сохрани выбор, новую концепцию и текст только по выбранным фактам. '
    'Не ищи новые источники, не создавай картинку и не публикуй.'
)
VISUAL_REQUEST = (
    'Подготовь запрос на читаемую инфографику по этому посту: сам выбери небольшое '
    'подмножество уже выбранных фактов для картинки. Полный выбор фактов, '
    'концепцию и текст поста сохрани. Публиковать не нужно.'
)
ALLOWED_TOOLS = {'read_topic', 'get_facts', 'get_evidence', 'continue_story',
                 'select_facts', 'set_concept', 'edit_text'}


def frozen_corpus(source_db: Path, story_id: str) -> dict:
    with sqlite3.connect(source_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        story = db.execute('SELECT * FROM stories WHERE id=?', (story_id,)).fetchone()
        if story is None:
            raise ValueError('identified_story_required')
        identity = json.loads(story['research_json']).get('visual_identity') or {}
        if identity.get('status') not in {'match', 'owner_confirmed'}:
            raise ValueError('identified_story_required')
        assertions = [dict(row) for row in db.execute(
            "SELECT * FROM fact_assertions WHERE story_id=? AND eligibility='eligible'", (story_id,))]
        ids = {row['assertion_id'] for row in assertions}
        facts = [dict(row) for row in db.execute('SELECT * FROM facts WHERE story_id=?', (story_id,))
                 if row['fact_id'] in ids]
        if len(facts) < 8 or any(not fact['evidence_supported'] for fact in facts):
            raise ValueError('rich_eligible_source_backed_inventory_required')
        observations = [dict(row) for row in db.execute(
            'SELECT * FROM fact_observations WHERE story_id=?', (story_id,)) if row['assertion_id'] in ids]
        spans = [dict(row) for row in db.execute(
            'SELECT e.* FROM fact_evidence_spans e JOIN fact_observations o '
            'ON o.observation_id=e.observation_id WHERE o.story_id=?', (story_id,))
            if row['observation_id'] in {item['observation_id'] for item in observations}]
    # Deliberately exclude auth, runtime jobs, media, private provider receipts,
    # research branches and generation/publication handles from this fixture.
    return {'source_story_id': story_id, 'identity': identity, 'place_name': story['place_name'],
            'prior_concept': json.loads(story['research_json']).get('publication_concept'),
            'facts': facts, 'fact_assertions': assertions,
            'fact_observations': observations, 'fact_evidence_spans': spans}


def seed_story(service, corpus: dict, *, previous: bool = False) -> str:
    story_id = 'story_' + uuid.uuid4().hex[:24]
    now = service.store.now()
    research = {'visual_identity': corpus['identity'], 'identity_generation': 0}
    if previous:
        research['publication_concept'] = corpus['prior_concept'] or ''
    with service.store.tx() as db:
        db.execute('INSERT INTO stories(id,client_story_id,photo_sha256,photo_mime_type,photo_path,'
            'voice_protocol,state,place_name,research_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (story_id, 'editorial-utilization-' + story_id, 'editorial-upload-' + story_id,
             'image/jpeg', '', 'voice-chunks-v2', 'review', corpus['place_name'],
             json.dumps(research, ensure_ascii=False), now, now))
        for table in ('facts', 'fact_assertions', 'fact_observations', 'fact_evidence_spans'):
            for original in corpus[table]:
                row = dict(original)
                if 'story_id' in row:
                    row['story_id'] = story_id
                if 'observation_id' in row:
                    row['observation_id'] = story_id + ':' + row['observation_id']
                if 'evidence_id' in row:
                    row['evidence_id'] = story_id + ':' + row['evidence_id']
                if not previous:
                    for field in ('selected', 'owner_selected'):
                        if field in row:
                            row[field] = 0
                columns = ','.join(row)
                db.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in row)})', tuple(row.values()))
    return story_id


def selected_state(service, story_id: str) -> dict:
    story = service.story(story_id)
    return {'selected_fact_ids': [f['fact_id'] for f in story['facts'] if f['selected']],
            'selected_facts': [f for f in story['facts'] if f['selected']],
            'concept': story.get('publication_concept'), 'draft': story.get('draft_text')}


def assess(receipt: dict, assessment: dict) -> dict:
    """Human semantic assessment bound to the exact scenario text and IDs."""
    passed = receipt.get('mechanical_pass') is True
    for scenario, review in zip(receipt['scenarios'], assessment.get('scenarios', []), strict=True):
        passed = passed and review.get('selected_fact_ids') == scenario['selected_fact_ids']
        passed = passed and review.get('draft') == scenario['draft'] and review.get('concept') == scenario['concept']
        passed = passed and review.get('source_backed_diverse_selection') is True
        passed = passed and review.get('draft_uses_only_selected_facts') is True
        passed = passed and bool(review.get('reason'))
    return {**receipt, 'semantic_assessment': assessment, 'status': 'PASS' if passed else 'FAIL_SEMANTIC'}


async def run(output: Path, corpus: dict, budget: int) -> dict:
    case_dir = output / ('editorial-' + uuid.uuid4().hex[:12])
    settings = replace(Settings.from_env(), data_dir=case_dir)
    service = MvpLocationStreetStoryService(settings)
    ensure_live_schema(service)
    previous_id = seed_story(service, corpus, previous=True)
    host = create_live_host(service, settings)
    original_execute = host.adapter.execute_tool
    original_initialize = host.adapter.initialize
    traces, scenarios = [], []
    active_trace = []
    delivered_inventory = []
    allow_visual = False

    def initialized(**kwargs):
        result = original_initialize(**kwargs)
        context = result['context']
        # These whole claim texts are delivered in the actual ready setup,
        # not obtained from a server-side audit pretending to be a model read.
        delivered_inventory.extend(context.get('known_fact_inventory') or [])
        return result

    host.adapter.initialize = initialized

    async def traced(session, call):
        entry = {'name': call.get('name'), 'args': call.get('args') or {}}
        active_trace.append(entry)
        if entry['name'] not in ALLOWED_TOOLS and not (allow_visual and entry['name'] == 'generate_visual'):
            entry['error'] = 'unexpected_editorial_action'
            raise ValueError('unexpected_editorial_action')
        try:
            response = await original_execute(session, call)
            entry['response'] = response
            return response
        except Exception as exc:
            entry['error'] = str(getattr(exc, 'code', type(exc).__name__))
            raise

    host.adapter.execute_tool = traced
    status, error = 'FAIL', None
    try:
        for prompt in (RICH_REQUEST, SECOND_REQUEST):
            story_id = seed_story(service, corpus)
            active_trace = []
            delivered_inventory = []
            traces.append(active_trace)
            started = await host.start(resource_id=story_id, actor=None, model='gemini-3.8-live')
            session_id, cursor = started['session_id'], 0
            clarification_sent = False
            try:
                await host.input(session_id=session_id, resource_id=story_id, message={'text': prompt})
                deadline = time.monotonic() + budget
                while time.monotonic() < deadline:
                    events = host.events(session_id=session_id, resource_id=story_id, after=cursor)
                    cursor = events['cursor']
                    failure = next((e for e in events['events'] if e.get('type') == 'error'), None)
                    if failure:
                        raise RuntimeError(str(failure.get('code') or 'live_error'))
                    current = selected_state(service, story_id)
                    if current['draft'] and current['concept'] and len(current['selected_fact_ids']) > 2:
                        break
                    if (not clarification_sent and any(e.get('type') == 'turn_complete' for e in events['events'])
                            and not any('response' not in t and 'error' not in t for t in active_trace)):
                        # One ordinary owner clarification after a known turn
                        # boundary, never a replay of an unknown tool mutation.
                        await host.input(session_id=session_id, resource_id=story_id, message={'text':
                            'Список зачитывать не нужно. Мне нужен сохранённый пост: выбери несколько '
                            'содержательно разных подтверждённых фактов, больше двух, сохрани выбор, '
                            'свою концепцию и текст только по выбранным фактам. Не повторяй '
                            'без необходимости угол предыдущего поста. Картинка и публикация не нужны.'})
                        clarification_sent = True
                    if events['closed']:
                        raise RuntimeError('live_closed_before_editorial_result')
                    await asyncio.sleep(.25)
                else:
                    raise TimeoutError('editorial_result_timeout')
                # Read back after the text write. Do not mistake a transient
                # selection or a spoken promise for a durable editorial result.
                before = selected_state(service, story_id)
                read_ids = {f['fact_id'] for t in active_trace if t['name'] == 'get_facts'
                    for f in (t.get('response') or {}).get('facts', []) if f.get('eligibility') == 'eligible'}
                # A complete setup inventory is equally valid; require exact
                # whole text as well as ID. Paginated tools cover truncation.
                eligible_text = {f['fact_id']: f['text'] for f in corpus['facts']}
                setup_ids = {fact_id for fact_id, text in delivered_inventory
                    if eligible_text.get(fact_id) == text}
                read_ids.update(setup_ids)
                full_ids = {f['fact_id'] for f in corpus['facts']}
                snapshot = host.adapter._topic_state(story_id)
                scenario = {'story_id': story_id, 'owner_request': prompt, **before,
                    'model_read_eligible_ids': sorted(read_ids), 'full_inventory_read': read_ids == full_ids,
                    'whole_claims_delivered_in_ready_setup': sorted(setup_ids),
                    'owner_clarification_sent': clarification_sent,
                    'previous_editorial_context': snapshot.get('previous_editorial_context'),
                    'selected_ids_preserved': selected_state(service, story_id)['selected_fact_ids'] == before['selected_fact_ids']}
                scenarios.append(scenario)
                if len(scenarios) == 2:
                    allow_visual = True
                    await host.input(session_id=session_id, resource_id=story_id, message={'text': VISUAL_REQUEST})
                    deadline = time.monotonic() + budget
                    while time.monotonic() < deadline:
                        successful = [t for t in active_trace if t['name'] == 'generate_visual' and t.get('response')]
                        if successful:
                            break
                        events = host.events(session_id=session_id, resource_id=story_id, after=cursor)
                        cursor = events['cursor']
                        if events['closed'] or any(e.get('type') == 'error' for e in events['events']):
                            raise RuntimeError('visual_request_live_failed')
                        await asyncio.sleep(.25)
                    else:
                        raise TimeoutError('visual_request_timeout')
                    visual_ids = successful[-1]['args'].get('fact_ids') or []
                    after = selected_state(service, story_id)
                    scenario.update(visual_fact_ids=visual_ids,
                        visual_subset_ok=bool(visual_ids) and set(visual_ids) < set(before['selected_fact_ids']),
                        selected_ids_preserved=after['selected_fact_ids'] == before['selected_fact_ids'],
                        visual_preserved_draft=after['draft'] == before['draft'],
                        visual_preserved_concept=after['concept'] == before['concept'])
            finally:
                await host.stop(session_id=session_id, resource_id=story_id)
        status = 'REVIEW_REQUIRED'
    except Exception as exc:
        error = {'type': type(exc).__name__, 'code': str(getattr(exc, 'code', str(exc)))[:160]}
    mechanical = len(scenarios) == 2 and all(s['full_inventory_read'] and s['selected_ids_preserved']
        and len(s['selected_fact_ids']) > 2 for s in scenarios) and all(scenarios[-1].get(k)
        for k in ('visual_subset_ok', 'visual_preserved_draft', 'visual_preserved_concept')) and error is None
    result = {'acceptance': 'editorial_utilization', 'status': status if mechanical else 'FAIL',
        'mechanical_pass': mechanical, 'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'source_story_id': corpus['source_story_id'], 'eligible_corpus_count': len(corpus['facts']),
        'previous_story_id': previous_id, 'scenarios': scenarios, 'tool_trace': traces, 'error': error,
        'imagegen_dispatches': 0, 'telegram_dispatches': 0, 'production_writes': 0,
        'visual_scope': 'Real generate_visual queues a request in the test store; no worker/imagegen is run.',
        'transport_scope': 'existing shared guarded Live host with natural owner text; no physical mic or Android claim'}
    diff = subprocess.check_output(['git', 'diff', 'HEAD', '--', 'backend'], text=True)
    result['tracked_diff_sha256'] = hashlib.sha256(diff.encode()).hexdigest()
    result['tracked_tree_clean'] = not bool(diff)
    path = case_dir / 'acceptance.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'receipt': str(path), 'status': result['status'], 'mechanical_pass': mechanical,
                      'selected_counts': [len(s['selected_fact_ids']) for s in scenarios], 'error': error}), flush=True)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-db', type=Path)
    parser.add_argument('--story-id')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budget', type=int, default=180)
    parser.add_argument('--assess-receipt', type=Path)
    parser.add_argument('--assessment', type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if not root.is_relative_to('/home/dev/artifacts') or not (root / '.artifact.json').is_file():
        parser.error('output must be a managed artifact directory')
    if args.assess_receipt:
        if not args.assessment:
            parser.error('assessment required')
        receipt = json.loads(args.assess_receipt.read_text())
        result = assess(receipt, json.loads(args.assessment.read_text()))
        (root / 'editorial-acceptance-assessed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not args.source_db or not args.story_id:
            parser.error('source-db and an already identified story-id required')
        for path in [Path('/home/dev/.local/state/street-story/providers.env'),
                     Path('/home/dev/.local/state/street-story/service.env')]:
            os.environ.update(parse_dotenv(path))
        result = await run(root, frozen_corpus(args.source_db, args.story_id), args.budget)
    return 0 if result['status'] == 'PASS' else 2 if result['status'] == 'REVIEW_REQUIRED' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
