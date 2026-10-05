"""Live visual requests observe original outcomes through the ordinary async API."""
import json

import pytest

from street_story.service import ConflictError
from test_live_editor import make_service, mark_identity_ready
from test_mvp_visual import previous_visual


def visual_snapshot(service, story_id):
    with service.store.connection() as db:
        row = service._story_row(db, story_id)
        return {'context': json.loads(row['visual_context_json']), 'state': row['state'],
            'revision': row['revision'], 'draft': row['draft_text'],
            'jobs': [dict(job) for job in db.execute(
                "SELECT * FROM jobs WHERE story_id=? AND kind='visual' ORDER BY created_at,id", (story_id,))]}


@pytest.mark.asyncio
@pytest.mark.parametrize('capability', ['research', 'editor', 'publication'])
async def test_live_safe_visual_retry_observes_receipt_and_replays_without_repeating(tmp_path, capability):
    service, adapter, session, _events = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    configuration = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled',
                                       full_configuration=True)['configuration']
    bundle = adapter._capability_configuration(configuration, capability)
    tools = {function['name'] for function in bundle['functions']}
    assert 'generate_visual' in tools
    assert ('prepare_publication' in tools) is (capability == 'publication')
    assert ('confirm_publication' in tools) is (capability == 'publication')
    before = previous_visual(service, session.resource_id)
    observed = []

    async def status(operation):
        observed.append(operation)
        return {'receipts': [{'operation_id': operation, 'state': 'failed', 'retry_safe': True,
            'generation_dispatch': 'not_sent', 'revision': 2}]}

    service.providers.vibepublish.status = status
    call = {'name': 'generate_visual', 'id': 'live-safe-visual-retry', 'args': {}}
    result = await adapter.execute_tool(session, call)
    after = visual_snapshot(service, session.resource_id)
    assert result['accepted'] is True
    assert after['state'] == 'visual_processing'
    assert len(after['jobs']) == 2
    assert after['jobs'][0]['state'] == 'done'
    assert after['jobs'][1]['state'] == 'ready'
    assert result['operation_id'] == after['jobs'][1]['id']
    assert after['context']['content_revision'] != before['content_revision']
    assert after['context']['attempt_history'][0]['operation_id'] == 'visual-op'
    assert after['context']['attempt_history'][0]['outcome']['generation_dispatch'] == 'not_sent'
    assert after['draft'] == 'Original draft'
    assert service._merge_visual_context(session.resource_id, before['content_revision'],
        {'operation_id': 'late-old-operation'}) is None
    repeated = await adapter.execute_tool(session, call)
    assert repeated['operation_id'] == result['operation_id']
    assert visual_snapshot(service, session.resource_id) == after
    assert observed == ['visual-op']


@pytest.mark.asyncio
@pytest.mark.parametrize(('state', 'retry_safe', 'receipt_operation'), [
    ('outcome_unknown', False, 'visual-op'),
    ('outcome_unknown', True, 'visual-op'),
    ('running', False, 'visual-op'),
    ('failed', False, 'visual-op'),
    ('failed', True, 'different-operation'),
])
async def test_live_unsafe_visual_outcome_preserves_frozen_context_and_never_queues_retry(
        tmp_path, state, retry_safe, receipt_operation):
    service, adapter, session, _events = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    previous_visual(service, session.resource_id)
    before = visual_snapshot(service, session.resource_id)
    observed = []

    async def status(operation):
        observed.append(operation)
        return {'receipts': [{'operation_id': receipt_operation, 'state': state, 'retry_safe': retry_safe}]}

    service.providers.vibepublish.status = status
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'live-unsafe-retry', 'args': {}})
    assert error.value.code == 'visual_outcome_unresolved'
    assert visual_snapshot(service, session.resource_id) == before
    assert observed == ['visual-op']
    with service.store.connection() as db:
        assert not db.execute("SELECT 1 FROM live_commands WHERE command_id='live-unsafe-retry'").fetchone()


@pytest.mark.asyncio
async def test_legacy_adapter_fallback_allows_first_request_and_blocks_unobserved_repeat(tmp_path, monkeypatch):
    service, adapter, session, _events = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    monkeypatch.setattr(service, 'request_visual', None)
    first = await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'legacy-first', 'args': {}})
    assert first['accepted'] is True
    before = visual_snapshot(service, session.resource_id)
    assert len(before['jobs']) == 1
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'legacy-repeat', 'args': {}})
    assert error.value.code == 'visual_outcome_unresolved'
    assert visual_snapshot(service, session.resource_id) == before
