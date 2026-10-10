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
async def test_repeated_current_stage_uses_tools_without_provider_reconnect(tmp_path):
    svc, adapter, session, _ = prepared(tmp_path)
    session.capability = 'review'
    call = {'name': 'continue_story', 'id': 'same-review', 'args': {'stage': 'review', 'intent': 'Verify existing candidates'}}
    before = rows(svc, session.resource_id)
    assert adapter.resolve_capability(session, call) is None
    result = await adapter.execute_tool(session, call)
    assert result['ready'] and result['already_active'] and result['next_tool'] == 'read_topic'
    assert rows(svc, session.resource_id) == before


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


def test_every_bundle_exposes_controls_through_one_router(tmp_path):
    _, adapter, session, _ = prepared(tmp_path)
    configuration = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled', full_configuration=True)['configuration']
    for capability in adapter.CAPABILITY_TOOLS:
        bundle = adapter._capability_configuration(configuration, capability)
        assert sum(f['name'] == 'continue_story' for f in bundle['functions']) == 1
        router = next(f for f in bundle['functions'] if f['name'] == 'continue_story')
        properties = router['parameters']['properties']
        assert properties['research_purpose']['enum'] == ['identity', 'facts', 'all']
        assert properties['research_action']['enum'] == ['stop', 'resume']


def test_review_setup_keeps_subject_and_run_without_resending_whole_inventory():
    import copy

    context = {
        'story_id': 'subject-story', 'photo_sha256': 'a' * 64, 'identity_generation': 2,
        'physical_identity_accepted': True, 'selected_fact_ids': ['selected-1'],
        'publication_concept': 'Saved author angle', 'fact_count': 80,
        'visual_identity': {'candidate_id': 'osm:way:91', 'candidate_name': 'Named subject',
            'physical_scope': 'This photographed building', 'aliases': ['Old subject name'],
            'observations': ['Detailed identity justification' * 100]},
        'known_fact_inventory': [['fact', 'Long claim' * 1000]],
        'draft_text': 'Saved draft' * 1000, 'previous_editorial_context': ['Old draft' * 1000],
        'research_run': {'run_id': 'current-run', 'state': 'verifying',
            'pending_review_fact_ids': ['requested-1'], 'identity_article_sources': ['Large discovery' * 1000]},
    }
    original = copy.deepcopy(context)
    result = StreetStoryLiveAdapter._capability_context(context, 'review')
    assert context == original
    assert result['visual_identity']['physical_scope'] == 'This photographed building'
    assert result['visual_identity']['candidate_name'] == 'Named subject'
    assert result['visual_identity']['aliases'] == ['Old subject name']
    assert result['visual_identity']['physical_identity_accepted'] is True
    unresolved = StreetStoryLiveAdapter._capability_context({**context, 'physical_identity_accepted': False}, 'review')
    assert unresolved['visual_identity']['physical_identity_accepted'] is False
    assert result['research_run'] == {'run_id': 'current-run', 'state': 'verifying',
                                    'pending_review_fact_ids': ['requested-1']}
    assert result['selected_fact_ids'] == ['selected-1']
    assert result['publication_concept'] == 'Saved author angle'
    assert 'known_fact_inventory' not in result and 'draft_text' not in result
    assert 'observations' not in result['visual_identity']
    assert StreetStoryLiveAdapter._capability_context(context, 'editor') == original


@pytest.mark.asyncio
async def test_research_session_persists_requested_concept_and_text_without_stage_switch(tmp_path):
    from street_story.live import FUNCTIONS

    service, adapter, session, _ = prepared(tmp_path)
    bundle = adapter._capability_configuration({'functions': FUNCTIONS}, 'research')
    names = {function['name'] for function in bundle['functions']}
    assert {'search_web', 'get_research_chunk', 'set_concept', 'edit_text'} <= names
    assert 'Persist an owner' in bundle['system_instruction']
    assert 'only selected evidence-backed facts' in bundle['system_instruction']
    assert 'A research-only request must not select facts or draft a publication' in bundle['system_instruction']

    await adapter.execute_tool(session, {'name': 'set_concept', 'id': 'requested-concept',
                                      'args': {'concept': 'История городских ворот'}})
    await adapter.execute_tool(session, {'name': 'edit_text', 'id': 'requested-draft',
                                      'args': {'expected_text_revision': 0, 'new_text': 'Сохранённый текст.',
                                               'change_summary': 'Первый текст по просьбе автора'}})
    story = service.story(session.resource_id)
    assert story['publication_concept'] == 'История городских ворот'
    assert story['draft_text'] == 'Сохранённый текст.'
    with service.store.connection() as db:
        tools = [row[0] for row in db.execute('SELECT tool_name FROM live_commands WHERE story_id=?',
                                             (session.resource_id,))]
    assert 'set_concept' in tools and 'edit_text' in tools and 'continue_story' not in tools


