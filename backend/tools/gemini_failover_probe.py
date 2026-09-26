"""Explicit operator canary: real providers, only the first error may be injected.

Run from backend with python -m tools.gemini_failover_probe. Never against either
resident DATA_DIR. Secrets are read from the environment, never command arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

from street_story.config import Settings
from street_story.service import StreetStoryService


class SimulatedRateLimit(Exception):
    code = 429
    status = 'RESOURCE_EXHAUSTED'
    details = {'error': {'details': [{'retryDelay': '120s'}]}}


async def run(args):
    settings = Settings.from_env()
    target = args.data_dir.expanduser().resolve()
    if target == settings.data_dir or target.exists():
        raise ValueError('Canary DATA_DIR must be a new isolated directory, not the resident store')
    target.mkdir(mode=0o700, parents=True)
    settings = replace(settings, data_dir=target)
    service = StreetStoryService(settings)
    gemini = service.providers.gemini
    original = gemini._provider_request
    calls = []
    injected = False

    async def generate(key, timeout, contents, config=None):
        nonlocal injected
        operation = 'grounded_research' if config and config.tools else 'transcription'
        slot = next(i+1 for i, value in enumerate(gemini.pool.keys) if value.get_secret_value() == key)
        record = {'slot':slot,'operation':operation,'simulated':False}
        calls.append(record)
        if args.simulate_first_retryable and not injected and operation == args.operation:
            injected = True
            record.update(simulated=True, result='429')
            raise SimulatedRateLimit('explicit operator canary')
        try:
            result = await original(key, timeout, contents, config)
            record['result'] = 'real_provider_success'
            return result
        except Exception:
            record['result'] = 'real_provider_error'
            raise

    gemini._provider_request = generate
    photo, audio = args.photo.read_bytes(), args.voice.read_bytes()
    name = 'reliability-'+uuid.uuid4().hex
    story = service.create_story(key=name,client_story_id=name,photo_sha256=hashlib.sha256(photo).hexdigest(),photo_mime_type='image/jpeg',photo_bytes=photo,voice_protocol='voice-chunks-v2',lat=54.7065,lon=20.5123)
    sid = story['id']
    service.open_voice(sid, name+'-voice', {'session_id':name,'kind':'initial'})
    sha = hashlib.sha256(audio).hexdigest()
    service.put_chunk(sid,name,0,name+'-chunk',sha,audio,{'start_ms':0,'end_ms':args.duration_ms,'wall_start_ms':0,'wall_end_ms':args.duration_ms},'audio/mp4')
    service.complete_voice(sid,name,name+'-complete',{'chunks':[{'index':0,'sha256':sha}],'chunk_count':1})
    await service.run_once()
    with service.store.connection() as db:
        job = db.execute('SELECT state,attempts,available_at FROM jobs WHERE story_id=?',(sid,)).fetchone()
        transcript = db.execute('SELECT transcript FROM voice_sessions WHERE session_id=?',(name,)).fetchone()[0]
    story = service.story(sid)
    report = {'story_id':sid,'configured_slots':len(gemini.pool.keys),'calls':calls,'job':dict(job),'story_state':story['state'],
              'transcript_persisted':transcript is not None,'facts_count':len(story['facts']), 'simulated_success':False,
              'pool':{op:gemini.pool.snapshot(op) for op in ('transcription','grounded_research')},'stand':'isolated real-provider service canary'}
    # Verify fresh service reconstructs durable health/checkpoints without calling a provider.
    restarted = StreetStoryService(settings)
    report['restart'] = {'same_story':restarted.story(sid)['id']==sid,
                         'pool':{op:restarted.providers.gemini.pool.snapshot(op) for op in ('transcription','grounded_research')}}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--photo', required=True, type=Path)
    parser.add_argument('--voice', required=True, type=Path)
    parser.add_argument('--duration-ms', required=True, type=int)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--simulate-first-retryable', action='store_true')
    parser.add_argument('--operation', choices=('transcription','grounded_research'), default='transcription')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
