import json

import pytest

from street_story.service import canonical
from test_visual_search_continuation import prepared
from visual_queue_fixture import reference_receipt


@pytest.mark.asyncio
async def test_ready_nearby_wikipedia_does_not_wait_for_selector_and_retains_reserve(tmp_path):
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
        raise AssertionError('Optional selector must not precede ready SOURCE/REF.')

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
    assert selections == [] and images == ['wiki:0']
    assert reply['references'][0]['candidate_id'] == 'wiki:0'
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'
    with svc.store.connection() as db:
        state = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
    operation = state['visual_search_operation']
    assert {c['candidate_id'] for c in operation['queue']} == {'wiki:1'}
    assert {c['url'] for c in candidates} <= set(operation['sources'])
    assert len(state['visual_identity']['candidates']) == 2


@pytest.mark.asyncio
async def test_ready_ref_does_not_depend_on_google_or_text_selector_availability(tmp_path):
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
        assert [c['candidate_id'] for c in batch] == ['wiki:0']
        evidence.append(reference_receipt(batch[0]))
        return [('wiki:0', 'image/jpeg', b'fixture')]
    svc.providers.gemini.select_identity_sources = quota
    svc.providers.research = SimpleNamespace(select_identity_sources=select)
    svc._candidate_reference_images = references
    reply = await adapter._compare_place_images(sessions(), {})
    assert not calls and reply['references'][0]['candidate_id'] == 'wiki:0'
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'


@pytest.mark.asyncio
async def test_late_distance_sort_preserves_active_physical_choice_and_reserve(tmp_path):
    svc, adapter, story, sessions = prepared(tmp_path)
    candidates = [{'candidate_id': f'wiki:{i}', 'name': f'Body {i}', 'distance_m': metres,
        'url': f'https://ru.wikipedia.org/wiki/Body_{i}',
        'reference_image_urls': [f'https://upload.wikimedia.org/body-{i}.jpg']}
        for i, metres in enumerate((5, 500))]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': candidates,
                'research_priority': {'active_candidate_ids': ['wiki:1'], 'reserve_candidate_ids': ['wiki:0']}}}), story['id']))
    async def references(batch, limit, *, story_id, evidence):
        evidence.append(reference_receipt(batch[0]))
        return [(batch[0]['candidate_id'], 'image/jpeg', b'fixture')]
    svc._candidate_reference_images = references
    session = sessions()
    reply = await adapter._compare_place_images(session, {})
    assert reply['references'][0]['candidate_id'] == 'wiki:1'
    assert [c['candidate_id'] for c in session.state['visual_comparison']['queue']] == ['wiki:0']
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'
