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


def addressed_joint_models(marker):
    """Models with possibly sent or closed requests cannot replay this question."""
    marker = marker or {}
    operations = dict(marker.get('route_operations') or {})
    if marker.get('model_id'):
        operations[marker['model_id']] = marker
    return {model for model, operation in operations.items()
        if operation.get('phase') not in {'not_sent', None}} | {
        row['model_id'] for row in marker.get('closed_route_failures') or []}


def joint_route_reassignable(marker, model_id=None):
    """Fence an UNKNOWN route, while allowing a different independently admitted model."""
    marker = marker or {}
    return ((marker.get('phase') == 'unknown' or marker.get('phase') == 'closed_failure'
        and (marker.get('status_code') in {401, 403, 404, 408, 429, 500, 502, 503, 504}
            or marker.get('code') == 'identity_native_source_map_closed_failure'))
        and not marker.get('response_sha256') and not marker.get('closed_plan')
        and bool(marker.get('model_id'))
        and (model_id is None or model_id not in addressed_joint_models(marker)))


def joint_operation_marker(service, story, *, stage, binding=None, phase=None, code=None,
        response_sha256=None, status_code=None, closed_plan=None, prepared_request=None,
        admission_retry=None, retry_not_sent=False, retry_unsent_key=False, model_id=None,
        retry_unsent_route=False, retry_closed_route=False):
    """Keep each initial route's original binding/outcome across independent failover."""
    if stage not in {'initial', 'followup'}:
        raise ValueError('invalid joint operation stage')
    from .providers import RetryableProviderError
    from .service import canonical
    if not callable(getattr(getattr(service, 'store', None), 'tx', None)) or not callable(getattr(service, '_story_row', None)):
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
        envelope = previous
        route_operations = dict(previous.get('route_operations') or {})
        route_fields = {'route_operations', 'closed_route_failures'}
        old_model = previous.get('model_id') or (previous.get('prepared_request') or {}).get('model')
        closed_technical_failure = (previous.get('phase') == 'closed_failure'
            and previous.get('status_code') in {401, 403, 404, 408, 429, 500, 502, 503, 504})
        route_retry = (stage == 'followup' and phase == 'send_intent'
            and (retry_unsent_route and previous.get('phase') == 'not_sent'
                or retry_closed_route and closed_technical_failure)
            and old_model and model_id != old_model and model_id not in route_operations
            and model_id and len(route_operations) < 2 and previous.get('binding') == binding
            and prepared_request is not None)
        if (retry_unsent_route or retry_closed_route) and not route_retry:
            raise RetryableProviderError('identity_joint_followup_route_retry_denied')
        if route_retry:
            # Only the executor changes after authoritative NotSent or a closed
            # technical failure. Preserve all envelopes; UNKNOWN is fenced.
            old_content = {k: v for k, v in previous['prepared_request'].items() if k not in {'model', 'sha256'}}
            new_content = {k: v for k, v in prepared_request.items() if k not in {'model', 'sha256'}}
            if old_content != new_content:
                raise RetryableProviderError('identity_joint_followup_binding_changed')
            route_operations[old_model] = {k: v for k, v in previous.items() if k not in route_fields}
        if stage == 'initial' and old_model:
            route_operations[previous['model_id']] = {
                key: value for key, value in previous.items() if key not in route_fields}
        switching = (stage == 'initial' and model_id is not None
                     and model_id != previous.get('model_id'))
        target = (route_operations.get(model_id) or {}) if switching else previous
        if prepared_request is not None:
            content = {key: value for key, value in prepared_request.items() if key != 'sha256'}
            if (stage != 'followup' or prepared_request.get('binding') != binding
                    or prepared_request.get('contract') != 'identity-prepared-joint-followup-v1'
                    or hashlib.sha256(canonical(content).encode()).hexdigest() != prepared_request.get('sha256')
                    or not route_retry and previous.get('prepared_request') not in (None, prepared_request)):
                raise RetryableProviderError('identity_joint_followup_binding_changed')
        retry = previous.get('admission_retry') or {}
        retry_permitted = (stage == 'followup' and retry_not_sent and previous.get('phase') == 'not_sent'
            and retry.get('retry_count') == 0 and isinstance(retry.get('retry_at'), (int, float))
            and retry['retry_at'] <= service.store.now()
            and prepared_request is not None and retry.get('prepared_request_sha256') == prepared_request['sha256'])
        key_retry_permitted = (stage == 'followup' and retry_unsent_key
            and previous.get('phase') == 'not_sent' and not retry
            and prepared_request is not None and previous.get('prepared_request') == prepared_request)
        if phase == 'send_intent' and previous and not (
                stage == 'initial' and (previous['phase'] == 'not_sent'
                    or model_id is not None and joint_route_reassignable(previous, model_id))
                or retry_permitted or key_retry_permitted or route_retry):
            raise RetryableProviderError(f'identity_joint_{stage}_outcome_unknown')
        if phase == 'send_intent' and switching and target and target.get('phase') != 'not_sent':
            raise RetryableProviderError(f'identity_joint_{stage}_outcome_unknown')
        if target and target.get('binding') != binding:
            raise RetryableProviderError(f'identity_joint_{stage}_binding_changed')
        if switching:
            previous = target
        marker = {**previous, 'scope': scope, 'binding': binding, 'phase': phase,
            'updated_at': service.store.now()}
        failures = list(envelope.get('closed_route_failures') or [])
        if phase == 'send_intent' and envelope.get('phase') == 'closed_failure' and joint_route_reassignable(envelope, model_id):
            failures.append({key: envelope.get(key) for key in ('model_id', 'status_code', 'updated_at')})
        if failures:
            marker['closed_route_failures'] = failures
        if model_id is not None:
            marker['model_id'] = model_id
        if prepared_request is not None:
            marker['prepared_request'] = prepared_request
        if stage == 'followup' and route_operations:
            marker['route_operations'] = route_operations
        if route_retry:
            marker.pop('admission_retry', None)  # Native retry stays in its original route capsule.
        if admission_retry is not None:
            if (stage != 'followup' or phase != 'not_sent' or retry
                    or admission_retry.get('retry_count') != 0
                    or admission_retry.get('prepared_request_sha256') != (marker.get('prepared_request') or {}).get('sha256')):
                raise RetryableProviderError('identity_joint_followup_retry_changed')
            marker['admission_retry'] = admission_retry
        if retry_permitted:
            marker['admission_retry'] = {**retry, 'retry_count': 1}
        if key_retry_permitted:
            marker['unsent_key_retries'] = int(previous.get('unsent_key_retries') or 0) + 1
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
        if stage == 'initial' and marker.get('model_id'):
            route_operations[marker['model_id']] = {
                key: value for key, value in marker.items() if key not in route_fields}
            marker['route_operations'] = route_operations
        research[key] = marker
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        return marker


