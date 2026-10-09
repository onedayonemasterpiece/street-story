import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story.live_research import LiveSemanticClient, RESULT_TOOL
from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter
from street_story.research_budget import ensure_budget
from test_mvp_research import service

SCHEMA = {'type': 'object', 'properties': {'facts': {'type': 'array', 'items': {'type': 'string'}}},
          'required': ['facts'], 'additionalProperties': False}


def client(svc, behavior, *, tool_args=None, followup_calls=(), late_events=()):
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = svc
    calls = []
    class Host:
        def __init__(self, _service, _settings, **kwargs):
            self.adapter = kwargs['operation_adapter_factory']()
            self.guard = kwargs['before_operation_send']
            self.session = SimpleNamespace(id='original-live-session')
        async def start(self, **kwargs):
            calls.append(('start', kwargs))
            self.initialized = self.adapter.initialize(**kwargs)
            if behavior == 'setup_closed':
                from websockets.exceptions import ConnectionClosedError
                from websockets.frames import Close
                raise ConnectionClosedError(Close(1007, 'Invalid function schema'), None)
            self.adapter.on_event(self.session, {'type': 'ready'})
            if behavior == 'quota':
                self.adapter.on_event(self.session, {'type': 'error', 'code': 'RESOURCE_DAILY_BUDGET'})
                raise RuntimeError('setup denied')
            return {'session_id': self.session.id}
        async def input(self, **kwargs):
            self.guard()
            # Exercise the real shared host's text-turn validation instead of a
            # permissive test double that silently accepts arbitrary prompts.
            from live_interaction.session_host import LiveSessionHost
            shim = SimpleNamespace(adapter=SimpleNamespace(), _touch=lambda session: None,
                _get=lambda *args: self.session, _write=lambda session, frame: 1)
            await LiveSessionHost.input(shim, **kwargs)
            calls.append(('input', kwargs))
            self.adapter.on_event(self.session, {'type': 'input_timing', 'text_sent_at': 1})
            if behavior == 'pending':
                return
            self.adapter.on_event(self.session, {'type': 'usage', 'metadata': {'totalTokenCount': 77}})
            first = {'name': RESULT_TOOL, 'id': 'provider-call', 'args': tool_args if tool_args is not None
                     else {'facts': ['Evidence-backed result']} if behavior == 'valid' else {'facts': 3}}
            for call in [first, *followup_calls]:
                try:
                    await self.adapter.execute_tool(self.session, call)
                except Exception as exc:
                    calls.append(('tool_error', getattr(exc, 'code', type(exc).__name__)))
            for event in late_events:
                self.adapter.on_event(self.session, event)
        async def stop_all(self):
            calls.append(('stop', None))
    return LiveSemanticClient(adapter, host_factory=Host), calls


def binding(svc, story):
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            ('live-original', 'one-unit', story['id'], 'facts_live', '{}', svc.store.now(), svc.store.now()))
    return {'attempt_id': 'live-original', 'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
            'generation': 0, 'control_revision': 0, 'purpose': 'facts'}


@pytest.mark.asyncio
async def test_live_capability_returns_only_schema_bound_result_and_closes(tmp_path):
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'valid')
    result = await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    assert result['result']['facts'] == ['Evidence-backed result']
    assert result['receipt']['phase'] == 'completed'
    assert result['receipt']['text_sends'] == 1
    assert result['receipt']['usage_snapshots'] == [{'totalTokenCount': 77}]
    assert [name for name, _ in calls] == ['start', 'input', 'stop']
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['phase'] == 'completed'
    assert saved['result'] == result['result']


@pytest.mark.asyncio
async def test_long_frozen_evidence_is_delivered_in_actual_setup_and_one_small_text_turn(tmp_path):
    from live_interaction.provider import setup_config
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'valid')
    # More than the real host's 4000-character conversation limit, within the
    # product's existing frozen-operation bound. Nothing is truncated.
    prompt = 'Exact source passage and subject binding. ' * 300
    captured = {}
    original = provider.host_factory
    class CaptureHost(original):
        async def start(self, **kwargs):
            result = await super().start(**kwargs)
            captured.update(self.initialized)
            return result
    provider.host_factory = CaptureHost
    result = await provider._run('facts', prompt, binding(svc, story), SCHEMA)
    assert captured['context']['frozen_research_operation']['prompt'] == prompt
    setup = setup_config(provider.model_id, captured['context'], None,
        configuration=captured['configuration'], search=False)
    assert prompt in json.dumps(setup, ensure_ascii=False)
    turns = [args['message']['text'] for kind, args in calls if kind == 'input']
    assert len(turns) == 1 and len(turns[0]) < 4000
    assert result['receipt']['text_sends'] == 1 and result['receipt']['phase'] == 'completed'
    assert result['receipt']['source_prompt_chars'] == len(prompt)


