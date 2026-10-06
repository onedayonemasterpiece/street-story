"""Normal Live can recover the saved image importer without another generation."""
import json

import pytest

from street_story.service import ConflictError
from test_live_editor import make_service, mark_identity_ready
from test_live_visual_retry import visual_snapshot
from test_mvp_visual import PROCESSED, PROCESSED_SHA, previous_visual


def existing_operation(service, sid):
    previous = previous_visual(service, sid)
    previous.update(source_asset_ref='source-asset', visual_command={'command': {'kind': 'tune'}})
    with service.store.tx() as db:
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(previous), sid))
    return previous


@pytest.mark.asyncio
async def test_normal_live_observation_requeues_same_context_and_imports_without_generation(tmp_path):
    service, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    previous = existing_operation(service, session.resource_id)
    reads = []

    async def status(operation):
        reads.append(operation)
        return {'receipts': [{'operation_id': operation, 'state': 'verified',
            'visual_job_id': 'original-job', 'selected_asset_ref': 'processed-asset',
            'selected_sha256': PROCESSED_SHA}]}

    async def forbidden(*args):
        raise AssertionError('Result observation must not submit generation or another source')

    async def read_asset(asset):
        assert asset == 'processed-asset'
        return PROCESSED, 'image/png'

    service.providers.vibepublish.status = status
    service.providers.vibepublish.visual = forbidden
    service.providers.vibepublish.ingress_asset = forbidden
    service.providers.vibepublish.read_asset = read_asset
    call = {'name': 'generate_visual', 'id': 'recover-existing', 'args': {'observe_existing_visual': True}}
    result = await adapter.execute_tool(session, call)
    queued = visual_snapshot(service, session.resource_id)
    assert result['accepted'] and queued['context'] == previous
    assert len(queued['jobs']) == 2 and queued['jobs'][1]['state'] == 'ready'
    assert (await adapter.execute_tool(session, call)) == result
    assert visual_snapshot(service, session.resource_id) == queued
    await service._run_visual(queued['jobs'][1])
    after = visual_snapshot(service, session.resource_id)
    assert after['state'] == 'ready_to_publish' and after['draft'] == 'Original draft'
    assert after['context']['operation_id'] == previous['operation_id']
    assert after['context']['content_revision'] == previous['content_revision']
    assert 'generation_attempt' not in after['context']
    assert after['context']['selected_sha256'] == PROCESSED_SHA
    assert reads == ['visual-op', 'visual-op', 'visual-op']
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model=session.model)
    assert initialized['capability'] == 'publication'
    tools = {tool['name'] for tool in initialized['configuration']['functions']}
    assert {'prepare_publication', 'confirm_publication'} <= tools
    assert visual_snapshot(service, session.resource_id) == after


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['outcome_unknown', 'running', 'failed'])
async def test_unresolved_observation_keeps_original_context_and_jobs(tmp_path, state):
    service, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    existing_operation(service, session.resource_id)
    before = visual_snapshot(service, session.resource_id)

    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': state, 'retry_safe': True}]}

    service.providers.vibepublish.status = status
    with pytest.raises(ConflictError, match='not ready'):
        await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'unknown-observation',
            'args': {'observe_existing_visual': True}})
    assert visual_snapshot(service, session.resource_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['photo', 'concept', 'instruction'])
async def test_observation_cannot_reuse_stale_or_new_visual_inputs(tmp_path, change):
    service, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    existing_operation(service, session.resource_id)
    with service.store.tx() as db:
        if change == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('changed-photo', session.resource_id))
        elif change == 'concept':
            row = service._story_row(db, session.resource_id)
            research = json.loads(row['research_json'])
            research['publication_concept'] = 'New concept'
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    before = visual_snapshot(service, session.resource_id)

    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': 'needs_selection'}]}

    service.providers.vibepublish.status = status
    args = {'observe_existing_visual': True}
    if change == 'instruction':
        args['visual_instruction'] = 'Make another visual'
    with pytest.raises(ConflictError, match='no longer matches'):
        await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'stale-observation', 'args': args})
    assert visual_snapshot(service, session.resource_id) == before


@pytest.mark.asyncio
async def test_wrong_default_on_existing_non_selected_image_cannot_regenerate(tmp_path):
    service, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    existing_operation(service, session.resource_id)
    before = visual_snapshot(service, session.resource_id)

    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': 'needs_selection'}]}

    service.providers.vibepublish.status = status
    with pytest.raises(ConflictError, match='Import and review'):
        await adapter.execute_tool(session, {'name': 'generate_visual', 'id': 'wrong-default', 'args': {}})
    assert visual_snapshot(service, session.resource_id) == before
