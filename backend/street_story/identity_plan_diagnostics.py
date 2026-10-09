"""Bounded, immutable evidence for a closed rejected identity plan."""
from __future__ import annotations

import hashlib
import json
from itertools import islice

from jsonschema import Draft202012Validator

RAW_JSON_BYTES = 32768
RAW_PREFIX_BYTES = 8192
VALIDATION_ERRORS = 16


def _scope(story):
    captured = json.loads(story.get('research_json') or '{}')
    generation = int(story.get('_identity_generation', captured.get('identity_generation') or 0))
    return {'photo_sha256': story['photo_sha256'], 'generation': generation,
        'control_revision': _revision(captured, story['photo_sha256'], generation)}


def _revision(research, photo, generation):
    control = (research.get('research_controls') or {}).get('identity') or {}
    return int(control.get('revision') or 0) if (control.get('photo_sha256') == photo
        and control.get('identity_generation') == generation) else 0


def _checked_research(service, story, db, scope):
    from .research_control import research_stopped
    from .service import ConflictError
    row = service._story_row(db, story['id'])
    research = json.loads(row['research_json'] or '{}')
    if (row['photo_sha256'] != scope['photo_sha256']
            or int(research.get('identity_generation') or 0) != scope['generation']
            or _revision(research, row['photo_sha256'], scope['generation']) != scope['control_revision']
            or research_stopped(research, 'identity', photo_sha256=row['photo_sha256'],
                identity_generation=scope['generation'])):
        raise ConflictError('visual_comparison_changed', 'Фото или управление исследованием изменилось.')
    return research


def joint_operation_marker(service, story, *, stage, binding=None, phase=None, code=None,
        response_sha256=None, status_code=None, closed_plan=None):
    """One scoped SOURCE+MAP operation; only authoritative not_sent permits reassignment."""
    if stage not in {'initial', 'followup'}:
        raise ValueError('invalid joint operation stage')
    from .providers import RetryableProviderError
    from .service import canonical
    if not callable(getattr(service.store, 'tx', None)) or not callable(getattr(service, '_story_row', None)):
        return None
    scope = _scope(story)
    with service.store.tx() as db:
        research = _checked_research(service, story, db, scope)
        key = 'identity_joint_initial' if stage == 'initial' else 'identity_joint_followup'
        previous = research.get(key) or {}
        if previous.get('scope') != scope:
            previous = {}
        if phase is None:
            return previous or None
        if phase == 'send_intent' and previous and not (stage == 'initial' and previous['phase'] == 'not_sent'):
            raise RetryableProviderError(f'identity_joint_{stage}_outcome_unknown')
        if previous and previous.get('binding') != binding:
            raise RetryableProviderError(f'identity_joint_{stage}_binding_changed')
        marker = {**previous, 'scope': scope, 'binding': binding, 'phase': phase,
            'updated_at': service.store.now()}
        if phase != previous.get('phase') and phase in {'send_intent', 'response_closed'}:
            for field in ('code', 'status_code', 'response_sha256'):
                marker.pop(field, None)
        if code is not None:
            marker['code'] = code
        if response_sha256 is not None:
            marker['response_sha256'] = response_sha256
        if status_code is not None:
            marker['status_code'] = status_code
        if closed_plan is not None:
            if stage != 'initial' or phase != 'response_closed':
                raise ValueError('closed plan requires a closed initial operation')
            marker.setdefault('closed_plan', closed_plan)
        research[key] = marker
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        return marker


def joint_followup_marker(service, story, **kwargs):
    """Compatibility for the existing durable joint2 contract."""
    return joint_operation_marker(service, story, stage='followup', **kwargs)


def retain_closed_initial_plan(service, story, binding, payload, schema, raw_json, prompt,
        provider_response_id=None, resolutions=None):
    """Retain a host-validated original answer before an optional TEXT send.

    Full JSON is required for reuse; oversized responses are never reconstructed
    from a prefix. The current exact input/configuration and schema remain fenced.
    """
    from .service import canonical
    if (not isinstance(payload, dict) or not isinstance(raw_json, str)
            or len(raw_json.encode()) > RAW_JSON_BYTES
            or len(canonical(payload).encode()) > RAW_JSON_BYTES
            or len(canonical(schema).encode()) > 262144
            or len(prompt.encode()) > 262144):
        return None
    saved = {'contract': 'identity-closed-initial-plan-v1', 'binding': binding,
        'payload': payload, 'schema': schema, 'raw_json': raw_json, 'prompt': prompt,
        'provider_response_id': provider_response_id[:160] if isinstance(provider_response_id, str) else None,
        'identity_response_id_resolutions': resolutions or []}
    saved['sha256'] = hashlib.sha256(canonical(saved).encode()).hexdigest()
    return joint_operation_marker(service, story, stage='initial', binding=binding,
        phase='response_closed', closed_plan=saved)


def reusable_closed_initial_plan(marker, binding):
    """Verify the immutable receipt before the caller revalidates its contract."""
    from .service import canonical
    saved = (marker or {}).get('closed_plan') or {}
    if (not saved or marker.get('phase') != 'response_closed'
            or marker.get('binding') != binding or saved.get('binding') != binding
            or saved.get('contract') != 'identity-closed-initial-plan-v1'):
        return None
    content = {key: value for key, value in saved.items() if key != 'sha256'}
    if (hashlib.sha256(canonical(content).encode()).hexdigest() != saved.get('sha256')
            or hashlib.sha256(saved.get('raw_json', '').encode()).hexdigest() != marker.get('response_sha256')
            or hashlib.sha256(canonical(saved.get('schema')).encode()).hexdigest() != binding['schema_sha256']):
        return None
    return saved