def test_private_json_schema_is_preserved_in_shared_live_wire(tmp_path):
    from live_interaction.provider import setup_config
    svc, _, _ = service(tmp_path)
    provider, _ = client(svc, 'valid')
    schema = {'type': 'object', 'properties': {
        'equivalent_to': {'type': ['integer', 'null'], 'enum': [0, 1, None]}},
        'additionalProperties': False}
    context, configuration, trigger, measured = provider._prepared_input('facts', 'Frozen packet', schema)
    setup = setup_config(provider.model_id, context, configuration=configuration, search=False)
    function = setup['setup']['tools'][0]['functionDeclarations'][0]
    assert function['parametersJsonSchema'] == schema
    assert 'parameters' not in function
    assert measured['input_utf8_bytes'] == len(json.dumps(setup).encode()) + len(json.dumps(trigger).encode())


@pytest.mark.asyncio
async def test_setup_close_retains_reason_and_definitive_unsent_state(tmp_path):
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'setup_closed')
    with pytest.raises(ResearchUnavailable):
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['provider_close_code'] == 1007
    assert saved['provider_close_reason'] == 'Invalid function schema'
    assert saved['provider_send_state'] == 'not_sent' and saved['text_sends'] == 0
    assert [kind for kind, _ in calls] == ['start', 'stop']


@pytest.mark.asyncio
async def test_live_setup_quota_is_exact_not_sent_blocker(tmp_path):
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'quota')
    with pytest.raises(ResearchUnavailable) as error:
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    assert error.value.code == 'RESOURCE_DAILY_BUDGET'
    assert error.value.receipt['provider_send_state'] == 'not_sent'
    assert [name for name, _ in calls] == ['start', 'stop']


@pytest.mark.asyncio
async def test_live_timeout_preserves_unknown_and_will_not_restart(tmp_path):
    svc, _, story = service(tmp_path)
    svc.settings = replace(svc.settings, research_timeout_seconds=.5)
    ensure_budget(svc, story['id'], explicit=True)
    provider, calls = client(svc, 'pending')
    original = binding(svc, story)
    with pytest.raises(ResearchUnavailable) as error:
        await provider._run('facts', 'Frozen evidence', original, SCHEMA)
    receipt = error.value.receipt
    assert receipt['phase'] == 'unknown'
    assert receipt['session_id'] == 'original-live-session'
    before = list(calls)
    with pytest.raises(ResearchUnavailable, match='original_outcome_unknown'):
        await provider._run('facts', 'Frozen evidence', {**original, 'phase': 'unknown'}, SCHEMA)
    assert calls == before


@pytest.mark.asyncio
async def test_live_malformed_result_is_withheld(tmp_path):
    svc, _, story = service(tmp_path)
    provider, _ = client(svc, 'invalid')
    with pytest.raises(ResearchUnavailable, match='result_malformed'):
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['phase'] == 'failed'
    assert 'result' not in saved
    assert saved['provider_call_id'] == 'provider-call'
    assert saved['malformed_args'] == {'facts': 3}
    assert saved['validation_errors'][0]['instance_path'] == ['facts']
    assert saved['validation_errors'][0]['schema_path'] == ['properties', 'facts', 'type']
    assert saved['validation_errors'][0]['validator'] == 'type'


@pytest.mark.parametrize('args,path,validator', [
    ({'facts': [777]}, ['facts', 0], 'type'),
    ({}, [], 'required'),
    ({'facts': [], 'private_note': 'Synthetic private body'}, [], 'additionalProperties'),
])
@pytest.mark.asyncio
async def test_live_schema_failure_retains_exact_bounded_args_and_paths_without_logging_values(tmp_path, caplog, args, path, validator):
    from street_story.service import canonical
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'invalid', tool_args=args)
    caplog.set_level('INFO', logger='street_story.live_research')
    with pytest.raises(ResearchUnavailable) as error:
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['phase'] == 'failed' and saved['provider_send_state'] == 'response_closed'
    assert saved['error_code'] == error.value.code == 'live_research_result_malformed'
    assert saved['provider_call_id'] == 'provider-call'
    assert saved['malformed_args'] == args
    encoded = canonical(args).encode('utf-8')
    assert saved['malformed_args_sha256'] == hashlib.sha256(encoded).hexdigest()
    assert saved['malformed_args_utf8_bytes'] == len(encoded) and saved['malformed_args_truncated'] is False
    assert saved['validation_errors'][0]['instance_path'] == path
    assert saved['validation_errors'][0]['validator'] == validator
    if validator == 'required':
        assert saved['validation_errors'][0]['missing_properties'] == ['facts']
    assert saved['usage_snapshots'] == [{'totalTokenCount': 77}] and saved['text_sends'] == 1
    assert 'result' not in saved
    assert [name for name, _ in calls].count('input') == 1
    assert 'provider-call' in caplog.text and validator in caplog.text
    assert 'Synthetic private body' not in caplog.text and 'private_note' not in caplog.text
    assert all('message' not in entry for entry in saved['validation_errors'])


