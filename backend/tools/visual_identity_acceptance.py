"""Run the owner gate photo through the actual managed Live visual tools.

An isolated store is required. No worker, publication or image generation runs.
Optional source seeds must be real search results, never image URLs or verdicts.
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
import time

from devcoveer_story_diag import load_installer


async def run(args):
    from street_story.config import Settings
    from street_story.runtime import RuntimeStreetStoryService
    from street_story.live import create_live_host
    from street_story.article_media import article_candidates
    output = args.output.resolve()
    if not output.is_relative_to('/home/dev/artifacts') or not (output / '.artifact.json').is_file():
        raise ValueError('Use a managed artifact task directory')
    logging.basicConfig(filename=output / 'live-acceptance.log', level=logging.INFO)
    installer = load_installer()
    for path in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        os.environ.update(installer.parse_dotenv(path))
    settings = replace(Settings.from_env(), data_dir=output / args.run_name,
        vibepublish_base_url=None, vibepublish_bearer_token=None)
    service = RuntimeStreetStoryService(settings)
    photo = args.photo.read_bytes()
    story = service.create_story(key='visual-acceptance', client_story_id='visual-acceptance',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/png', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.709614, lon=20.538257)
    # Exercise the explicit no-Wikipedia-confirmation boundary. No claim is
    # made that the model failed this fixture on every Wikipedia viewpoint.
    identity = {'status': 'uncertain', 'candidate_name': 'Закхаймские ворота',
        'observations': ['В этой проверке нет визуального подтверждения по Википедии.'],
        'candidates': [], 'photo_sha256': hashlib.sha256(photo).hexdigest()}
    if args.sources:
        sources = json.loads(args.sources.read_text())
        identity['candidates'] = await article_candidates(service, story, sources, set())
        if args.start_image > 1:
            for candidate in identity['candidates']:
                candidate['reference_image_urls'] = candidate['reference_image_urls'][args.start_image - 1:]
        print(json.dumps({'seeded_article_candidates': len(identity['candidates']),
            'illustrations': sum(len(c['article_media']) for c in identity['candidates'])}), flush=True)
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='needs_review',research_json=? WHERE id=?", (
            json.dumps({'visual_identity': identity, 'identity_generation': 0, 'identity_attempted_generation': 0}), story['id']))
        db.execute("UPDATE jobs SET state='cancelled' WHERE story_id=?", (story['id'],))
    host = create_live_host(service, settings)
    actor = {'subject': 'street-story-device', 'tenant_id': 'street-story'}
    session_id, cursor = '', 0
    events, progress = [], []
    started = time.monotonic()
    try:
        session = await host.start(resource_id=story['id'], actor=actor)
        session_id = session['session_id']
        print(json.dumps({'live_started': session_id, 'model': 'gemini-3.8-live'}), flush=True)
        await host.input(session_id=session_id, resource_id=story['id'], actor=actor, message={'text':
            'Определи объект по исходному фото. Гипотеза — Закхаймские ворота, но визуального подтверждения '
            'по Википедии пока нет. Вызови compare_place_images с этой поисковой гипотезой. '
            'Внимательно сравни SOURCE с каждым REF и вызови record_place_comparison. '
            'Если доказанного совпадения нет, продолжай следующие группы. Нужны только идентификация и визуальные доказательства.'})
        deadline = time.monotonic() + 300
        last_count = -1
        while time.monotonic() < deadline:
            response = host.events(session_id=session_id, resource_id=story['id'], actor=actor, after=cursor)
            cursor = response['cursor']
            for event in response['events']:
                if event.get('type') not in {'audio', 'output_audio'}:
                    events.append(event)
                if event.get('type') in {'tool_result', 'error', 'resource_fallback'}:
                    print(json.dumps({k:event.get(k) for k in ('type','name','status','code')}, ensure_ascii=False), flush=True)
            current = service.story(story['id'])
            projection = current.get('identity_progress') or {}
            count = projection.get('images_reviewed_count', 0)
            if count != last_count:
                progress.append({'elapsed_ms': round((time.monotonic()-started)*1000), 'count': count,
                                 'verified': projection.get('visual_comparison_verified', False)})
                last_count = count
                print(json.dumps({'reviewed': count, 'state': current['state']}), flush=True)
            if (current.get('visual_identity') or {}).get('status') == 'match':
                break
            if response.get('closed') or any(e.get('type') in {'error', 'resource_fallback'} for e in response['events']):
                break
            await asyncio.sleep(.5)
    except Exception as exc:
        events.append({'type': 'acceptance_error', 'error_type': type(exc).__name__, 'message': settings.redact(str(exc))[:300]})
        print(json.dumps(events[-1]), flush=True)
    finally:
        if session_id:
            await host.stop(session_id=session_id, resource_id=story['id'], actor=actor)
        await host.stop_all()
    result = service.story(story['id'])
    report = {'photo_sha256': hashlib.sha256(photo).hexdigest(), 'live_model': 'gemini-3.8-live',
        'same_live_session': session_id, 'discovery_seeded': bool(args.sources),
        'no_wikipedia_confirmation_boundary': True, 'progress_samples': progress,
        'gallery_start_image': args.start_image,
        'identity': result.get('visual_identity'), 'progress': result.get('identity_progress'),
        'events': events, 'elapsed_ms': round((time.monotonic()-started)*1000),
        'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in Path(__file__).resolve().parents[1].joinpath('street_story').glob('*.py')}}
    (output / f'{args.run_name}-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'matched': (result.get('visual_identity') or {}).get('status') == 'match',
        'candidate': (result.get('visual_identity') or {}).get('candidate_name'),
        'processed_illustrations': (result.get('identity_progress') or {}).get('images_reviewed_count',0),
        'report': str(output / f'{args.run_name}-report.json')}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--photo', type=Path, default=Path(__file__).parent / 'fixtures/zachheim-owner-20260928/source.png')
    parser.add_argument('--sources', type=Path)
    parser.add_argument('--run-name', default='live-visual-acceptance')
    parser.add_argument('--start-image', type=int, default=1, help='Exercise a later actual gallery image as the first group')
    args = parser.parse_args()
    if not args.run_name.replace('-', '').isalnum():
        parser.error('Use a bounded run name')
    if not 1 <= args.start_image <= 1000:
        parser.error('Invalid gallery start')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
