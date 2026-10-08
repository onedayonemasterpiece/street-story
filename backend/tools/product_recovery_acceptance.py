"""Bounded photo→physical identity→reviewed POI facts acceptance on one frozen SHA.

Use a retained managed manifest/output. Expected IDs, labels and minimum counts
are report-only fields; provider input contains original photo bytes and EXIF.
Run canary --messages 102 first, then reuse this output for the other cases.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from devcoveer_story_diag import load_installer
from identity_corpus import NoPublication

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
CAPS = {'identity_seconds': 180, 'first_eligible_seconds': 300, 'total_seconds': 480}
ACTIVE = {'ready', 'retry', 'running'}


def instrument_google_sdk(client_type, path):
    """Journal the real SDK invocation boundary, independent of product receipts."""
    from street_story.headless_vision import _usage
    original = client_type._provider_request
    def append(record):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    async def measured(self, key, timeout, contents, config=None, *, model=None):
        identifier = str(uuid.uuid4())
        base = {'event': 'google_sdk_invocation', 'sdk_call_id': identifier,
                'model': model or self.settings.gemini_model}
        append({**base, 'phase': 'possibly_sent', 'observed_at': time.time()})
        try:
            response = await original(self, key, timeout, contents, config, model=model)
        except BaseException as exc:
            append({**base, 'phase': 'outcome_unavailable', 'observed_at': time.time(),
                    'error_type': type(exc).__name__, 'usage': _usage(None)})
            raise
        append({**base, 'phase': 'response_closed', 'observed_at': time.time(),
                'provider_request_id': getattr(response, 'response_id', None), 'usage': _usage(response)})
        return response
    client_type._provider_request = measured
    return lambda: setattr(client_type, '_provider_request', original)


def summarize_sdk_journal(path):
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    calls = {}
    for record in records:
        calls.setdefault(record['sdk_call_id'], {}).update(record)
    return {'scope': 'entire acceptance run; no inferred per-story allocation',
        'google_sdk_invocations': len(calls),
        'response_closed': sum(record['phase'] == 'response_closed' for record in calls.values()),
        'outcome_unavailable': sum(record['phase'] != 'response_closed' for record in calls.values()),
        'basis': 'Observed Google SDK invocation boundary after admission and before the SDK call; '
                 'possibly sent is not proof of provider response or paid usage.',
        'money': 'unknown unless a provider reports measured cost',
        'coverage_gaps': ['Other provider transports remain represented by product receipt lower bounds.',
                          'Provider-internal operations and unreported usage are unknown.'],
        'details': list(calls.values())}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    pending.chmod(0o600)
    pending.replace(path)


def managed(path):
    resolved = path.resolve()
    if not resolved.is_relative_to('/home/dev/artifacts'):
        raise ValueError('Acceptance artifacts must be under /home/dev/artifacts')
    if not any((parent / '.artifact.json').is_file() for parent in (resolved, *resolved.parents)):
        raise ValueError('Acceptance artifacts require a managed task ancestor')
    return resolved


def load_manifest(path):
    path = managed(path)
    raw = json.loads(path.read_text())
    items = raw.get('items') if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError('Manifest must contain source items')
    normalized, seen = [], set()
    for item in items:
        identifier = int(item.get('message_id', item.get('id')))
        if identifier in seen:
            raise ValueError('Duplicate manifest message ID')
        seen.add(identifier)
        original = Path(item['path'])
        original = managed(original if original.is_absolute() else path.parent / original)
        sha = str(item.get('sha256') or item.get('sha') or '').lower()
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Each source requires its exact content SHA256')
        minimum = item.get('min_useful_facts')
        if type(minimum) is not int or minimum < 1:
            raise ValueError('Fix min_useful_facts before running; do not lower it after a failure')
        normalized.append({**item, 'message_id': identifier, 'path': str(original),
            'sha256': sha, 'mime': item.get('mime', 'image/jpeg')})
    return normalized, digest(raw)


def require_frozen_checkout(expected_sha):
    if not re.fullmatch('[0-9a-f]{40}', expected_sha):
        raise ValueError('--expected-sha must be the full candidate SHA')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=no'], cwd=REPO, text=True)
    if head != expected_sha or dirty:
        raise ValueError('Acceptance requires the expected SHA and clean tracked source')
    return head


def blank_result(item):
    return {'message_id': item['message_id'], 'report_label': item.get('report_label'),
        'input_sha256': item['sha256'], 'min_useful_facts': item['min_useful_facts'],
        'expected_physical_id': item.get('expected_physical_id'), 'status': 'NOT_RUN',
        'operator_labels_supplied': False}


def upload_story(service, item, data, manifest_digest):
    """The ordinary upload contract has no acceptance labels/expected IDs."""
    return service.create_story(key=f'acceptance:{manifest_digest}:{item["message_id"]}',
        client_story_id=f'acceptance:{manifest_digest}:{item["message_id"]}',
        photo_sha256=item['sha256'], photo_mime_type=item['mime'], photo_bytes=data,
        voice_protocol='voice-chunks-v2', lat=None, lon=None)


def acceptance_status(case):
    outcome = (case.get('product_outcome') or {}).get('outcome')
    if outcome == 'resource_blocked':
        return 'BLOCKED'
    if not case.get('terminal'):
        return 'RUNNING'
    gates = case.get('gates') or {}
    if any(value is False for value in gates.values()):
        return 'FAIL'
    if gates.get('correct_physical_object') is None:
        return 'REVIEW_REQUIRED'
    return 'PASS_CANDIDATE'  # Final substantive-fact review is external to this harness.


def summarize_receipts(attempts):
    counts = {'attempts': len(attempts), 'actual_sends': 0, 'not_sent': 0, 'unknown': 0,
        'completed_empty': 0, 'failed': 0, 'completed': 0}
    sends = set()
    details = []
    for row in attempts:
        receipt = json.loads(row['receipt_json'] or '{}')
        phase = receipt.get('phase')
        send = receipt.get('provider_send_state')
        uncertain = (phase in {'prompt_intent', 'submitted', 'unknown', 'abort_outcome_unknown'}
            or phase == 'aborted' and not receipt.get('abort_acknowledged'))
        counts['unknown'] += int(uncertain)
        counts['not_sent'] += int(send == 'not_sent')
        counts['failed'] += int(phase in {'failed', 'aborted'} and not uncertain)
        counts['completed'] += int(phase in {'completed', 'response_completed'})
        result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
        source_inventory = receipt.get('sources', result.get('sources'))
        counts['completed_empty'] += int(phase in {'completed', 'response_completed'}
            and row['role'].startswith('search') and source_inventory == [])
        assistants = receipt.get('assistants') or []
        for message in assistants:
            if message.get('message_id'):
                sends.add(('assistant', message['message_id']))
        if receipt.get('turn_id'):
            sends.add(('native_turn', receipt['turn_id']))
        for index in range(int(receipt.get('text_sends') or 0)):
            sends.add(('live_text', receipt.get('session_id') or row['attempt_id'], index))
        for index, inference in enumerate(receipt.get('inference_sends') or []):
            sends.add(('inference', inference.get('attempt_id') or row['attempt_id'], index))
        observation = receipt.get('observation') or {}
        for index, model_attempt in enumerate(observation.get('model_attempts') or receipt.get('model_attempts') or []):
            if model_attempt.get('provider_send_state') in {'sent', 'possibly_sent', 'response_closed'} or model_attempt.get('response_received'):
                sends.add((row['attempt_id'], index))
        details.append({'attempt_id': row['attempt_id'], 'logical_id': row['logical_id'], 'role': row['role'],
            'phase': phase, 'provider_send_state': send, 'provider': receipt.get('provider_id', receipt.get('provider')),
            'model': receipt.get('model_id', receipt.get('model')),
            'original_ids': {key: receipt[key] for key in ('session_id', 'message_id', 'thread_id', 'turn_id') if receipt.get(key)},
            'usage': receipt.get('usage', observation.get('usage')),
            'usage_snapshots': receipt.get('usage_snapshots'), 'resource_events': receipt.get('resource_events'),
            'text_sends': receipt.get('text_sends'), 'setup_ready': receipt.get('setup_ready'),
            'inference_sends': receipt.get('inference_sends'),
            'model_attempts': observation.get('model_attempts') or receipt.get('model_attempts'),
            'assistants_usage': [{key: message.get(key) for key in ('message_id', 'tokens', 'cost', 'time')}
                for message in assistants], 'fallback_reason': receipt.get('fallback_reason', receipt.get('error_code')),
            'created_at': row['created_at'], 'updated_at': row['updated_at']})
    counts['actual_sends'] = len(sends)
    return {**counts, 'actual_sends_basis': 'Observed assistant message IDs/native turn IDs/Live text events/inference-send callbacks/explicit provider-send receipts; '
        'unknown or uninstrumented sends are reported separately, never inferred from attempt count.',
        'actual_sends_is_lower_bound': True,
        'money': 'unknown unless individual receipts provide measured cost', 'details': details}


def read_case(service, case, item):
    from street_story.poi_memory import memory_keys, _review_snapshot
    with service.store.connection() as db:
        row = service._story_row(db, case['story_id'])
        research = json.loads(row['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        jobs = [dict(job) for job in db.execute('SELECT id,kind,state,attempts,last_error,created_at,updated_at '
            'FROM jobs WHERE story_id=? ORDER BY created_at', (row['id'],))]
        story_facts = [dict(fact) for fact in db.execute('SELECT a.assertion_id,a.display_text,a.revision_digest,'
            'a.review_status,a.eligibility,a.updated_at,f.sources_json FROM fact_assertions a '
            'JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id '
            'WHERE a.story_id=? AND a.eligibility=\'eligible\'', (row['id'],))]
        keys = memory_keys(db, identity)
        canonical = [dict(fact) for fact in db.execute('SELECT * FROM poi_research_assertions WHERE poi_key IN ('
            + ','.join('?' for _ in keys) + ') AND eligibility=\'eligible\'', keys)] if keys else []
        sources = [dict(source) for source in db.execute('SELECT poi_key,url,title,first_seen_at,last_seen_at '
            'FROM poi_research_sources WHERE poi_key IN (' + ','.join('?' for _ in keys) + ')', keys)] if keys else []
        attempts = [dict(attempt) for attempt in db.execute('SELECT * FROM research_provider_attempts '
            'WHERE story_id=? ORDER BY created_at', (row['id'],))]
        events = [dict(event) for event in db.execute('SELECT source,event_type,payload_json,created_at '
            'FROM live_diagnostics WHERE story_id=? ORDER BY id', (row['id'],))]
        scans = [dict(scan) for scan in db.execute('SELECT id,detector,status,coverage_complete,created_at '
            'FROM fact_conflict_scans WHERE story_id=? ORDER BY id', (row['id'],))]
    proved = []
    for fact in story_facts:
        snapshot = list(_review_snapshot(fact['display_text'], fact['sources_json']))
        matches = []
        for persisted in canonical:
            proof = json.loads(persisted.get('review_proof_json') or '{}')
            if (persisted['assertion_id'] == fact['assertion_id'] and proof.get('detector') == 'backend_semantic_review'
                    and proof.get('scan_id') and proof.get('source_story_id')
                    and proof.get('snapshot') == snapshot and persisted['review_status'] == 'eligible'):
                matches.append({**persisted, 'review_proof': proof})
        if matches:
            proved.append({**fact, 'sources': json.loads(fact['sources_json']), 'canonical_evidence': matches})
    now, started = service.store.now(), float(row['created_at'])
    identity_at = float(identity.get('resolved_at') or now) if identity.get('status') == 'match' else None
    first_at = min((float(fact['updated_at']) for fact in proved), default=None)
    if identity_at is not None:
        case['identity_elapsed_s'] = min(case.get('identity_elapsed_s', float('inf')), max(0, identity_at - started))
    if first_at is not None:
        case['first_eligible_elapsed_s'] = min(case.get('first_eligible_elapsed_s', float('inf')), max(0, first_at - started))
    outcome = research.get('automatic_research_outcome')
    terminal = bool(outcome) or (bool(jobs) and not any(job['state'] in ACTIVE
        for job in jobs if job['kind'] in {'identity', 'identity_visual', 'research', 'refinement'}))
    # A match between stages is not terminal: the normal scheduler still needs
    # an opportunity to open its fact job. facts_ready/terminal outcome is final.
    if identity.get('status') == 'match' and not outcome and row['state'] not in {'facts_ready', 'needs_review'}:
        terminal = False
    finish = float(outcome.get('finished_at') or now) if outcome else now
    total = max(0, finish - started) if terminal else max(0, now - started)
    candidate = next((candidate for candidate in identity.get('candidates') or []
        if candidate.get('candidate_id') == identity.get('candidate_id')), {})
    expected = item.get('expected_physical_id')
    gates = {'identity_in_180s': case.get('identity_elapsed_s', float('inf')) <= CAPS['identity_seconds'],
        'first_eligible_in_300s': case.get('first_eligible_elapsed_s', float('inf')) <= CAPS['first_eligible_seconds'],
        'terminal_in_480s': terminal and total <= CAPS['total_seconds'],
        'eligible_minimum': len(proved) >= item['min_useful_facts'],
        'correct_physical_object': identity.get('candidate_id') == expected if expected else None,
        'visual_proof': identity.get('status') == 'match' and identity.get('visual_reference_verified') is True,
        'canonical_poi_readback': len(proved) >= item['min_useful_facts']}
    live_receipts = [json.loads(attempt['receipt_json'] or '{}') for attempt in attempts]
    live_receipts = [receipt for receipt in live_receipts if receipt.get('provider_id') == 'google-live']
    case.update(status='RUNNING', uploaded_at=started, elapsed_from_upload_s=max(0, now-started),
        total_elapsed_s=total, terminal=terminal, state=row['state'], error_code=row['error_code'],
        identity=identity, candidate_map_object=candidate.get('map_object'), physical_id=identity.get('candidate_id'),
        poi_id=research.get('poi_id'), mode='warm' if research.get('poi_reused_fact_count') else 'cold',
        poi_reused_fact_count=research.get('poi_reused_fact_count', 0), camera=research.get('photo_camera_hints'),
        product_outcome=outcome, eligible_count=len(story_facts), eligible_proved_count=len(proved),
        eligible_facts=proved, canonical_sources=sources, gates=gates, jobs=jobs,
        provider_receipts=summarize_receipts(attempts), review_scans=scans,
        timeline=[{**event, 'payload': json.loads(event['payload_json'])} for event in events],
        usefulness_review='Model-reviewed eligible facts are shown in full for final substantive acceptance; '
            'the harness applies no text/year/address heuristics.',
        coverage=research.get('fact_acquisition_manifest'),
        shared_core_evidence={'identity_provider_receipt': identity.get('provider_receipt'),
            'reference_evidence': identity.get('reference_evidence'),
            'fact_review_proofs': [entry['review_proof'] for fact in proved
                for entry in fact['canonical_evidence']],
            'live_transport_proven': any((receipt.get('text_sends') or 0) > 0 for receipt in live_receipts),
            'live_provider_receipts': [{key: receipt.get(key) for key in ('binding', 'provider_id', 'model_id',
                'session_id', 'setup_ready', 'text_sends', 'usage', 'usage_snapshots', 'phase',
                'provider_send_state', 'error_code', 'resource_events')} for receipt in live_receipts],
            'note': 'Live transport is attested only by actual Google Live text-send events.'})
    case['status'] = acceptance_status(case)
    return case


def apply_hard_cap(service, story_id):
    from street_story.research_budget import finish_attempt
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        if research.get('automatic_research_outcome'):
            return
        elapsed = service.store.now() - row['created_at']
        matched = (research.get('visual_identity') or {}).get('status') == 'match'
        if elapsed >= CAPS['total_seconds'] or not matched and elapsed >= CAPS['identity_seconds']:
            finish_attempt(service, db, story_id, outcome='deadline_exceeded',
                reason='acceptance_upload_deadline_exceeded', purpose='facts' if matched else 'identity')


async def run(args):
    sys.path.insert(0, str(BACKEND))
    sha = require_frozen_checkout(args.expected_sha)
    items, manifest_digest = load_manifest(args.manifest)
    output = managed(args.output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected = {int(value) for value in args.messages.split(',')} if args.messages else {item['message_id'] for item in items}
    if not selected <= {item['message_id'] for item in items}:
        raise ValueError('Requested message absent from manifest')
    installer = load_installer()
    environment_digests = {}
    for config in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(config, 0o600)
        environment_digests[config.name] = hashlib.sha256(config.read_bytes()).hexdigest()
        os.environ.update(installer.parse_dotenv(config))
    os.environ['STREET_STORY_DEPLOY_SHA'] = sha
    # app.py constructs its default app on import. Redirect configuration before
    # that import so even initialization/recovery can only touch this task DB.
    os.environ.update(DATA_DIR=str(output/'data'), VIBEPUBLISH_BASE_URL='', VIBEPUBLISH_BEARER_TOKEN='',
        IDENTITY_TIMEOUT_SECONDS=str(CAPS['identity_seconds']), RESEARCH_TIMEOUT_SECONDS=str(CAPS['total_seconds']))
    from street_story.config import Settings
    settings = Settings.from_env()
    effective = {field.name: getattr(settings, field.name) for field in fields(settings)}
    # Persist only the digest of credential-bearing effective configuration.
    # Public model names/settings remain inspectable; no raw secret is retained.
    def config_value(value):
        if callable(getattr(value, 'get_secret_value', None)):
            return value.get_secret_value()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [config_value(item) for item in value]
        return value
    effective_digest = digest({key: config_value(value) for key, value in effective.items()})
    public_settings = {field.name: config_value(getattr(settings, field.name))
        for field in fields(settings) if field.repr}
    installer.require_mode(installer.RESEARCH_QUALIFICATION, 0o600)
    qualification = json.loads(installer.RESEARCH_QUALIFICATION.read_text())
    for evidence in qualification['evidence']:
        path = Path(evidence['path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Provider qualification evidence changed')
    frozen = {'policy': 'product-recovery-acceptance-v1', 'source_sha': sha,
        'manifest_sha256': manifest_digest, 'environment_sha256': environment_digests,
        'qualification_sha256': digest(qualification), 'caps_from_upload': CAPS,
        'effective_settings_sha256': effective_digest, 'public_settings': public_settings,
        'source_hashes': {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((BACKEND/'street_story').glob('*.py'))},
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    run_path = output/'run.json'
    if run_path.exists() and json.loads(run_path.read_text()) != frozen:
        raise ValueError('Source/config/qualification/manifest changed; this output cannot be resumed')
    save(run_path, frozen)
    report_path = output/'results.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {
        'run': frozen, 'cases': [blank_result(item) for item in items]}
    cases = {case['message_id']: case for case in report['cases']}
    save(report_path, report)  # Every unselected manifest photo is explicitly NOT_RUN.
    logging.basicConfig(filename=output/'workers.log', level=logging.INFO)
    if 'street_story.app' in sys.modules:
        raise ValueError('Run acceptance in a fresh CLI process with the frozen task environment')
    from street_story.app import app
    from street_story.providers import GeminiClient
    service = app.state.service
    service.providers.vibepublish = NoPublication()
    for key, value in qualification['caches'].items():
        service.store.cache_put(key, value, 3600)
    journal_path = output/'google-sdk-calls.jsonl'
    restore_sdk = instrument_google_sdk(GeminiClient, journal_path)
    try:
        async with app.router.lifespan_context(app):
            for item in items:
                case = cases[item['message_id']]
                if item['message_id'] not in selected or case.get('terminal'):
                    continue  # Canary completion is reused without any provider send.
                require_frozen_checkout(sha)
                data = Path(item['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('Source bytes changed since the frozen manifest')
                if not case.get('story_id'):
                    # Expectations/labels/known URLs never enter this call.
                    story = upload_story(service, item, data, manifest_digest)
                    case.update(story_id=story['id'], status='RUNNING')
                    save(report_path, report)
                    service.ensure_identity(story['id'])
                while True:
                    apply_hard_cap(service, case['story_id'])
                    read_case(service, case, item)
                    report['sdk_accounting'] = summarize_sdk_journal(journal_path)
                    save(report_path, report)
                    save(output/f'case-{item["message_id"]}.json', case)
                    if case['terminal']:
                        print(json.dumps({key: case.get(key) for key in ('message_id', 'status', 'physical_id',
                            'identity_elapsed_s', 'first_eligible_elapsed_s', 'total_elapsed_s',
                            'eligible_proved_count', 'product_outcome')}, ensure_ascii=False), flush=True)
                        break
                    await asyncio.sleep(.5)
                if case['status'] == 'BLOCKED':
                    break  # Preserve quota; remaining manifest entries stay NOT_RUN.
    finally:
        restore_sdk()
        report['sdk_accounting'] = summarize_sdk_journal(journal_path)
        save(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--messages', default='102', help='Comma-separated IDs; default is the one canary')
    args = parser.parse_args()
    if args.messages and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', args.messages):
        parser.error('Invalid --messages selection')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
