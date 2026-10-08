"""Bounded GET-only observation after the automatic product attempt has ended.

Late provider responses remain technical evidence. This module never starts a
request, releases a reservation, projects facts/identity, or revives a job.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid

from jsonschema import Draft202012Validator

LOG = logging.getLogger(__name__)
WINDOW_SECONDS = 60
POLL_SECONDS = 20
MAX_READS = 3
PENDING = {'prompt_intent', 'submitted', 'unknown', 'abort_intent', 'abort_outcome_unknown'}
MAX_RESPONSE_CHARS = 200000


def _unknown(receipt):
    return (receipt.get('phase') in PENDING
        or receipt.get('phase') == 'aborted' and not receipt.get('abort_acknowledged')
        or receipt.get('provider_send_state') == 'possibly_sent'
        and receipt.get('phase') not in {'completed', 'response_completed'})


def _scope(service, db, story_id, receipt):
    row = service._story_row(db, story_id)
    research = json.loads(row['research_json'] or '{}')
    outcome = research.get('automatic_research_outcome') or {}
    binding = receipt.get('binding') or {}
    generation = int(research.get('identity_generation') or 0)
    purpose = binding.get('purpose', 'identity')
    control = (research.get('research_controls') or {}).get(purpose) or {}
    original_photo = binding.get('photo_sha256') or receipt.get('photo_sha256')
    # Terminal readback may outlive the original worker lease, but it may not
    # cross a changed owner epoch, photo, generation, or original job attempt.
    if (not outcome or outcome.get('photo_sha256') != row['photo_sha256']
            or outcome.get('identity_generation') != generation
            or binding.get('story_id') != story_id or binding.get('generation') != generation
            or original_photo is not None and original_photo != row['photo_sha256']
            or int(binding.get('control_revision') or 0) != int(control.get('revision') or 0)
            or control.get('stopped')):
        return None
    if binding.get('job_id'):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND story_id=?',
            (binding['job_id'], story_id)).fetchone()
        if (not job or job['attempts'] != binding.get('job_attempt')
                or job['state'] not in {'done', 'failed'}):
            return None
        payload = json.loads(job['payload_json'] or '{}')
        original_photo = original_photo or payload.get('photo_sha256')
        if (payload.get('photo_sha256', row['photo_sha256']) != row['photo_sha256']
                or payload.get('identity_generation', generation) != generation):
            return None
    if original_photo != row['photo_sha256']:
        return None  # A missing visual photo binding cannot become current by default.
    return outcome


def _claim(service, attempt_id):
    with service.store.tx() as db:
        row = db.execute('SELECT * FROM research_provider_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not row:
            return None
        receipt = json.loads(row['receipt_json'] or '{}')
        outcome = _scope(service, db, row['story_id'], receipt)
        if not outcome or not _unknown(receipt):
            return None
        state = receipt.get('terminal_reconciliation') or {}
        now = service.store.now()
        expires = float(outcome.get('finished_at') or now) + WINDOW_SECONDS
        if (state.get('status') in {'completed', 'failed', 'unaddressable', 'expired', 'exhausted'}
                or state.get('lease_until', 0) > now or state.get('next_read_at', 0) > now):
            return None
        if now >= expires or int(state.get('read_count') or 0) >= MAX_READS:
            receipt['terminal_reconciliation'] = {**state, 'status': 'expired' if now >= expires else 'exhausted',
                'expires_at': expires, 'original_phase': receipt['phase']}
            db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
                (json.dumps(receipt, ensure_ascii=False), now, attempt_id))
            return None
        addressed = (receipt.get('session_id') and receipt.get('message_id')
                     or receipt.get('thread_id') and receipt.get('turn_id'))
        if not addressed:
            receipt['terminal_reconciliation'] = {**state, 'status': 'unaddressable',
                'expires_at': expires, 'original_phase': receipt['phase']}
            db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
                (json.dumps(receipt, ensure_ascii=False), now, attempt_id))
            return None
        token = uuid.uuid4().hex
        receipt['terminal_reconciliation'] = {**state, 'status': 'observing', 'lease_id': token,
            'lease_until': now + 6, 'read_count': int(state.get('read_count') or 0) + 1,
            'next_read_at': now + POLL_SECONDS, 'expires_at': expires,
            'original_phase': receipt['phase']}
        db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
            (json.dumps(receipt, ensure_ascii=False), now, attempt_id))
        return dict(row), receipt, token


def _client(adapter, receipt):
    clients = [getattr(adapter, 'client', None)]
    pool = getattr(adapter, '_fact_pool_routes', None)
    if callable(pool):
        clients.extend(route.get('client') for route in pool())
    for client in clients:
        if (client is None or not callable(getattr(client, '_request', None))
                or receipt.get('model_id') != getattr(client, 'model_id', None)
                or receipt.get('provider_id') != getattr(client, 'provider_id', None)):
            continue
        directory = (receipt.get('isolation') or {}).get('directory')
        if directory and directory != getattr(client, 'directory', None):
            continue
        endpoint = receipt.get('endpoint') or (receipt.get('binding') or {}).get('endpoint')
        if endpoint and endpoint != getattr(client, 'endpoint', None):
            continue
        return client
    return None


def _late_result(result, text, schema):
    encoded = json.dumps(result, ensure_ascii=False) if result is not None else ''
    if len(text) > MAX_RESPONSE_CHARS or len(encoded) > MAX_RESPONSE_CHARS:
        return {'status': 'completed', 'code': 'late_response_too_large',
            'response_sha256': hashlib.sha256((text + encoded).encode()).hexdigest()}
    if result is None:
        try:
            value = text.strip()
            if value.startswith('```json') and value.endswith('```'):
                value = value[7:-3].strip()
            result = json.loads(value)
        except ValueError:
            result = None
    valid = isinstance(schema, dict) and Draft202012Validator(schema).is_valid(result)
    return {'status': 'completed', 'result': result, 'response_text': text,
        'frozen_schema_validated': bool(valid), 'product_projection_allowed': False}


async def _opencode_read(adapter, receipt):
    client = _client(adapter, receipt)
    sid, mid = receipt.get('session_id'), receipt.get('message_id')
    if not client or not isinstance(sid, str) or not re.fullmatch(r'ses[A-Za-z0-9_-]+', sid):
        return {'status': 'unknown', 'code': 'original_transport_unavailable'}
    tokens = []
    # Scoped transport attests the original session's directory/permissions on
    # GET. Supply its original role without invoking _run/admission/abort.
    for name, value in [('_receipt', {'value': receipt}), ('_deadline', time.monotonic() + 6)]:
        context = getattr(client, name, None)
        if callable(getattr(context, 'set', None)):
            tokens.append((context, context.set(value)))
    try:
        messages = await client._request(getattr(client, 'client', None), 'GET',
            f'/session/{sid}/message', params={'limit': 100})
    finally:
        for context, token in reversed(tokens):
            context.reset(token)
    original = next((message for message in messages if message.get('info', {}).get('id') == mid), None)
    if not original:
        return {'status': 'unknown', 'code': 'original_message_not_observed'}
    texts = [part.get('text') for part in original.get('parts', []) if part.get('type') == 'text']
    frozen = receipt.get('frozen_prompt')
    if frozen and (not texts or texts[0] != frozen):
        return {'status': 'unknown', 'code': 'original_prompt_readback_changed'}
    schema = receipt.get('frozen_schema')
    if schema is None and texts and isinstance(texts[0], str):
        # Legacy operations did not save the schema separately. Recover only
        # the contract contained in their exact accepted original user message.
        match = re.search(r'\nResponse JSON schema[^\n]*:\n(\{.*)\Z', texts[0], re.S)
        if match:
            try:
                schema = json.loads(match[1])
            except ValueError:
                pass
    related = [message for message in messages if message.get('info', {}).get('parentID') == mid]
    for message in reversed(related):
        info = message.get('info') or {}
        if info.get('providerID') != receipt.get('provider_id') or info.get('modelID') != receipt.get('model_id'):
            continue
        if info.get('error'):
            return {'status': 'failed', 'assistant_message_id': info.get('id'),
                'error_type': (info['error'] or {}).get('name', 'provider_error')}
        if info.get('time', {}).get('completed') and info.get('finish') not in {'tool-calls', 'unknown'}:
            text = ''.join(part.get('text', '') for part in message.get('parts', []) if part.get('type') == 'text')
            result = _late_result(info.get('structured'), text, schema)
            result.update(assistant_message_id=info.get('id'), usage=client._usage(info)
                if callable(getattr(client, '_usage', None)) else {})
            return result
    return {'status': 'unknown', 'code': 'original_response_pending'}


async def _native_read(adapter, receipt):
    provider = getattr(adapter, 'native_vision', None)
    if provider is None:
        return {'status': 'unknown', 'code': 'original_native_transport_unavailable'}
    client = getattr(provider, 'client', None)
    if client is None:
        factory = getattr(provider, 'client_factory', None)
        if not callable(factory):
            return {'status': 'unknown', 'code': 'original_native_transport_unavailable'}
        client = provider.client = factory()
    read = await client.request('thread/read', {'threadId': receipt['thread_id'], 'includeTurns': True}, timeout=6)
    turn = next((turn for turn in read.get('thread', {}).get('turns', [])
        if turn.get('id') == receipt['turn_id']), None)
    if not turn or turn.get('status') not in {'completed', 'failed', 'interrupted'}:
        return {'status': 'unknown', 'code': 'original_turn_pending'}
    if turn['status'] == 'failed' and not turn.get('error'):
        return {'status': 'unknown', 'code': 'original_turn_failed_without_error'}
    if turn['status'] != 'completed':
        return {'status': 'failed', 'code': 'original_turn_' + turn['status']}
    items = turn.get('items') or []
    if any(item.get('type') not in {'userMessage', 'reasoning', 'agentMessage'} for item in items):
        return {'status': 'unknown', 'code': 'original_turn_tool_boundary_unverified'}
    text = next((item.get('text', '') for item in reversed(items) if item.get('type') == 'agentMessage'), '')
    if not text:
        return {'status': 'unknown', 'code': 'original_turn_text_pending'}
    return _late_result(None, text, receipt.get('frozen_schema'))


def _commit(service, row, token, observation):
    with service.store.tx() as db:
        current = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
            (row['attempt_id'],)).fetchone()
        receipt = json.loads(current['receipt_json'] or '{}') if current else {}
        state = receipt.get('terminal_reconciliation') or {}
        if state.get('lease_id') != token or not _scope(service, db, row['story_id'], receipt):
            return False
        status = observation['status']
        state.update(status=status, lease_until=0, observed_at=service.store.now(),
            code=observation.get('code'))
        if status in {'completed', 'failed'}:
            # This is a closed provider response, not an accepted semantic
            # result. Preserve every original address/frozen binding and fence.
            receipt['late_provider_response'] = observation
            receipt['phase'] = 'response_completed' if status == 'completed' else 'failed'
            receipt['provider_send_state'] = 'completed'
            receipt['retry_safe'] = False
        receipt['terminal_reconciliation'] = state
        db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
            (json.dumps(receipt, ensure_ascii=False), service.store.now(), row['attempt_id']))
    LOG.info('street_story_terminal_readback story_id=%s attempt_id=%s status=%s reads=%s',
        row['story_id'], row['attempt_id'], status, state['read_count'])
    return True


async def reconcile_terminal_attempts(service, *, story_id=None, max_attempts=3, timeout_seconds=6):
    """Observe at most three saved IDs within six seconds; product stays ended."""
    adapter = getattr(service.providers, 'research', None)
    if adapter is None:
        return {'observed': 0, 'closed': 0}
    limit = max(0, min(3, int(max_attempts)))
    deadline = time.monotonic() + max(0, min(6, float(timeout_seconds)))
    with service.store.connection() as db:
        rows = list(db.execute('SELECT a.attempt_id FROM research_provider_attempts a JOIN stories s ON s.id=a.story_id '
            'WHERE json_extract(s.research_json,\'$.automatic_research_outcome\') IS NOT NULL '
            'AND json_extract(a.receipt_json,\'$.phase\') NOT IN (\'created\',\'completed\',\'response_completed\',\'failed\') '
            + ('AND a.story_id=? ' if story_id else '') + 'ORDER BY a.updated_at LIMIT 60',
            (story_id,) if story_id else ()))
    observed, closed = 0, 0
    for candidate in rows:
        if observed >= limit or time.monotonic() >= deadline:
            break
        claimed = _claim(service, candidate['attempt_id'])
        if not claimed:
            continue
        row, receipt, token = claimed
        observed += 1
        try:
            async with asyncio.timeout(max(.001, deadline - time.monotonic())):
                observation = (await _opencode_read(adapter, receipt) if receipt.get('session_id')
                    else await _native_read(adapter, receipt))
        except asyncio.CancelledError:
            _commit(service, row, token, {'status': 'unknown', 'code': 'observer_cancelled'})
            raise
        except Exception as exc:
            observation = {'status': 'unknown', 'code': 'readback_' + type(exc).__name__}
        committed = _commit(service, row, token, observation)
        closed += int(committed and observation['status'] in {'completed', 'failed'})
    return {'observed': observed, 'closed': closed}