@pytest.mark.asyncio
async def test_more_after_editing_reads_saved_document_and_preserves_draft(tmp_path):
    from street_story.research_runs import persist_source_version

    service, adapter, session, _ = prepared(tmp_path)
    with service.store.tx() as db:
        persist_source_version(db, run_id='partial-run', requested_url='https://example.org/history',
                               final_url='https://example.org/history', title='History', content_type='text/html',
                               http_status=200, redirect_chain=[], normalized_text='Documented history. ' * 80,
                               read_status='complete', now=service.store.now())
    begin_turn(session, 'Сохрани концепцию', origin='text')
    await adapter.execute_tool(session, {'name': 'set_concept', 'id': 'concept-before-more',
                                       'args': {'concept': 'История ворот'}})
    with pytest.raises(ConflictError, match='paused'):
        await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': 'partial-run'}})

    begin_turn(session, 'Найди ещё факты, сохрани текущий текст', origin='text')
    page = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': 'partial-run'}})
    assert page['research_run_id'] == 'partial-run'
    assert page['evidence_passages']
    assert service.story(session.resource_id)['draft_text'] == 'Owner draft'
    assert service.story(session.resource_id)['publication_concept'] == 'История ворот'
    assert not session.state['research_cancelled']
    assert not session.state['research_author_interrupted']
    with service.store.connection() as db:
        assert db.execute("SELECT state FROM research_runs WHERE run_id='partial-run'").fetchone()[0] == 'extracting'


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_reason', ['live_stopped_resume_required', 'invalid_batch_budget_exhausted'])
async def test_more_cannot_bypass_other_research_pauses(tmp_path, pause_reason):
    _, adapter, session, _ = prepared(tmp_path)
    adapter._pause_research(session, pause_reason)
    begin_turn(session, 'Найди ещё факты', origin='text')
    with pytest.raises(ConflictError) as denied:
        await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': 'partial-run'}})
    assert denied.value.code == 'live_research_partial'
    assert session.state['research_cancelled']


@pytest.mark.asyncio
async def test_more_cannot_bypass_explicit_stop(tmp_path):
    _, adapter, session, _ = prepared(tmp_path)
    await control(adapter, session, 'stop', 'facts', 'owner-stop')
    begin_turn(session, 'Найди ещё факты', origin='text')
    with pytest.raises(ConflictError) as denied:
        await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': 'partial-run'}})
    assert denied.value.code == 'live_research_stopped'


def test_more_keeps_completed_run_immutable(tmp_path):
    service, adapter, session, _ = prepared(tmp_path)
    with service.store.tx() as db:
        db.execute("UPDATE research_runs SET state='completed' WHERE run_id='partial-run'")
    adapter._pause_research(session, 'live_owner_switched_to_editing')
    begin_turn(session, 'Найди ещё факты', origin='text')
    adapter._enter_requested_research(session)
    assert not session.state['research_cancelled']
    assert 'research_run_id' not in session.state
    with service.store.connection() as db:
        assert db.execute("SELECT state FROM research_runs WHERE run_id='partial-run'").fetchone()[0] == 'completed'


def test_more_does_not_clear_pause_when_old_run_is_stale(tmp_path):
    service, adapter, session, _ = prepared(tmp_path)
    adapter._pause_research(session, 'live_owner_switched_to_editing')
    with service.store.tx() as db:
        db.execute("UPDATE research_runs SET identity_generation=1 WHERE run_id='partial-run'")
    begin_turn(session, 'Найди ещё факты', origin='text')
    with pytest.raises(ConflictError) as denied:
        adapter._enter_requested_research(session)
    assert denied.value.code == 'live_research_run_stale'
    assert session.state['research_cancelled']


def test_stage_prompts_advertise_only_available_tools_and_leave_room_for_transition(tmp_path):
    import json
    import re
    from live_interaction.provider import setup_config
    from street_story.live import FUNCTIONS
    from street_story.review_packets import EXTRACTION_CHECKS, REVIEW_CHECKS

    _, adapter, session, _ = prepared(tmp_path)
    full = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled',
                              full_configuration=True)['configuration']
    for capability, tools in adapter.CAPABILITY_TOOLS.items():
        bundle = adapter._capability_configuration(full, capability)
        instruction = bundle['system_instruction']
        for name in {f['name'] for f in FUNCTIONS} - tools:
            assert not re.search(r'\b' + re.escape(name) + r'\b', instruction), (capability, name)
        assert 'continue_story first' in instruction
        assert 'explicitly requested correction or independent reconsideration' in instruction
        assert 'An explanation alone does not change eligibility or POI memory' in instruction
        if capability != 'review':
            assert 'use continue_story(stage=review)' in instruction
        else:
            assert 'read get_review_packet with the exact requested fact_ids and run_id' in instruction
            assert 'finalize_fact_review or repair/review the changed claim' in instruction
        if capability not in {'research', 'review'}:
            assert EXTRACTION_CHECKS not in instruction and REVIEW_CHECKS not in instruction
    publication = adapter._capability_configuration(full, 'publication')
    setup = setup_config('gemini-3.8-live', {}, configuration=publication, search=False)['setup']
    # Previously the publication setup included all research/review/editor rules
    # (25,763 estimated units) and exhausted the unchanged rolling grant.
    assert len(json.dumps(setup, ensure_ascii=False, separators=(',', ':')).encode()) < 10_000
    assert 'separate unambiguous author confirmation' in publication['system_instruction']
    assert 'a readable subset of owner-selected eligible facts' in publication['system_instruction']
    assert 'never use unselected facts' in publication['system_instruction']