def joint_followup_marker(service, story, **kwargs):
    """Compatibility for the existing durable joint2 contract."""
    return joint_operation_marker(service, story, stage='followup', **kwargs)


def retain_closed_initial_plan(service, story, binding, payload, schema, raw_json, prompt,
        provider_response_id=None, resolutions=None, source_text_receipt=None):
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
    if source_text_receipt:
        saved['source_text_receipt'] = source_text_receipt
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


def retain_physical_hypothesis(service, story, payload, source_map_receipt, candidates, *, reason,
        raw_json, provider_response_id=None):
    """A closed nomination survives rejection without granting any identity authority."""
    import copy
    from .identity_discovery import _conditional_text_prior
    from .identity_architectural_context import _subject_addresses, _physical_subject
    from .identity_source_selection import observed_address_context
    from .service import canonical
    if not callable(getattr(getattr(service, 'store', None), 'tx', None)):
        return None
    catalog = {item['candidate_id']: item for item in candidates if _physical_subject(item)}
    prior = _conditional_text_prior(payload, catalog)
    if not prior or not prior['candidate_ids']:
        return None
    scope = _scope(story)
    address_context = observed_address_context(story, candidates)
    receipt = {'contract': 'identity-unconfirmed-physical-hypothesis-v1', 'scope': scope,
        'state': 'unconfirmed_physical_hypothesis', 'identity_accepted': False,
        'reason': copy.deepcopy(reason), 'initial_decision': prior,
        'closed_payload': copy.deepcopy(payload), 'raw_json': raw_json,
        'raw_json_sha256': hashlib.sha256(raw_json.encode()).hexdigest(),
        'provider_response_id': provider_response_id,
        'source_map_receipt': copy.deepcopy(source_map_receipt),
        'physical_candidates': [copy.deepcopy(catalog[cid]) for cid in prior['candidate_ids']],
        'address_memberships': {cid: _subject_addresses(address_context, catalog[cid])
            for cid in prior['candidate_ids']}}
    receipt['sha256'] = hashlib.sha256(canonical(receipt).encode()).hexdigest()
    with service.store.tx() as db:
        research = _checked_research(service, story, db, scope)
        previous = research.get('identity_physical_hypothesis') or {}
        if previous.get('scope') == scope:
            return previous
        research['identity_physical_hypothesis'] = receipt
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    from .identity_telemetry import record_identity_event
    record_identity_event(service, story['id'], 'identity_physical_hypothesis_retained', {
        'generation': scope['generation'], 'control_revision': scope['control_revision'],
        'candidate_ids': prior['candidate_ids'], 'reason_code': reason.get('code'),
        'raw_json_sha256': receipt['raw_json_sha256'], 'hypothesis_sha256': receipt['sha256'],
        'identity_accepted': False})
    return receipt


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
        provider_response_id=None, errors=None, errors_truncated=False, joint_stage=None, operation_binding=None):
    """Keep an immutable initial diagnostic and a separate closed joint2 slot.

    This evidence cannot authorize a resend, subject match, or source read.
    The frozen original schema is used by the caller for original readback.
    """
    from .service import canonical
    from .identity_telemetry import record_identity_event
    if joint_stage not in {None, 'initial', 'followup'}:
        raise ValueError('invalid joint diagnostic stage')
    if not callable(getattr(getattr(service, 'store', None), 'tx', None)) or not callable(getattr(service, '_story_row', None)):
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
    if joint_stage:
        diagnostic.update(joint_stage=joint_stage, operation_binding=operation_binding)
    key = 'identity_closed_invalid_followup_plan' if joint_stage == 'followup' else 'identity_closed_invalid_plan'
    with service.store.tx() as db:
        research = _checked_research(service, story, db, scope)
        if joint_stage and operation_binding is not None:
            from .service import ConflictError
            marker = research.get('identity_joint_' + joint_stage) or {}
            if (marker.get('scope') != scope or marker.get('binding') != operation_binding
                    or marker.get('phase') != 'response_closed'
                    or marker.get('response_sha256') is not None
                        and marker['response_sha256'] != diagnostic['raw_json_sha256']):
                raise ConflictError('visual_comparison_changed', 'Исходный закрытый запрос изменился.')
        previous = research.get(key) or {}
        if previous.get('scope') == scope:
            return previous  # Immutable first response in this stage, including across restart.
        research[key] = diagnostic
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    # Only contract metadata and hashes reach logs/telemetry; raw rejected values,
    # arbitrary instance keys, validator messages, and response IDs do not.
    record_identity_event(service, story['id'], 'identity_search_plan_diagnostic_retained', {
        'generation': generation, 'control_revision': scope['control_revision'],
        'joint_stage': joint_stage,
        'code': code, 'route': route, 'raw_json_sha256': diagnostic['raw_json_sha256'],
        'raw_json_utf8_bytes': diagnostic['raw_json_utf8_bytes'], 'raw_json_truncated': diagnostic['raw_json_truncated'],
        'validation_schema_sha256': diagnostic['validation_schema_sha256'],
        'validation_errors_truncated': errors_truncated,
        'schema_errors': [{'validator': item['validator'], 'schema_path': item['schema_path']} for item in errors]})
    return diagnostic
