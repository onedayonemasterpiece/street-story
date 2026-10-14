"""Actual editor tools leave evidence durable and normalize absent visual options."""
import json

import pytest

from test_live_editor import make_service, mark_identity_ready


@pytest.mark.asyncio
async def test_editor_topic_read_keeps_persisted_research_out_of_model_reply(tmp_path, monkeypatch):
    service, adapter, session, events = make_service(tmp_path)
    session.capability = 'editor'
    state = adapter._topic_state(session.resource_id)
    state['story']['identity_mode'] = 'manual'
    state['story']['draft_text'] = 'Сохранённый текст автора.'
    state['research_run'] = {'run_id': 'original-run', 'received_source': 'Retained source ' * 5000}
    state['jobs'] = [{'id': 'original-job', 'payload': 'Frozen checkpoint ' * 1000}]
    monkeypatch.setattr(adapter, '_topic_state', lambda _sid: state)
    result = await adapter.execute_tool(session, {'name': 'read_topic', 'id': 'editor-read', 'args': {}})
    assert result['draft_text'] == state['story']['draft_text']
    assert result['story_id'] == session.resource_id
    assert len(json.dumps(result)) < 12_000
    assert 'research_run' not in result and 'jobs' not in result
    pushed = next(event['state'] for event in events if event.get('type') == 'product_state')
    assert pushed['research_run'] == state['research_run']
    assert pushed['jobs'] == state['jobs']


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    {'preserve_source_frame': None, 'visual_copy': None, 'visual_layout': None},
    {'preserve_source_frame': False, 'visual_copy': {'title': 'Дом'}, 'visual_layout': {}},
])
async def test_visual_optional_nulls_match_omission_and_explicit_values_survive(tmp_path, options):
    service, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    observed = []
    async def request(story_id, key, body):
        observed.append(body)
        return service.story(story_id)
    service.request_visual = request
    result = await adapter._generate_visual(session.resource_id, 'visual-options',
        {'visual_instruction': 'Архитектурный скетч.', **options})
    assert result['accepted'] is True
    assert observed == [{'selected_fact_ids': [], 'visual_instruction': 'Архитектурный скетч.',
                         **{key: value for key, value in options.items() if value is not None}}]
