"""Bounded fact-only qualification of the existing native research adapter.

Uses normal EXIF intake, explicit mapped selection, durable job admission and
ledger writes. No app/voice/publication acceptance is implied. Qualification
calls an unqualified configured route directly; it never installs a fake proof.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

from devcoveer_story_diag import load_installer
from identity_corpus import NoPublication
from owner_place_acceptance import registry_item
from product_recovery_acceptance import managed, require_frozen_checkout, save

BACKEND = Path(__file__).resolve().parents[1]


async def recover_assessments(args):
    """An explicit write-only segment over the original closed native output."""
    sys.path.insert(0, str(BACKEND))
    sha = require_frozen_checkout(args.expected_sha)
    source, output = managed(args.recover_assessments_from), managed(args.output)
    if output.exists() or not args.attempt_id:
        raise ValueError('Use a fresh segment and an explicit original native attempt')
    output.mkdir(mode=0o700)
    installer = load_installer()
    for path in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(path, 0o600)
        os.environ.update(installer.parse_dotenv(path))
    os.environ.update(DATA_DIR=str(source/'data'), STREET_STORY_DEPLOY_SHA=sha,
        STREET_STORY_FACT_AGENT_ROUTES=json.dumps([{'provider_id': 'codex_native', 'model_id': 'gpt-6-luna'}]),
        VIBEPUBLISH_BASE_URL='', VIBEPUBLISH_BEARER_TOKEN='', STREET_STORY_NATIVE_VISION_RESERVE='0')
    logging.basicConfig(filename=output/'writes.log', level=logging.INFO)
    from street_story.config import Settings
    from street_story.runtime import RuntimeStreetStoryService
    from street_story.native_fact_contract import source_candidates
    from street_story.fact_agents import _finish
    from street_story.headless_facts import HeadlessFacts
    from street_story.research_runs import begin_research_run
    from street_story.research_budget import finish_attempt
    service = RuntimeStreetStoryService(Settings.from_env())
    service.providers.vibepublish = NoPublication()
    report = {'status': 'PREPARING', 'mode': 'closed_native_assessment_write_recovery',
        'source_sha': sha, 'original_campaign': str(source), 'original_attempt_id': args.attempt_id,
        'new_inference_sent': False, 'new_search_or_fetch': False, 'cold_acceptance': False}
    try:
        with service.store.connection() as db:
            row = db.execute('SELECT receipt_json,story_id FROM research_provider_attempts WHERE attempt_id=?',
                (args.attempt_id,)).fetchone()
            if not row:
                raise ValueError('Original native receipt missing')
            original_bytes = row['receipt_json']
            receipt, sid = json.loads(original_bytes), row['story_id']
            active = db.execute("SELECT 1 FROM jobs WHERE story_id=? AND state IN ('ready','retry','running')",
                (sid,)).fetchone()
            if active or receipt.get('phase') != 'completed' or receipt.get('native_terminal_status') != 'completed':
                raise ValueError('Finish the original native operation before write recovery')
        item, _ = registry_item(args.registry, args.message)
        if receipt['binding']['photo_sha256'] != item['sha256']:
            raise ValueError('Recovery does not belong to the selected registry original')
        context = json.loads(receipt['frozen_prompt'].split('\nINPUT=', 1)[1])
        origin_run = context['run_id']
        with service.store.connection() as db:
            run = dict(db.execute('SELECT * FROM research_runs WHERE run_id=? AND story_id=?', (origin_run, sid)).fetchone())
        before = {fact['fact_id'] for fact in service.story(sid)['facts']}
        key = 'native-closed-write:' + hashlib.sha256(str(output).encode()).hexdigest()
        goal = 'Сохрани законченные подтверждения исходного исследования без повторного поиска или проверки моделью.'
        service._research_request(sid, key, {'coverage_goal': goal, 'extraction_scope': key,
            'expected_identity_generation': receipt['binding']['generation'],
            'expected_research_control_revision': receipt['binding']['control_revision']})
        job = service._claim(claim_kind='research', claim_story_id=sid)
        if not job:
            raise ValueError('Normal write-recovery request was not claimed')
        with service.store.tx() as db:
            current = service._story_row(db, sid)
            run_id = begin_research_run(db, story_id=sid, poi_key=run['poi_key'], goal=goal, scope=key,
                expected_story_revision=current['revision'], identity_generation=receipt['binding']['generation'],
                run_id=None, now=service.store.now())
        headless = HeadlessFacts(service)
        snapshot = headless._snapshot(job, run_id)
        if not snapshot:
            raise ValueError('Write-recovery owner is stale')
        epoch = snapshot[0]['_fact_research_control_revision']
        report.update(status='WRITING', story_id=sid, job_id=job['id'], run_id=run_id,
            origin_run_id=origin_run, before_fact_ids=sorted(before))
        save(output/'segment.json', report)
        agent = {'provider_id': 'codex_native', 'model_id': 'gpt-6-luna',
            'attempt_id': args.attempt_id, 'route_id': json.loads((source/'campaign.json').read_text())['route_id'],
            'prompt_sha256': receipt['prompt_sha256'], 'recovery_origin_run_id': origin_run}
        outcomes = [await service.confirm_agent_fact(job, run_id, candidate, agent)
            for candidate in source_candidates(receipt['raw_result'])['facts']]
        pending = any(value['status'] == 'unverified' for value in outcomes)
        outcome = _finish(service.providers.research, job, run_id, headless, epoch,
            'original_native_assessments_written', pending=pending)
        with service.store.tx() as db:
            finish_attempt(service, db, sid, outcome=outcome['outcome'], reason=outcome['reason'],
                job=job, purpose='facts', coverage_complete=not pending)
        current = service.story(sid)
        after = {fact['fact_id'] for fact in current['facts']}
        with service.store.connection() as db:
            unchanged = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (args.attempt_id,)).fetchone()[0] == original_bytes
        report.update(status='RECOVERED' if unchanged and not pending else 'INCOMPLETE', outcomes=outcomes,
            added_fact_ids=sorted(after-before), total=len(after), preserved_previous=before.issubset(after),
            original_native_receipt_unchanged=unchanged)
        save(output/'material.json', current)
    except BaseException as exc:
        report.update(status='INCOMPLETE', error_type=type(exc).__name__)
        raise
    finally:
        save(output/'segment.json', report)
        await service.providers.research.close()


async def run(args):
    sys.path.insert(0, str(BACKEND))
    sha = require_frozen_checkout(args.expected_sha)
    output = managed(args.output)
    recovering = args.resume_closed_native
    if output.exists() and not recovering:
        raise ValueError('Existing original operation must be reconciled; never replay this campaign')
    if not recovering:
        output.mkdir(mode=0o700)
    installer = load_installer()
    for path in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(path, 0o600)
        os.environ.update(installer.parse_dotenv(path))
    os.environ.update(DATA_DIR=str(output/'data'), STREET_STORY_DEPLOY_SHA=sha,
        STREET_STORY_FACT_AGENT_ROUTES=json.dumps([{'provider_id': 'codex_native', 'model_id': 'gpt-6-luna'}]),
        VIBEPUBLISH_BASE_URL='', VIBEPUBLISH_BEARER_TOKEN='', STREET_STORY_NATIVE_VISION_RESERVE='0')
    logging.basicConfig(filename=output/'research.log', level=logging.INFO)
    from street_story.config import Settings
    from street_story.runtime import RuntimeStreetStoryService
    from street_story.fact_agents import configured_routes, native_agent_context, _finish
    from street_story.fact_agent_providers import run_tool_agent
    from street_story.headless_facts import HeadlessFacts
    from street_story.place_selection import place_map, prepare_place_context, confirm_place
    from street_story.research_runs import begin_research_run
    from street_story.research_budget import finish_attempt

    item, data = registry_item(args.registry, args.message)
    if item['camera_gps'].get('status') != 'verified_exif':
        raise ValueError('Only original files with internal verified GPS may be tested')
    service = RuntimeStreetStoryService(Settings.from_env())
    service.providers.vibepublish = NoPublication()
    adapter = service.providers.research
    route = next(route for route in configured_routes(adapter, include_unqualified=True)
        if route.provider_id == 'codex_native')
    campaign = {'status': 'PREPARING', 'mode': 'native_fact_only_qualification', 'source_sha': sha,
        'message_id': args.message, 'photo_sha256': item['sha256'], 'route_id': route.route_id,
        'profile_sha256': route.profile_fingerprint, 'attempts': [], 'publication_dispatches': 0,
        'app_e2e': False, 'reference_facts_or_urls_supplied': False, 'cost': 'unknown'}
    original_receipt = None
    if recovering:
        from street_story.research_adapter import _closed_native_input_readback
        campaign = json.loads((output/'campaign.json').read_text())
        if (campaign.get('status') != 'INCOMPLETE' or campaign.get('message_id') != args.message
                or campaign.get('profile_sha256') != route.profile_fingerprint
                or len(campaign.get('attempts', [])) != 1):
            raise ValueError('Only the original unchanged native input-readback failure may resume')
        with service.store.connection() as db:
            rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=? '
                "AND role LIKE 'facts_agent_%'", (campaign['story_id'],)))
        if len(rows) != 1:
            raise ValueError('Original native operation is ambiguous')
        original_receipt = json.loads(rows[0][0])
        if not _closed_native_input_readback(original_receipt):
            raise ValueError('Original native turn is not eligible for input-readback recovery')
        save(output/'campaign-before-readback-recovery.json', campaign)
        save(output/'receipt-before-readback-recovery.json', original_receipt)
        campaign.update(status='RECOVERING_ORIGINAL_READBACK', recovery_code_sha=sha,
            recovery_started_at=time.time(), recovery_new_inference_sent=False)
    save(output/'campaign.json', campaign)
    try:
        token = 'native-fact-only:' + hashlib.sha256(str(output).encode()).hexdigest()
        if recovering:
            sid, selected = campaign['story_id'], campaign['selected_candidate_id']
            if service.story(sid)['photo_sha256'] != item['sha256']:
                raise ValueError('Original selected photo changed')
        else:
            story = service.create_story(key=token, client_story_id=token, photo_sha256=item['sha256'],
                photo_mime_type=item['mime'], photo_bytes=data, voice_protocol='voice-chunks-v2',
                lat=None, lon=None, identity_mode='manual')
            sid = story['id']
            campaign['story_id'] = sid
            snapshot = await place_map(service, sid)
            selected = next(feature['id'] for feature in snapshot['buildings']['features']
                if feature['id'] in item['expected_physical_ids'])
            body = {'mode': 'exact', 'command_id': token+':place', 'snapshot_id': snapshot['snapshot_id'],
                'candidate_id': selected, 'expected_photo_sha256': item['sha256'],
                'expected_identity_generation': story['identity_generation'], 'expected_candidate_id': None}
            await prepare_place_context(service, sid, body)
            confirmed = confirm_place(service, sid, body['command_id'], body)
            campaign.update(selected_candidate_id=selected, status='RESEARCHING')
            save(output/'selection.json', confirmed)
        save(output/'campaign.json', campaign)
        for ordinal in range(args.additions+1):
            if ordinal:
                key = token+':more:'+str(ordinal)
                request = {'coverage_goal': 'Найди ещё полезные сведения о выбранном здании. '
                    'Сохрани известные факты и все источники, исследуй содержательные пробелы, '
                    'не выдавай перефразирование за новый факт.', 'extraction_scope': key}
                service._research_request(sid, key, request)
                service._research_request(sid, key, request)  # Same delivered command joins.
            else:
                service._schedule_confirmed_facts()
            job = service._claim(claim_kind='research', claim_story_id=sid)
            if not job:
                raise ValueError('Ordinary fact research request was not claimed')
            recovery_segment = recovering and ordinal == 0
            if recovery_segment and job['id'] != original_receipt['binding']['job_id']:
                raise ValueError('Original native job changed')
            payload = json.loads(job['payload_json'])
            goal = payload.get('coverage_goal') or 'Исследуй содержательную историю выбранного здания.'
            if recovery_segment:
                run_id = campaign['attempts'][0]['run_id']
            else:
                with service.store.tx() as db:
                    current = service._story_row(db, sid)
                    research = json.loads(current['research_json'])
                    run_id = begin_research_run(db, story_id=sid, poi_key=selected, goal=goal,
                        scope=payload.get('extraction_scope'), expected_story_revision=current['revision'],
                        identity_generation=research['identity_generation'], run_id=None, now=service.store.now())
            headless = HeadlessFacts(service)
            owned, research, _ = headless._snapshot(job, run_id)
            epoch = owned['_fact_research_control_revision']
            context = (json.loads(original_receipt['frozen_prompt'].split('\nINPUT=', 1)[1])
                if recovery_segment else native_agent_context(service, owned, research['visual_identity'], goal, run_id))
            unit = {'run_id': run_id, 'context': context, 'route_id': route.route_id}
            digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            owned.update(_fact_pool_unit_id='agent_'+digest[:24], _fact_pool_input_sha256=digest)
            before = {fact['fact_id'] for fact in service.story(sid)['facts']}
            record = {'ordinal': ordinal, 'job_id': job['id'], 'run_id': run_id,
                'started_at': time.time(), 'before_fact_ids': sorted(before), 'status': 'RUNNING'}
            if recovery_segment:
                record.update(original_started_at=campaign['attempts'][0]['started_at'],
                    mode='original_native_readback_and_write_recovery', new_inference_sent=False,
                    original_research_sha=campaign['source_sha'], recovery_code_sha=sha)
                campaign['attempts'][0] = record
            else:
                campaign['attempts'].append(record)
            save(output/'campaign.json', campaign)
            def guard():
                if headless._snapshot(job, run_id, epoch) is None:
                    from street_story.service import ConflictError
                    raise ConflictError('native_acceptance_owner_stale', 'Selected object, Stop or job lease changed')

            async def observe():
                while True:
                    now = service.store.now()
                    with service.store.tx() as db:
                        db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND attempts=?",
                            (now+90, job['id'], job['attempts']))
                    facts = service.story(sid)['facts']
                    row = {'elapsed_seconds': time.time()-record['started_at'],
                        'fact_ids': [fact['fact_id'] for fact in facts if fact.get('eligibility') == 'eligible']}
                    with (output/f'observations-{ordinal}.jsonl').open('a') as stream:
                        stream.write(json.dumps(row)+'\n')
                    await asyncio.sleep(1)

            observer = asyncio.create_task(observe())
            try:
                async def invoke(binding):
                    return await run_tool_agent(adapter, route, owned, context, binding=binding, guard=guard)
                result = await adapter.run(owned, 'facts_agent_'+route.route_id, unit, invoke, client=route)
                receipt = result['receipt']
                save(output/f'receipt-{ordinal}.json', receipt)
                agent = {'route_id': route.route_id, 'provider_id': route.provider_id, 'model_id': route.model_id,
                    'attempt_id': receipt['binding']['attempt_id'], 'prompt_sha256': receipt['prompt_sha256']}
                outcomes = []
                for candidate in result['result']['facts']:
                    outcomes.append(await service.confirm_agent_fact(job, run_id, candidate, agent))
                pending = any(row['status'] == 'unverified' for row in outcomes)
                outcome = _finish(adapter, job, run_id, headless, epoch, 'native_fact_only_completed', pending=pending)
                with service.store.tx() as db:
                    finish_attempt(service, db, sid, outcome=outcome['outcome'], reason=outcome['reason'],
                        job=job, purpose='facts', coverage_complete=not pending)
                current = service.story(sid)
                after = {fact['fact_id'] for fact in current['facts']}
                record.update(status='COMPLETED', attempt_id=agent['attempt_id'],
                    elapsed_seconds=time.time()-record['started_at'], added_fact_ids=sorted(after-before),
                    preserved_previous=before.issubset(after), total=len(after), usage=receipt.get('usage'), outcomes=outcomes)
                save(output/f'material-{ordinal}.json', current)
                save(output/'campaign.json', campaign)
                if not any(fact.get('eligibility') == 'eligible' for fact in current['facts']):
                    raise ValueError('No useful admitted material; fix the affected control before another research attempt')
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
        campaign['status'] = 'MEASURED'
    except BaseException as exc:
        campaign.update(status='INCOMPLETE', error_type=type(exc).__name__,
            error_code=str(exc) if type(exc).__name__ in {'ValueError', 'RetryableProviderError'} else type(exc).__name__)
        raise
    finally:
        save(output/'campaign.json', campaign)
        await adapter.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--message', type=int, choices=[102, 105, 131], required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--additions', type=int, choices=[0, 1, 2], default=0)
    parser.add_argument('--resume-closed-native', action='store_true',
        help='Observe the original completed turn after a local input-readback projection failure; never resend it')
    parser.add_argument('--recover-assessments-from', type=Path,
        help='Write complete original native assessments in a separate measured segment; no inference or source replay')
    parser.add_argument('--attempt-id')
    args = parser.parse_args()
    asyncio.run(recover_assessments(args) if args.recover_assessments_from else run(args))
