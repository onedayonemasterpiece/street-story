import json

import pytest

from street_story.service import canonical
from test_visual_search_continuation import prepared
from visual_queue_fixture import reference_receipt


@pytest.mark.asyncio
async def test_initial_nearby_wikipedia_is_semantically_selected_with_source_and_retains_alternatives(tmp_path):
    svc, adapter, story, sessions = prepared(tmp_path)
    candidates = [{'candidate_id': f'wiki:{i}', 'name': f'Physical {i}', 'distance_m': 30+i,
        'url': f'https://ru.wikipedia.org/wiki/Physical_{i}',
        'reference_image_urls': [f'https://upload.wikimedia.org/ref-{i}.jpg']}
        for i in range(2)]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), story['id']))
    selections, images = [], []

    async def select(query, observed, snapshot):
        selections.append(observed)
        assert snapshot['_identity_selection_image'][1]
        return {'sources': [observed[1]], 'source_selection': {'status': 'model_selected'}}

    async def references(batch, limit, *, story_id, evidence):
        images.extend(x['candidate_id'] for x in batch)
        evidence.append(reference_receipt(batch[0]))
        return [(batch[0]['candidate_id'], 'image/jpeg', b'fixture')]

    svc.providers.gemini.select_identity_sources = select
    svc._candidate_reference_images = references
    session = sessions()
    reply = await adapter._compare_place_images(session, {})
    repeated = await adapter._compare_place_images(session, {})
    assert repeated['comparison_id'] == reply['comparison_id']
    assert len(selections) == 1 and images == ['wiki:1']
    assert reply['references'][0]['candidate_id'] == 'wiki:1'
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'
    with svc.store.connection() as db:
        state = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
    operation = state['visual_search_operation']
    assert operation['initial_reference_selection']['selected_urls'] == [candidates[1]['url']]
    assert candidates[0]['url'] not in operation['sources']
    assert len(state['visual_identity']['candidates']) == 2


@pytest.mark.asyncio
async def test_initial_google_quota_uses_existing_fenced_text_selector_before_real_vision(tmp_path):
    from types import SimpleNamespace
    from street_story.gemini import GeminiUnavailable
    svc, adapter, story, sessions = prepared(tmp_path)
    candidates = [{'candidate_id': f'wiki:{i}', 'name': f'Physical {i}', 'distance_m': 30+i,
        'url': f'https://ru.wikipedia.org/wiki/Physical_{i}',
        'reference_image_urls': [f'https://upload.wikimedia.org/ref-{i}.jpg']} for i in range(2)]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), story['id']))
    calls = []
    async def quota(*args):
        raise GeminiUnavailable(300, 'provider_quota')
    async def select(query, observed, snapshot):
        calls.append(observed)
        return {'sources': [observed[1]], 'source_selection': {'status': 'model_selected'}}
    async def references(batch, limit, *, story_id, evidence):
        assert [c['candidate_id'] for c in batch] == ['wiki:1']
        evidence.append(reference_receipt(batch[0]))
        return [('wiki:1', 'image/jpeg', b'fixture')]
    svc.providers.gemini.select_identity_sources = quota
    svc.providers.research = SimpleNamespace(select_identity_sources=select)
    svc._candidate_reference_images = references
    reply = await adapter._compare_place_images(sessions(), {})
    assert calls and reply['references'][0]['candidate_id'] == 'wiki:1'
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'
