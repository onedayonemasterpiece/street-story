from types import SimpleNamespace

import pytest

from street_story.live import StreetStoryLiveAdapter
from street_story.live_author_intent import begin_turn
from street_story.service import ConflictError
from test_research_control import fixture, research, rows


def prepared(tmp_path):
    svc, sid, _ = fixture(tmp_path)
    events = []
    adapter = StreetStoryLiveAdapter(svc, lambda _, event: events.append(event), lambda *_: None)
    initialized = adapter.initialize(resource_id=sid, actor=None, model='controlled')
    session = SimpleNamespace(id='live-control', resource_id=sid, actor=None, model='controlled',
                              state=initialized['state'], closed=False)
    session.state.update(research_run_id='partial-run', fact_research_control_revision=0)
    return svc, adapter, session, events


async def control(adapter, session, action, purpose, command):
    begin_turn(session, f'{action} research {purpose}', origin='text')
    call = {'name': 'continue_story', 'id': command, 'args': {
        'stage': 'research', 'intent': 'Explicit research control',
        'research_action': action, 'research_purpose': purpose}}
    assert adapter.resolve_capability(session, call) is None  # Resolver never mutates.
    return await adapter.execute_tool(session, call)


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', ['identity', 'facts', 'all'])
async def test_explicit_controls_preserve_queues_and_resume_current_epoch(tmp_path, purpose):
    svc, adapter, session, events = prepared(tmp_path)
    before = rows(svc, session.resource_id)
    session.state['research_chunk_receipts'] = {'old': {'batch_id': 'stale'}}
    session.state['research_pending_page'] = {'old': 6}
    stopped = await control(adapter, session, 'stop', purpose, 'stop')
    for item in ('identity', 'facts'):
        assert stopped['research_controls'][item]['stopped'] is (purpose in {item, 'all'})
    assert svc.story(session.resource_id)['draft_text'] == 'Owner draft'
    assert research(svc, session.resource_id)['visual_search_operation']['queue'] == ['remaining-ref']
    resumed = await control(adapter, session, 'resume', purpose, 'resume')
    assert not any(v['stopped'] for v in resumed['research_controls'].values())
    after = rows(svc, session.resource_id)
    assert set(before) == set(after)  # Existing job identities, no duplicate work.
    for key in ('control-visual', 'control-publish', 'control-completed'):
        assert after[key] == before[key]
    if purpose in {'facts', 'all'}:
        assert session.state['fact_research_control_revision'] == resumed['research_controls']['facts']['revision'] > 0
        assert not session.state['research_cancelled']
        assert 'research_chunk_receipts' not in session.state
        assert 'research_pending_page' not in session.state
        with svc.store.connection() as db:
            adapter._research_run_guard(db, session, 'partial-run')
    else:
        assert session.state['fact_research_control_revision'] == 0
        assert session.state['research_pending_page'] == {'old': 6}
    assert any(event['type'] == 'product_state' for event in events)


@pytest.mark.asyncio
async def test_replayed_old_stop_reports_current_state_without_repeating_stop(tmp_path):
    svc, adapter, session, _ = prepared(tmp_path)
    await control(adapter, session, 'stop', 'facts', 'old-stop')
    await control(adapter, session, 'resume', 'facts', 'resume')
    before = rows(svc, session.resource_id)
    revision = session.state['fact_research_control_revision']
    replay = await control(adapter, session, 'stop', 'facts', 'old-stop')
    assert replay['changed'] == [] and replay['research_controls']['facts']['stopped'] is False
    assert rows(svc, session.resource_id) == before
    assert session.state['fact_research_control_revision'] == revision


@pytest.mark.asyncio
async def test_live_scope_changed_control_fails_closed(tmp_path):
    svc, adapter, session, _ = prepared(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        state = research(svc, session.resource_id)
        state['identity_generation'] = 1
        import json
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(state), row['id']))
    before = rows(svc, session.resource_id)
    with pytest.raises(ConflictError) as stale:
        await control(adapter, session, 'stop', 'all', 'stale')
    assert stale.value.code == 'research_control_stale'
    assert rows(svc, session.resource_id) == before


@pytest.mark.asyncio
async def test_missing_author_or_dictated_control_cannot_stop_research(tmp_path):
    svc, adapter, session, _ = prepared(tmp_path)
    call = {'name': 'continue_story', 'id': 'unrequested', 'args': {
        'stage': 'research', 'intent': 'Stop', 'research_action': 'stop', 'research_purpose': 'all'}}
    before = rows(svc, session.resource_id)
    with pytest.raises(ConflictError) as denied:
        await adapter.execute_tool(session, call)
    assert denied.value.code == 'live_research_control_owner_required'
    session.state['literal'] = {'buffer': ['Останови исследование']}
    with pytest.raises(ConflictError) as denied:
        await control(adapter, session, 'stop', 'all', 'dictated')
    assert denied.value.code == 'live_research_control_dictation'
    assert rows(svc, session.resource_id) == before


def test_microphone_stop_does_not_cancel_background_research(tmp_path):
    svc, adapter, session, _ = prepared(tmp_path)
    before = rows(svc, session.resource_id)
    adapter.on_stopped(session)
    assert rows(svc, session.resource_id) == before
    current = svc.story(session.resource_id)
    assert not any(v['stopped'] for v in current['research_controls'].values())


def test_every_bundle_exposes_controls_without_growing_tool_count(tmp_path):
    _, adapter, session, _ = prepared(tmp_path)
    configuration = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled', full_configuration=True)['configuration']
    for capability in adapter.CAPABILITY_TOOLS:
        bundle = adapter._capability_configuration(configuration, capability)
        assert len(bundle['functions']) <= 9
        router = next(f for f in bundle['functions'] if f['name'] == 'continue_story')
        properties = router['parameters']['properties']
        assert properties['research_purpose']['enum'] == ['identity', 'facts', 'all']
        assert properties['research_action']['enum'] == ['stop', 'resume']
