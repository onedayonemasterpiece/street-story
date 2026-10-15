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
    {'preserve_source_frame': True, 'visual_copy': {'title': 'Дом'},
     'visual_layout': {'text_box': [0.1, 0.1, 0.8, 0.3], 'title_size': 32}},
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


def test_artistic_bundles_keep_photo_composition_in_its_stage_without_mutating_shared_schema(tmp_path):
    from copy import deepcopy
    from street_story.live import FUNCTIONS

    _, adapter, session, _ = make_service(tmp_path)
    full = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled',
                              full_configuration=True)['configuration']
    original_functions = deepcopy(FUNCTIONS)
    original_full = deepcopy(full)
    def properties(configuration):
        return next(f for f in configuration['functions'] if f['name'] == 'generate_visual')['parameters']['properties']
    full_fields = {'visual_instruction', 'fact_ids', 'observe_existing_visual',
                   'preserve_source_frame', 'visual_copy', 'visual_layout'}
    bundles = []
    for stage in ('editor', 'photo_format', 'publication', 'research', 'photo_format', 'editor'):
        bundle = adapter._capability_configuration(full, stage)
        expected = full_fields if stage == 'photo_format' else {
            'visual_instruction', 'fact_ids', 'observe_existing_visual'}
        assert set(properties(bundle)) == expected
        bundles.append((bundle, deepcopy(bundle)))
    assert set(properties(full)) == full_fields
    assert full == original_full and FUNCTIONS == original_functions
    assert all(bundle == frozen for bundle, frozen in bundles)