def provider_outcome(error):
    """Classify transport evidence, never infer dispatch from error prose."""
    from google.genai.errors import APIError
    receipt = getattr(error, 'receipt', None)
    state = (getattr(error, 'provider_send_state', None)
        or (receipt.get('provider_send_state') if isinstance(receipt, dict) else None))
    if state == 'not_sent':
        return 'not_sent', None
    status = getattr(error, 'code', None) if isinstance(error, APIError) else None
    response = getattr(error, 'response', None)
    if status is None and response is not None:
        status = getattr(response, 'status_code', None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    if state == 'response_closed' or status is not None and 400 <= status < 600:
        return 'closed_failure', status
    return 'unknown', None


def validation_details(schema, payload):
    # Same bounded, value-free formatter as Live closed-invalid diagnostics;
    # keep this pure helper independent of the interactive provider transport.
    errors = list(islice(Draft202012Validator(schema).iter_errors(payload), VALIDATION_ERRORS + 1))
    return [_validation_error(error) for error in errors[:VALIDATION_ERRORS]], len(errors) > VALIDATION_ERRORS


def _validation_error(error):
    paths = list(error.absolute_path), list(error.absolute_schema_path)
    result = {'validator': error.validator,
        'instance_path': [part if isinstance(part, int) else str(part)[:80] for part in paths[0][:12]],
        'schema_path': [part if isinstance(part, int) else str(part)[:80] for part in paths[1][:12]],
        'paths_truncated': any(len(path) > 12 or any(isinstance(part, str) and len(part) > 80 for part in path)
            for path in paths)}
    if error.validator == 'required' and isinstance(error.instance, dict):
        result['missing_properties'] = [str(key)[:80] for key in error.validator_value if key not in error.instance][:16]
    return result


def _raw_json(payload, raw_json, raw_json_available):
    from .service import canonical
    supplied = isinstance(raw_json, str)
    encoded = (raw_json if supplied else canonical(payload)).encode('utf-8')
    truncated = len(encoded) > RAW_JSON_BYTES
    result = {'raw_json_sha256': hashlib.sha256(encoded).hexdigest(),
        'raw_json_utf8_bytes': len(encoded), 'raw_json_truncated': truncated,
        'raw_json_origin': 'provider_text' if supplied else 'decoded_result',
        'raw_json_available': supplied and raw_json_available is not False}
    if truncated:
        result['raw_json_prefix'] = encoded[:RAW_PREFIX_BYTES].decode('utf-8', errors='ignore')
    else:
        result['raw_json'] = encoded.decode('utf-8')
    return result


def retain_closed_invalid(service, story, payload, schema, *, code, route,
        original_schema_readback=False, original_schema=False, raw_json=None, raw_json_available=None,
        provider_response_id=None, errors=None, errors_truncated=False):
    """Commit the first invalid answer for this exact photo/generation/revision.

    This evidence cannot authorize a resend, subject match, or source read.
    The frozen original schema is used by the caller for original readback.
    """
    from .service import canonical
    from .identity_telemetry import record_identity_event
    if not callable(getattr(service.store, 'tx', None)) or not callable(getattr(service, '_story_row', None)):
        return None  # Preserve legitimate minimal provider adapter compatibility.
    scope = _scope(story)
    generation = scope['generation']
    if errors is None:
        errors, errors_truncated = validation_details(schema, payload)
    response_id = provider_response_id if isinstance(provider_response_id, str) else None
    diagnostic = {'contract': 'identity-closed-invalid-plan-v1', 'scope': scope,
        'phase': 'closed_invalid', 'provider_send_state': 'response_closed',
        'code': code, 'route': route, 'original_schema_readback': original_schema_readback,
        'validation_schema_origin': ('original_frozen' if original_schema else
            'legacy_readback' if original_schema_readback else 'current_joint'),
        'validation_schema_sha256': hashlib.sha256(canonical(schema).encode()).hexdigest(),
        'validation_errors': errors, 'validation_errors_truncated': errors_truncated,
        'provider_response_id': response_id[:160] if response_id else None,
        'provider_response_id_truncated': bool(response_id and len(response_id) > 160),
        'captured_at': service.store.now(), **_raw_json(payload, raw_json, raw_json_available)}
    with service.store.tx() as db:
        research = _checked_research(service, story, db, scope)
        previous = research.get('identity_closed_invalid_plan') or {}
        if previous.get('scope') == scope:
            return previous  # Immutable first response, including across fallback/restart.
        research['identity_closed_invalid_plan'] = diagnostic
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    # Only contract metadata and hashes reach logs/telemetry; raw rejected values,
    # arbitrary instance keys, validator messages, and response IDs do not.
    record_identity_event(service, story['id'], 'identity_search_plan_diagnostic_retained', {
        'generation': generation, 'control_revision': scope['control_revision'],
        'code': code, 'route': route, 'raw_json_sha256': diagnostic['raw_json_sha256'],
        'raw_json_utf8_bytes': diagnostic['raw_json_utf8_bytes'], 'raw_json_truncated': diagnostic['raw_json_truncated'],
        'validation_schema_sha256': diagnostic['validation_schema_sha256'],
        'validation_errors_truncated': errors_truncated,
        'schema_errors': [{'validator': item['validator'], 'schema_path': item['schema_path']} for item in errors]})
    return diagnostic
