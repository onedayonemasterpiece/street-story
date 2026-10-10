"""Bounded photo→physical identity→reviewed POI facts acceptance on one frozen SHA.

Use a retained managed manifest/output. Expected IDs, labels and minimum counts
are report-only fields; provider input contains original photo bytes, EXIF and
explicit owner-provided approximate camera context where specified.
Run the simple canary --messages 104 first, then the complex control 102.
Reuse this output for the remaining selected cases after both pass.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import fields
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
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


def instrument_control_boundary():
    """Read-only timings around the installed authority, without payloads/URLs."""
    from ai_resource_control.client import Control
    original = Control.request
    async def observed(self, method, path, payload=None, params=None):
        started = time.monotonic()
        record = {'method': method, 'operation': path if re.fullmatch(r'rpc/[a-z0-9_]+', path) else 'registry'}
        try:
            result = await original(self, method, path, payload, params)
        except BaseException as exc:
            record.update(outcome='error', error_type=type(exc).__name__, code=getattr(exc, 'code', None))
            raise
        else:
            record.update(outcome='response', ok=result.get('ok') if isinstance(result, dict) else None)
            return result
        finally:
            record['duration_ms'] = round((time.monotonic()-started)*1000)
            logging.getLogger('street_story.control_boundary').info('resource_control_boundary %s', json.dumps(record))
    Control.request = observed
    return lambda: setattr(Control, 'request', original)


def inspect_retained_initial(path, output):
    """Offline assembled-envelope check from real frozen inputs; never infer identity."""
    import base64
    import io
    from PIL import Image
    from jsonschema import Draft202012Validator
    from street_story.identity_model_context import physical_overview_context
    from street_story.identity_architectural_evidence import literal_overview_inventory
    from street_story.identity_source_selection import compact_planner_packet, expand_planner_packet
    from street_story.native_vision import native_text_envelope
    path, output = managed(path), managed(output)
    if output.exists():
        raise ValueError('Original request-inspection evidence must not be overwritten')
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        frozen = next(json.loads(row[0])['frozen_source_map'] for row in db.execute(
            'SELECT receipt_json FROM research_provider_attempts ORDER BY created_at')
            if json.loads(row[0]).get('frozen_source_map'))
    prefix, tail = frozen['prompt'].split('Данные ниже — только контекст:\n', 1)
    encoded, end = json.JSONDecoder().raw_decode(tail)
    packet = expand_planner_packet(encoded)
    bodies = packet['map_scene']['physical_bodies']
    original_ids = [row[1] for row in bodies['rows']]
    packet['map_scene']['physical_bodies'] = physical_overview_context(bodies)
    text = packet.get('acquired_architectural_text')
    if text:
        text['articles'] = [{key: value for key, value in article.items() if key != 'text'}
                            for article in text['articles']]
        text['retrieval_receipt'] = {'article_count': len(text['articles']), 'status': 'acquired'}
        text['publisher_and_OSM_literal_records_NOT_prejoined'] = literal_overview_inventory(
            text['publisher_and_OSM_literal_records_NOT_prejoined'])
    encoded = compact_planner_packet(packet)
    if expand_planner_packet(encoded) != packet:
        raise ValueError('Planner packet roundtrip changed received references')
    prompt = prefix + 'Данные ниже — только контекст:\n' + json.dumps(encoded, ensure_ascii=False, separators=(',', ':')) + tail[end:]
    Draft202012Validator.check_schema(frozen['contract'])
    def envelope(value):
        raw = json.dumps(native_text_envelope([{'type': 'text', 'text': value},
            *[{'type': 'text', 'text': part['label']} for part in frozen['images']]], frozen['contract']),
            ensure_ascii=False, separators=(',', ':'))
        return {'text_chars': len(raw), 'text_utf8_bytes': len(raw.encode()),
                'estimated_reservation_tokens': (len(raw)+2)//3+8192+4096,
                'envelope_sha256': hashlib.sha256(raw.encode()).hexdigest()}
    images = []
    for part in frozen['images']:
        data = base64.b64decode(part['data'], validate=True)
        if hashlib.sha256(data).hexdigest() != part['sha256']:
            raise ValueError('Frozen image changed')
        with Image.open(io.BytesIO(data)) as image:
            images.append({'label': part['label'], 'bytes': len(data), 'sha256': part['sha256'],
                           'width': image.width, 'height': image.height})
    if [row[1] for row in packet['map_scene']['physical_bodies']['rows']] != original_ids:
        raise ValueError('Received physical pool changed')
    report = {'scope': 'Offline assembled Native envelope, not inference or measured provider usage',
              'before': envelope(frozen['prompt']), 'after': envelope(prompt), 'images': images,
              'body_count': len(original_ids), 'all_body_ids_retained': True, 'lossless_packet_roundtrip': True,
              'output_allowance': 8192, 'schema_valid': True, 'source_database': str(path),
              'deferred_fields': packet['map_scene']['physical_bodies']['deferred_fields'],
              'articles_full_text_retained_in_original_receipt': True}
    save(output, report)
    print(json.dumps(report), flush=True)
    return report


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
                'model': model or self.settings.gemini_model,
                'key_fingerprint': hashlib.sha256(key.encode()).hexdigest()}
        append({**base, 'phase': 'possibly_sent', 'observed_at': time.time()})
        try:
            response = await original(self, key, timeout, contents, config, model=model)
        except BaseException as exc:
            append({**base, 'phase': 'outcome_unavailable', 'observed_at': time.time(),
                    'error_type': type(exc).__name__,
                    'error_code': getattr(exc, 'code', None) if isinstance(getattr(exc, 'code', None), int) else None,
                    'usage': _usage(None)})
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


def availability_history(paths, frozen):
    """Provider history only; no source, object, fact, receipt or lease transfer."""
    def transport(settings):
        return {key: value for key, value in settings.items()
                if key.startswith(('gemini_', 'google_ai_'))}
    snapshots = []
    for path in paths:
        path = managed(path)
        prior = json.loads((path/'run.json').read_text())
        for key in ('environment_sha256', 'qualification_sha256'):
            if prior[key] != frozen[key]:
                raise ValueError('Provider availability history has different configuration/qualification')
        if transport(prior['public_settings']) != transport(frozen['public_settings']):
            raise ValueError('Provider availability history has different transport settings')
        with sqlite3.connect(f'file:{path}/data/street-story.sqlite3?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = []
            for row in db.execute('SELECT key_id,model,operation,cooldown_until,consecutive_failures,'
                                  'last_failure FROM gemini_key_health ORDER BY key_id,model,operation'):
                failure = json.loads(row['last_failure'] or '{}')
                if failure.get('code') != 429 or failure.get('category') not in {'quota_exhausted', 'rate_limited'}:
                    continue
                rows.append({key: row[key] for key in ('key_id', 'model', 'operation', 'cooldown_until',
                    'consecutive_failures')} | {'last_failure': canonical_failure(failure)})
        snapshots.append({'path': str(path), 'source_sha': prior['source_sha'],
                          'history_sha256': digest(rows), 'rows': rows})
    return snapshots


def runtime_qualification_caches(qualification, installer):
    """Derive runtime hints without changing immutable qualification provenance."""
    caches = copy.deepcopy(qualification['caches'])
    installer.validate_fact_semantic_pool(caches, qualification['evidence'],
        caches.get('research-text-verification-v1') or {})
    return caches


def canonical_failure(failure):
    return json.dumps({'category': failure['category'], 'code': 429}, separators=(',', ':'))


def apply_availability_history(store, snapshots):
    with store.tx() as db:
        for snapshot in snapshots:
            for row in snapshot['rows']:
                # Preserve actual absolute expiry and negative observation count.
                # Expired cooldowns stay expired; current configured keys/models
                # alone are eligible. Busy slots and reservations are untouched.
                db.execute('UPDATE gemini_key_health SET cooldown_until=max(cooldown_until,?),'
                    'last_failure=CASE WHEN consecutive_failures<=? THEN ? ELSE last_failure END,'
                    'quota_state=CASE WHEN consecutive_failures<=? THEN ? ELSE quota_state END,'
                    'consecutive_failures=max(consecutive_failures,?) '
                    'WHERE key_id=? AND model=? AND operation=?',
                    (row['cooldown_until'], row['consecutive_failures'], row['last_failure'],
                     row['consecutive_failures'], json.loads(row['last_failure'])['category'],
                     row['consecutive_failures'], row['key_id'], row['model'], row['operation']))


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


def selected_items(items, messages):
    """Keep the requested simple→complex order, independent of manifest order."""
    requested = [int(value.strip()) for value in messages.split(',')] if messages else [
        item['message_id'] for item in items]
    by_id = {item['message_id']: item for item in items}
    if len(requested) != len(set(requested)) or not set(requested) <= by_id.keys():
        raise ValueError('Requested messages must be distinct and present in the manifest')
    return [by_id[identifier] for identifier in requested]


def blank_result(item):
    return {'message_id': item['message_id'], 'report_label': item.get('report_label'),
        'input_sha256': item['sha256'], 'min_useful_facts': item['min_useful_facts'],
        'expected_physical_id': item.get('expected_physical_id'), 'status': 'NOT_RUN',
        'expected_physical_ids': item.get('expected_physical_ids'),
        'operator_labels_supplied': False}


def upload_story(service, item, data, manifest_digest):
    """The ordinary upload contract has no acceptance labels/expected IDs."""
    camera = item.get('owner_approx_camera') or {}
    return service.create_story(key=f'acceptance:{manifest_digest}:{item["message_id"]}',
        client_story_id=f'acceptance:{manifest_digest}:{item["message_id"]}',
        photo_sha256=item['sha256'], photo_mime_type=item['mime'], photo_bytes=data,
        voice_protocol='voice-chunks-v2', lat=camera.get('latitude'), lon=camera.get('longitude'),
        **({'location_provenance': {'kind': 'owner_approx_camera'}} if camera else {}))


def block_directed_reference_inference(service, items):
    """Test seam: directed spatial cases cannot use an external REF as evidence.

    Truth IDs/labels are never supplied. An attempted reference operation fails
    before image/pair dispatch; ordinary SOURCE+neutral MAP planning is intact.
    """
    from street_story.errors import PermanentProviderError
    from street_story.live_visual_comparison import LiveVisualComparisonMixin
    restricted = {item['sha256'] for item in items
        if item.get('acceptance_mode') == 'geometry_without_reference'}
    original_reply = LiveVisualComparisonMixin._visual_reply
    original_photo, original_batch = service._identify_photo, service._identify_photo_batch
    async def photo(story, *args, **kwargs):
        if story.get('photo_sha256') in restricted:
            raise PermanentProviderError('directed_acceptance_external_reference_disabled')
        return await original_photo(story, *args, **kwargs)
    async def batch(story, *args, **kwargs):
        if story.get('photo_sha256') in restricted:
            raise PermanentProviderError('directed_acceptance_external_reference_disabled')
        return await original_batch(story, *args, **kwargs)
    def reply(comparison_id, candidates, identity, remaining):
        if identity.get('photo_sha256') in restricted:
            raise PermanentProviderError('directed_acceptance_external_reference_disabled')
        return original_reply(comparison_id, candidates, identity, remaining)
    service._identify_photo, service._identify_photo_batch = photo, batch
    LiveVisualComparisonMixin._visual_reply = staticmethod(reply)
    def restore():
        service._identify_photo, service._identify_photo_batch = original_photo, original_batch
        LiveVisualComparisonMixin._visual_reply = staticmethod(original_reply)
    return restore


def acceptance_status(case):
    if case.get('operator_stopped'):
        return 'OPERATOR_STOPPED'
    outcome = (case.get('product_outcome') or {}).get('outcome')
    if outcome == 'resource_blocked':
        return 'BLOCKED'
    if not case.get('terminal'):
        return 'RUNNING'
    conditional = case.get('conditional_identity_context') or {}
    if outcome == 'clarification_required' and conditional.get('clarification'):
        return 'CLARIFICATION_REQUIRED'  # This checks dialogue, never automatic identity PASS.
    if conditional.get('joint_visual_input_verified') is True and conditional.get('article_sources') and any(item.get('support_status') == 'spatially_supported'
            for item in conditional.get('hypotheses') or []):
        return 'CONDITIONAL_CONTEXT_AVAILABLE'  # Readable context is not proof of delivered facts.
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
    from street_story.identity_proof import verified_physical_identity
    from street_story.poi_memory import memory_keys, _review_snapshot
    from street_story.live_identity_context import identity_research_context
    from street_story.research_control import research_stopped
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
        # Read the same product projection without hydrating memory or changing
        # the state being measured. Ordinary API mutation/restart is tested separately.
        visible = service._story_repr(db, row)
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
    # Availability is distinct from the original terminal/coverage metric.
    # Ordinary product projection exposes current verified claims immediately;
    # an independent pending review must not hide selection/editorial tools.
    visible_eligible = [fact for fact in visible.get('facts') or [] if fact.get('eligibility') == 'eligible']
    useful_available = (identity.get('status') == 'match'
        and visible.get('state') in {'facts_ready', 'review', 'visual_ready', 'scheduling', 'scheduled', 'published'}
        and len(proved) >= item['min_useful_facts']
        and len(visible_eligible) >= item['min_useful_facts'])
    if useful_available:
        case.setdefault('useful_product_elapsed_s', max(0, now - started))
    outcome = research.get('automatic_research_outcome')
    purpose = 'facts' if identity.get('status') == 'match' else 'identity'
    stopped = not outcome and research_stopped(research, purpose,
        photo_sha256=row['photo_sha256'], identity_generation=int(research.get('identity_generation') or 0))
    terminal = bool(outcome) or (bool(jobs) and not any(job['state'] in ACTIVE
        for job in jobs if job['kind'] in {'identity', 'identity_visual', 'research', 'refinement'}))
    # A match between stages is not terminal: the normal scheduler still needs
    # an opportunity to open its fact job. facts_ready/terminal outcome is final.
    if identity.get('status') == 'match' and not outcome and not stopped and row['state'] not in {'facts_ready', 'needs_review'}:
        terminal = False
    if stopped:
        terminal = True
    finish = float(outcome.get('finished_at') or now) if outcome else now
    if stopped:
        finish = float(((research.get('research_controls') or {}).get(purpose) or {}).get('stopped_at') or now)
    total = max(0, finish - started) if terminal else max(0, now - started)
    candidate = next((candidate for candidate in identity.get('candidates') or []
        if candidate.get('candidate_id') == identity.get('candidate_id')), {})
    expected = item.get('expected_physical_ids') or [item.get('expected_physical_id')]
    expected = [value for value in expected if value]
    gates = {'identity_in_180s': case.get('identity_elapsed_s', float('inf')) <= CAPS['identity_seconds'],
        'first_eligible_in_300s': case.get('first_eligible_elapsed_s', float('inf')) <= CAPS['first_eligible_seconds'],
        'terminal_in_480s': terminal and total <= CAPS['total_seconds'],
        'eligible_minimum': len(proved) >= item['min_useful_facts'],
        'correct_physical_object': identity.get('candidate_id') in expected if expected else None,
        'identity_proof': verified_physical_identity(identity, photo_sha256=row['photo_sha256'],
            generation=int(research.get('identity_generation') or 0)),
        'canonical_poi_readback': len(proved) >= item['min_useful_facts'],
        'natural_product_terminal': not stopped and (outcome or {}).get('reason') != 'acceptance_upload_deadline_exceeded'}
    if item.get('acceptance_mode') == 'geometry_without_reference':
        gates['geometry_without_reference'] = (identity.get('proof_kind') == 'geometry'
            and identity.get('visual_reference_verified') is False
            and not identity.get('reference_evidence'))
    live_receipts = [json.loads(attempt['receipt_json'] or '{}') for attempt in attempts]
    live_receipts = [receipt for receipt in live_receipts if receipt.get('provider_id') == 'google-live']
    case.update(status='RUNNING', operator_stopped=bool(stopped), uploaded_at=started, elapsed_from_upload_s=max(0, now-started),
        conditional_identity_context=identity_research_context(service, row, research),
        total_elapsed_s=total, terminal=terminal, state=row['state'], error_code=row['error_code'],
        useful_product_available=useful_available,
        useful_product_in_480s=case.get('useful_product_elapsed_s', float('inf')) <= CAPS['total_seconds'],
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
            'proof_kind': identity.get('proof_kind'), 'geometry_proof': identity.get('geometry_proof'),
            'visual_reference_verified': identity.get('visual_reference_verified') is True,
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


def stop_wrong_physical_case(service, case, item):
    """Stop acceptance spending after a report-only object expectation fails.

    This sends ordinary Stop with the current SOURCE/generation fence, never a
    correct address, candidate ID or repair hint to the runtime/model.
    """
    if (case.get('operator_stopped') or (case.get('identity') or {}).get('status') != 'match'
            or (case.get('gates') or {}).get('correct_physical_object') is not False):
        return False
    expected = item.get('expected_physical_ids') or [item.get('expected_physical_id')]
    expected = [identifier for identifier in expected if identifier]
    if not expected:
        return False
    with service.store.tx() as db:
        row = service._story_row(db, case['story_id'])
        research = json.loads(row['research_json'] or '{}')
        identity = research.get('visual_identity') or {}
        if (row['photo_sha256'] != item['sha256'] or identity.get('status') != 'match'
                or identity.get('candidate_id') in expected):
            return False
        body = {'action': 'stop', 'purpose': 'facts', 'expected_photo_sha256': row['photo_sha256'],
            'expected_identity_generation': int(research.get('identity_generation') or 0),
            'expected_control_revision': int(research.get('research_control_revision') or 0)}
    from street_story.service import ConflictError
    try:
        service.mutate_research_control(case['story_id'],
            'acceptance-wrong-object:' + digest({'story_id': case['story_id'], **body}), body)
    except ConflictError:
        return False  # A newer SOURCE/control must not be stopped by this readback.
    case['acceptance_stop_reason'] = 'wrong_physical_object'
    return True


def apply_hard_cap(service, story_id):
    from street_story.research_budget import finish_attempt
    from street_story.research_control import research_stopped
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        if research.get('automatic_research_outcome'):
            return
        elapsed = service.store.now() - row['created_at']
        matched = (research.get('visual_identity') or {}).get('status') == 'match'
        if research_stopped(research, 'facts' if matched else 'identity',
                photo_sha256=row['photo_sha256'], identity_generation=int(research.get('identity_generation') or 0)):
            return  # Operator Stop is reported separately, never rewritten as natural completion.
        # Allow the application deadline watcher one scheduling tick; if the
        # harness must intervene, natural_product_terminal explicitly fails.
        if elapsed >= CAPS['total_seconds']+1 or not matched and elapsed >= CAPS['identity_seconds']+1:
            finish_attempt(service, db, story_id, outcome='deadline_exceeded',
                reason='acceptance_upload_deadline_exceeded', purpose='facts' if matched else 'identity')


async def run(args):
    sys.path.insert(0, str(BACKEND))
    sha = require_frozen_checkout(args.expected_sha)
    items, manifest_digest = load_manifest(args.manifest)
    output = managed(args.output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected = selected_items(items, args.messages)
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
    runtime_caches = runtime_qualification_caches(qualification, installer)
    frozen = {'policy': 'product-recovery-acceptance-v1', 'source_sha': sha,
        'manifest_sha256': manifest_digest, 'environment_sha256': environment_digests,
        'qualification_sha256': digest(qualification), 'caps_from_upload': CAPS,
        'runtime_qualification_caches_sha256': digest(runtime_caches),
        'effective_settings_sha256': effective_digest, 'public_settings': public_settings,
        'source_hashes': {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((BACKEND/'street_story').glob('*.py'))},
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    frozen['availability_history'] = availability_history(args.availability_from, frozen)
    run_path = output/'run.json'
    fresh_run = not run_path.exists()
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
    if fresh_run:
        apply_availability_history(service.store, frozen['availability_history'])
    service.providers.vibepublish = NoPublication()
    for key, value in runtime_caches.items():
        service.store.cache_put(key, value, 3600)
    journal_path = output/'google-sdk-calls.jsonl'
    restore_sdk = instrument_google_sdk(GeminiClient, journal_path)
    restore_control = instrument_control_boundary()
    restore_references = block_directed_reference_inference(service, selected)
    try:
        async with app.router.lifespan_context(app):
            for item in selected:
                case = cases[item['message_id']]
                if case.get('terminal'):
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
                last_saved, last_signature = 0, None
                while True:
                    apply_hard_cap(service, case['story_id'])
                    read_case(service, case, item)
                    if stop_wrong_physical_case(service, case, item):
                        read_case(service, case, item)
                    report['sdk_accounting'] = summarize_sdk_journal(journal_path)
                    # Provider checkpoints and upload IDs remain durable in
                    # SQLite. Do not encode/write every full discovery reserve
                    # twice per second while its state is unchanged.
                    signature = (case['status'], case.get('physical_id'), case.get('state'),
                        case.get('eligible_proved_count'), case.get('error_code'))
                    now = time.monotonic()
                    if case['terminal'] or signature != last_signature or now - last_saved >= 5:
                        save(report_path, report)
                        save(output/f'case-{item["message_id"]}.json', case)
                        last_saved, last_signature = now, signature
                    if case['terminal']:
                        print(json.dumps({key: case.get(key) for key in ('message_id', 'status', 'physical_id',
                            'identity_elapsed_s', 'first_eligible_elapsed_s', 'total_elapsed_s',
                            'eligible_proved_count', 'product_outcome')}, ensure_ascii=False), flush=True)
                        break
                    await asyncio.sleep(.5)
                # Each selected SOURCE is independent. Preserve this exact
                # failure/UNKNOWN and continue other requested cases.
    finally:
        restore_references()
        restore_sdk()
        restore_control()
        report['sdk_accounting'] = summarize_sdk_journal(journal_path)
        save(report_path, report)
    return report


async def retained_review(args):
    """One explicitly scoped normal backend review, without photo/identity replay.

    This is a segment measurement, never a replacement cold corpus outcome.
    Original run reports and provider receipts are retained unchanged.
    """
    sha = require_frozen_checkout(args.expected_sha)
    source_run, output = managed(args.retained_review_run), managed(args.output)
    if not (source_run/'data'/'street-story.sqlite3').is_file() or output.exists():
        raise ValueError('Use an existing retained database and a fresh segment report')
    fact_ids = args.fact_ids.split(',')
    if not args.story_id or not 1 <= len(fact_ids) <= 12 or any(not fid for fid in fact_ids):
        raise ValueError('Explicit review requires this story and 1–12 own fact IDs')
    installer = load_installer()
    for config in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(config, 0o600)
        os.environ.update(installer.parse_dotenv(config))
    os.environ.update(DATA_DIR=str(source_run/'data'), VIBEPUBLISH_BASE_URL='', VIBEPUBLISH_BEARER_TOKEN='',
                      STREET_STORY_DEPLOY_SHA=sha)
    logging.basicConfig(filename=output.with_suffix('.log'), level=logging.INFO)
    from street_story.app import app
    from street_story.headless_facts import HeadlessFacts
    from street_story.headless_fact_review import HeadlessFactReview
    from street_story.poi_memory import memory_keys
    from street_story.research_runs import begin_research_run
    service = app.state.service
    service.providers.vibepublish = NoPublication()
    qualification = json.loads(installer.RESEARCH_QUALIFICATION.read_text())
    installer.require_mode(installer.RESEARCH_QUALIFICATION, 0o600)
    for evidence in qualification['evidence']:
        path = Path(evidence['path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Provider qualification evidence changed')
    for key, value in runtime_qualification_caches(qualification, installer).items():
        service.store.cache_put(key, value, 3600)
    sid = args.story_id
    with service.store.connection() as db:
        before = dict(service._story_row(db, sid))
        research = json.loads(before['research_json'])
        active = db.execute("SELECT 1 FROM jobs WHERE story_id=? AND state IN ('running','ready','retry')", (sid,)).fetchone()
        if active:
            raise ValueError('Finish the existing operation before explicit scoped review')
        vision_before = [list(row) for row in db.execute("SELECT attempt_id,receipt_json FROM research_provider_attempts "
            "WHERE story_id=? AND role LIKE 'vision%' ORDER BY attempt_id", (sid,))]
        poi_key = memory_keys(db, research['visual_identity'])[0]
    key = 'retained-review:' + digest([sha, sid, fact_ids, str(output)])
    goal = 'Проверь эти утверждения по всем их собственным источникам, сохрани субъект, даты, модальность и противоречия.'
    service._research_request(sid, key, {'coverage_goal': goal, 'extraction_scope': key})
    job = service._claim(claim_kind='research', claim_story_id=sid)
    if not job:
        raise ValueError('The scoped ordinary research request was not claimed')
    with service.store.tx() as db:
        story = service._story_row(db, sid)
        run_id = begin_research_run(db, story_id=sid, poi_key=poi_key, goal=goal, scope=key,
            expected_story_revision=story['revision'], identity_generation=int(research.get('identity_generation') or 0),
            run_id=None, now=service.store.now())
    harness = HeadlessFacts(service)
    revision = int(((research.get('research_controls') or {}).get('facts') or {}).get('revision') or 0)
    report = {'status': 'RUNNING', 'mode': 'retained_backend_review_segment', 'source_sha': sha,
              'story_id': sid, 'source_run': str(source_run), 'fact_ids': fact_ids, 'job_id': job['id'],
              'run_id': run_id, 'publication_dispatches': 0, 'cold_acceptance': False}
    save(output, report)
    started = time.monotonic()
    restore = instrument_control_boundary()
    try:
        async with asyncio.timeout(75):
            count = await HeadlessFactReview(harness).run(job, run_id, revision, fact_ids=fact_ids)
        if count:
            report['product_outcome'] = harness._finish(job, run_id, revision, 'explicit_review_packet_completed')
        report.update(status='REVIEW_REQUIRED' if count else 'NO_CLOSED_REVIEW', committed_packets=count,
            facts=harness.adapter._get_facts(sid, {'eligibility': 'all', 'limit': 50})['facts'])
        with service.store.tx() as db:
            db.execute("UPDATE jobs SET state=?,lease_until=0,available_at=?,updated_at=? WHERE id=? AND attempts=? AND state='running'",
                ('done' if count else 'retry', service.store.now()+300, service.store.now(), job['id'], job['attempts']))
    except BaseException as exc:
        report.update(status='FAILED_SEGMENT', error_type=type(exc).__name__, error_code=getattr(exc, 'code', None))
        raise
    finally:
        restore()
        report['elapsed_s'] = time.monotonic()-started
        with service.store.connection() as db:
            after = dict(service._story_row(db, sid))
            vision_after = [list(row) for row in db.execute("SELECT attempt_id,receipt_json FROM research_provider_attempts "
                "WHERE story_id=? AND role LIKE 'vision%' ORDER BY attempt_id", (sid,))]
            report['reviews'] = [json.loads(row[0]) for row in db.execute('SELECT value_json FROM research_checkpoints '
                "WHERE job_id=? AND stage LIKE 'headless_fact_review:%'", (job['id'],))]
        report['source_identity_unchanged'] = (before['photo_sha256'] == after['photo_sha256']
            and research['visual_identity'] == json.loads(after['research_json'])['visual_identity'])
        report['original_vision_receipts_unchanged'] = vision_before == vision_after
        save(output, report)
        await service.close()
    print(json.dumps({key: report.get(key) for key in ('status', 'mode', 'elapsed_s',
        'committed_packets', 'source_identity_unchanged', 'original_vision_receipts_unchanged')}), flush=True)
    return report


async def retained_completion(args):
    """Replay saved eligible model bases in RAM, leaving the retained DB intact.

    This checks completion/resume orchestration, not a new cold or client result.
    Only the closed job/run execution state is reopened in the disposable copy.
    No provider is installed and the original identity/evidence remain unchanged.
    """
    from types import SimpleNamespace
    from street_story.db import Store, ScopedConnection
    from street_story.service import StreetStoryService
    from street_story.headless_facts import HeadlessFacts
    path, output = managed(args.retained_completion_db), managed(args.output)
    if output.exists() or not args.story_id:
        raise ValueError('Use explicit own story IDs and a fresh segment report')
    uri = 'file:retained-completion-' + uuid.uuid4().hex + '?mode=memory&cache=shared'
    keeper = sqlite3.connect(uri, uri=True)
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as original:
        original.backup(keeper)
    class MemoryStore(Store):
        def connection(self):
            db = sqlite3.connect(uri, uri=True, isolation_level=None, factory=ScopedConnection)
            db.row_factory = sqlite3.Row
            return db
    service = StreetStoryService.__new__(StreetStoryService)
    service.store, service.providers = MemoryStore.__new__(MemoryStore), SimpleNamespace(research=None)
    harness = HeadlessFacts(service)
    report = {'mode': 'retained_completion_in_memory_replay', 'cold_acceptance': False,
        'source_database': str(path), 'provider_dispatches': 0,
        'synthetic_execution_preconditions': ['same closed job marked running', 'same run marked partial',
            'old terminal checkpoint removed in RAM'], 'cases': []}
    try:
        for sid in args.story_id.split(','):
            with service.store.tx() as db:
                job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='research' "
                    'ORDER BY created_at DESC LIMIT 1', (sid,)).fetchone())
                run = dict(db.execute('SELECT * FROM research_runs WHERE story_id=? ORDER BY created_at DESC LIMIT 1',
                    (sid,)).fetchone())
                vision_before = [tuple(r) for r in db.execute('SELECT attempt_id,receipt_json FROM research_provider_attempts WHERE story_id=?', (sid,))]
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (job['id'],))
                db.execute("UPDATE research_runs SET state='partial' WHERE run_id=?", (run['run_id'],))
                db.execute('DELETE FROM research_checkpoints WHERE job_id=? AND stage=?',
                    (job['id'], 'headless_fact_outcome:' + run['run_id']))
                job['state'] = 'running'
            started = time.monotonic()
            outcome = await harness.run(job, run['run_id'], run['goal'], run['extraction_scope'])
            with service.store.connection() as db:
                vision_after = [tuple(r) for r in db.execute('SELECT attempt_id,receipt_json FROM research_provider_attempts WHERE story_id=?', (sid,))]
            report['cases'].append({'story_id': sid, 'outcome': outcome, 'elapsed_s': time.monotonic()-started,
                'provider_receipts_unchanged': vision_before == vision_after})
        report['status'] = 'PASS' if all(c['provider_receipts_unchanged'] and
            (c['outcome'] or {}).get('reason') == 'model_goal_sufficient' for c in report['cases']) else 'FAIL'
        save(output, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        keeper.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-sha')
    parser.add_argument('--inspect-initial-db', type=Path, help='Offline assembled-input check; no model/network operations')
    parser.add_argument('--retained-review-run', type=Path, help='Explicit backend review segment over an existing retained DB; no identity replay')
    parser.add_argument('--retained-completion-db', type=Path, help='Offline in-memory completion replay; no provider or product acceptance')
    parser.add_argument('--story-id')
    parser.add_argument('--fact-ids', default='')
    parser.add_argument('--availability-from', type=Path, action='append', default=[],
                        help='Prior frozen run: import only same-config observed provider429 history')
    parser.add_argument('--messages', default='104', help='Ordered IDs; default is the simple canary')
    args = parser.parse_args()
    if args.inspect_initial_db:
        inspect_retained_initial(args.inspect_initial_db, args.output)
        return
    if args.retained_completion_db:
        asyncio.run(retained_completion(args))
        return
    if args.retained_review_run:
        if not args.expected_sha:
            parser.error('Retained review requires --expected-sha')
        asyncio.run(retained_review(args))
        return
    if not args.manifest or not args.expected_sha:
        parser.error('Actual acceptance requires --manifest and --expected-sha')
    if args.messages and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', args.messages):
        parser.error('Invalid --messages selection')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
