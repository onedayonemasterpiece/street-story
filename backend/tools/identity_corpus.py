"""Run real identity only against a retained private corpus using deployed provider configuration.

No workers, image generation, Live sessions or publication. Results are incremental and
re-runs of the same run name reuse completed cases. Labels never enter model input.
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
              'source_hashes': source_hashes, 'identity_only': True}
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
            story = service.create_story(key=f'corpus:{args.run_name}:{item["message_id"]}',
                client_story_id=f'corpus:{args.run_name}:{item["message_id"]}',
                photo_sha256=item['sha256'], photo_mime_type=item['mime'], photo_bytes=data,
                voice_protocol='voice-chunks-v2', lat=None, lon=None)
            result = await asyncio.wait_for(service.resolve_identity(story['id']), timeout=180)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--messages', default='')
    parser.add_argument('--model', choices=('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite', 'gemini-3.8-flash'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.run_name):
        parser.error('Use a bounded run name')
    if args.messages and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', args.messages):
        parser.error('Invalid message selection')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
