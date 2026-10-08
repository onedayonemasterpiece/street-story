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


def client(svc, behavior):
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
            try:
                await self.adapter.execute_tool(self.session, {'name': RESULT_TOOL, 'id': 'provider-call',
                    'args': {'facts': ['Evidence-backed result']} if behavior == 'valid' else {'facts': 3}})
            except Exception:
                pass
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
