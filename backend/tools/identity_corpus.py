"""Run photo identity against a retained corpus with deployed provider configuration.

Full-worker mode uses the application lifespan and ordinary queues in a cold database
per photo. Results are incremental; labels never enter model input. No publication.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time

from devcoveer_story_diag import load_installer


class NoPublication:
    def __getattr__(self, name):
        raise RuntimeError('Publication is disabled for the identity corpus')


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


async def full_worker_case(settings, output, args, item, data):
    from street_story.app import create_app
    from street_story.runtime import RuntimeStreetStoryService
    from street_story.service import canonical
    case_settings = replace(settings, data_dir=output / f'data-{item["message_id"]}')
    service = RuntimeStreetStoryService(case_settings)
    service.providers.vibepublish = NoPublication()
    installer = load_installer()
    installer.require_mode(installer.RESEARCH_QUALIFICATION, 0o600)
    qualification = json.loads(installer.RESEARCH_QUALIFICATION.read_text())
    for evidence in qualification['evidence']:
        path = Path(evidence['path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Provider qualification evidence changed')
    # Provider transport qualification is global evidence, not POI knowledge.
    # A cold case must keep the deployed routes available without seeding any
    # building, article, identity, permission grant or previous comparison.
    for key, value in qualification['caches'].items():
        service.store.cache_put(key, value, 3600)
    app = create_app(case_settings, service)
    story_id = None
    async with app.router.lifespan_context(app):
        story = service.create_story(key=f'corpus:{args.run_name}:{item["message_id"]}',
            client_story_id=f'corpus:{args.run_name}:{item["message_id"]}',
            photo_sha256=hashlib.sha256(data).hexdigest(),
            photo_mime_type=item['mime'], photo_bytes=data,
            voice_protocol='voice-chunks-v2', lat=None, lon=None)
        story_id = story['id']
        service.ensure_identity(story_id)
        deadline = time.monotonic() + args.timeout_seconds
        waiting = True
        while True:
            current, research = service._identity_snapshot(story_id)
            identity = research.get('visual_identity') or {}
            if identity.get('status') == 'match' or current.get('error_code') == 'visual_identity_conflict':
                break
            with service.store.connection() as db:
                waiting = db.execute("SELECT 1 FROM jobs WHERE story_id=? AND kind IN ('identity','identity_visual') AND state IN ('ready','retry','running')", (story_id,)).fetchone()
            if not waiting or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.5)
        facts_result = None
        if getattr(args, 'wait_facts', False) and identity.get('status') == 'match':
            # No WebSocket/Live session or owner instruction is created. The
            # ordinary scheduler must collect, verify and persist on its own.
            facts_deadline = time.monotonic() + args.facts_timeout_seconds
            while True:
                with service.store.connection() as db:
                    accepted = [dict(row) for row in db.execute(
                        "SELECT f.fact_id,f.text,a.review_status,a.eligibility FROM facts f "
                        "JOIN fact_assertions a ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                        "WHERE f.story_id=? AND a.eligibility='eligible'", (story_id,))]
                    canonical_facts = [dict(row) for row in db.execute(
                        "SELECT assertion_id,text,eligibility,review_proof_json FROM poi_research_assertions "
                        "WHERE eligibility='eligible'")]
                    live_count = db.execute('SELECT COUNT(*) FROM live_messages WHERE story_id=?', (story_id,)).fetchone()[0]
                    story_now = service._story_row(db, story_id)
                proved = [fact for fact in canonical_facts if json.loads(fact.get('review_proof_json') or '{}').get('detector') == 'backend_semantic_review']
                facts_result = {'facts': accepted, 'canonical_facts': canonical_facts,
                    'backend_proved_count': len(proved), 'live_message_count': live_count,
                    'elapsed_since_upload_s': service.store.now() - story_now['created_at']}
                if (proved and accepted) or time.monotonic() >= facts_deadline:
                    break
                await asyncio.sleep(.5)
            current, research = service._identity_snapshot(story_id)
            (output / f'facts-{item["message_id"]}.json').write_text(canonical(facts_result) + '\n')
        with service.store.connection() as db:
            attempts = [dict(row) for row in db.execute('SELECT role,receipt_json,created_at,updated_at FROM research_provider_attempts WHERE story_id=?', (story_id,))]
            events = [dict(row) for row in db.execute("SELECT event_type,payload_json,created_at FROM live_diagnostics WHERE story_id=? AND source='identity' ORDER BY id", (story_id,))]
        (output / f'evidence-{item["message_id"]}.json').write_text(canonical({'identity': identity,
            'search': research.get('identity_article_discovery'), 'visual_operation': research.get('visual_search_operation'),
            'attempts': attempts, 'events': events}) + '\n')
        return {'story_id': story_id, 'identity': identity, 'state': current['state'],
                'error_code': current.get('error_code'), 'full_workers': True,
                'cold_poi_memory': True, 'operator_label_supplied': False,
                **({'facts_result': facts_result} if facts_result is not None else {}),
                'terminal': not waiting if identity.get('status') != 'match' else True}


async def run(args):
    from street_story.config import Settings
    from street_story.runtime import RuntimeStreetStoryService
    corpus = args.corpus.resolve()
    if not corpus.is_relative_to('/home/dev/artifacts') or not (corpus / '.artifact.json').is_file():
        raise ValueError('Use a retained managed private corpus')
    manifest = json.loads((corpus / 'corpus.json').read_text())
    if not manifest.get('complete') or not manifest.get('original_bytes'):
        raise ValueError('Corpus acquisition must be complete and byte-exact')
    output = corpus / args.run_name
    output.mkdir(mode=0o700, exist_ok=True)
    logging.basicConfig(filename=output / 'identity.log', level=logging.INFO)
    installer = load_installer()
    for config in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(config, 0o600)
        os.environ.update(installer.parse_dotenv(config))
    settings = replace(Settings.from_env(), data_dir=output / 'data',
                       vibepublish_base_url=None, vibepublish_bearer_token=None)
    if args.model:
        settings = replace(settings, gemini_model=args.model)
    service = RuntimeStreetStoryService(settings)
    service.providers.vibepublish = NoPublication()
    code = Path(__file__).resolve().parents[1] / 'street_story'
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(code.glob('*.py'))}
    config = {'models': {'identity': settings.gemini_model,
                        'search_routes': [route[0] for route in service.providers.gemini.web_search_routes]},
              'source_hashes': source_hashes, 'identity_only': not args.wait_facts, 'full_workers': args.full_workers}
    config_file = output / 'run.json'
    if config_file.exists() and json.loads(config_file.read_text()) != config:
        raise ValueError('Source or model configuration changed; use a new run name')
    save(config_file, config)
    report_path = output / 'results.json'
    results = json.loads(report_path.read_text()) if report_path.exists() else []
    completed = {item['message_id'] for item in results}
    selection = {int(x) for x in args.messages.split(',')} if args.messages else None
    print(json.dumps({'run': args.run_name, 'models': config['models'], 'already_completed': len(results)}), flush=True)
    for item in manifest['items']:
        if item['message_id'] in completed or (selection is not None and item['message_id'] not in selection):
            continue
        photo = (corpus / item['path']).resolve()
        if not photo.is_relative_to(corpus):
            raise ValueError('Corpus path escapes its retained directory')
        data = photo.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Corpus original hash mismatch')
        started = time.monotonic()
        case = {'message_id': item['message_id'], 'input_sha256': item['sha256'],
                'gps_present': item['gps_present']}
        try:
            if args.full_workers:
                case.update(await full_worker_case(settings, output, args, item, data))
            else:
                story = service.create_story(key=f'corpus:{args.run_name}:{item["message_id"]}',
                    client_story_id=f'corpus:{args.run_name}:{item["message_id"]}',
                    photo_sha256=item['sha256'], photo_mime_type=item['mime'], photo_bytes=data,
                    voice_protocol='voice-chunks-v2', lat=None, lon=None)
                result = await asyncio.wait_for(service.resolve_identity(story['id']), timeout=args.timeout_seconds)
                identity = result.get('visual_identity') or {}
                case.update({'story_id': story['id'], 'identity': identity,
                             'progress': result.get('identity_progress'), 'state': result.get('state')})
        except Exception as exc:
            case['error_type'] = type(exc).__name__
            case['error'] = settings.redact(str(exc))[:300]
        case['duration_ms'] = round((time.monotonic() - started) * 1000)
        results.append(case)
        save(report_path, results)
        identity = case.get('identity') or {}
        print(json.dumps({'message_id': item['message_id'], 'status': identity.get('status'),
            'candidate': identity.get('candidate_name'), 'reference_count': len(identity.get('reference_evidence', [])),
            'duration_ms': case['duration_ms'], 'error_type': case.get('error_type')}, ensure_ascii=False), flush=True)
    print(json.dumps({'complete_for_selection': True, 'cases_recorded': len(results), 'report': str(report_path)}), flush=True)
    await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--messages', default='')
    parser.add_argument('--full-workers', action='store_true', help='Use ordinary application workers and cold per-photo POI memory')
    parser.add_argument('--wait-facts', action='store_true', help='Wait for backend-verified POI facts with no Live client')
    parser.add_argument('--facts-timeout-seconds', type=int, default=240)
    parser.add_argument('--timeout-seconds', type=int, default=300)
    parser.add_argument('--model', choices=('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite', 'gemini-3.8-flash'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.run_name):
        parser.error('Use a bounded run name')
    if args.messages and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', args.messages):
        parser.error('Invalid message selection')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