@pytest.mark.asyncio
async def test_live_oversized_malformed_args_keep_exact_digest_and_explicit_bounded_utf8_prefix(tmp_path, caplog):
    from street_story.live_research import MALFORMED_ARGS_BYTES, MALFORMED_PREFIX_BYTES
    from street_story.service import canonical
    svc, _, story = service(tmp_path)
    args = {'facts': 3, 'private_note': 'Synthetic private body: ' + 'Ж' * 20000}
    provider, _ = client(svc, 'invalid', tool_args=args)
    caplog.set_level('INFO', logger='street_story.live_research')
    with pytest.raises(ResearchUnavailable, match='result_malformed'):
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    encoded = canonical(args).encode('utf-8')
    assert len(encoded) > MALFORMED_ARGS_BYTES
    assert saved['malformed_args_sha256'] == hashlib.sha256(encoded).hexdigest()
    assert saved['malformed_args_utf8_bytes'] == len(encoded) and saved['malformed_args_truncated'] is True
    assert 'malformed_args' not in saved and 'result' not in saved
    prefix = saved['malformed_args_json_prefix'].encode('utf-8')
    assert len(prefix) <= MALFORMED_PREFIX_BYTES and encoded.startswith(prefix)
    assert len(canonical(saved).encode('utf-8')) < MALFORMED_ARGS_BYTES
    assert 'Synthetic private body' not in caplog.text and 'Ж' not in caplog.text


@pytest.mark.asyncio
async def test_live_many_schema_errors_are_explicitly_bounded(tmp_path):
    from street_story.live_research import VALIDATION_ERRORS
    svc, _, story = service(tmp_path)
    args = {'facts': [777] * (VALIDATION_ERRORS + 20)}
    provider, _ = client(svc, 'invalid', tool_args=args)
    with pytest.raises(ResearchUnavailable, match='result_malformed'):
        await provider._run('facts', 'Frozen evidence', binding(svc, story), SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['validation_errors_truncated'] is True
    assert len(saved['validation_errors']) == VALIDATION_ERRORS
    assert [entry['instance_path'] for entry in saved['validation_errors']] == [['facts', i] for i in range(VALIDATION_ERRORS)]
    assert saved['malformed_args'] == args and saved['phase'] == 'failed' and 'result' not in saved


@pytest.mark.asyncio
async def test_first_closed_live_schema_failure_cannot_be_salvaged_or_overwritten_by_later_callbacks(tmp_path):
    svc, _, story = service(tmp_path)
    args = {'facts': 3}
    followups = [{'name': RESULT_TOOL, 'id': 'later-valid-call', 'args': {'facts': ['Salvaged output']}},
                 {'name': RESULT_TOOL, 'id': 'later-invalid-call', 'args': {'facts': [777]}}]
    events = [{'type': 'usage', 'metadata': {'totalTokenCount': 88}}, {'type': 'error', 'code': 'LATE_TRANSPORT_ERROR'}]
    provider, calls = client(svc, 'invalid', tool_args=args, followup_calls=followups, late_events=events)
    original = binding(svc, story)
    with pytest.raises(ResearchUnavailable, match='result_malformed') as error:
        await provider._run('facts', 'Frozen evidence', original, SCHEMA)
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id='live-original'").fetchone()[0])
    assert saved['phase'] == 'failed' and saved['provider_send_state'] == 'response_closed'
    assert saved['error_code'] == error.value.code == 'live_research_result_malformed'
    assert saved['malformed_args'] == args and saved['provider_call_id'] == 'provider-call'
    assert saved['validation_errors'][0]['instance_path'] == ['facts'] and 'result' not in saved
    assert saved['usage_snapshots'] == [{'totalTokenCount': 77}, {'totalTokenCount': 88}]
    assert [value for name, value in calls if name == 'tool_error'] == [
        'live_research_result_malformed', 'live_research_result_closed', 'live_research_result_closed']
    before = list(calls)
    with pytest.raises(ResearchUnavailable, match='original_outcome_unknown'):
        await provider._run('facts', 'Frozen evidence', {**original, 'phase': 'failed'}, SCHEMA)
    assert calls == before
