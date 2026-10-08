import pytest
from street_story.errors import RetryableProviderError
from street_story.service import canonical
from test_independent_article_priority import gallery
from test_article_acquisition_priority import patch_reader, page


def insert_receipt(svc, story, receipt):
    frozen = canonical(receipt)
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
                   ('refused-unit', 'original-unit', story['id'], 'vision', frozen, 1, 1))
    return frozen


def unsent_receipt():
    return {'phase': 'created', 'binding': {'visual_scope': True, 'generation': 0},
            'route_failure': {'code': 'RESOURCE_TOKEN_BUDGET', 'retry_at': 10**12}}


@pytest.mark.asyncio
@pytest.mark.parametrize('planned_query', [False, True])
async def test_created_resource_refusal_does_not_starve_other_article_or_query(tmp_path, monkeypatch, planned_query):
    svc, adapter, story, session = gallery(tmp_path, 2)
    frozen = insert_receipt(svc, story, unsent_receipt())
    state = session.state['visual_comparison']
    useful = 'https://editor.example/news/actual-building'
    if planned_query:
        state.update(sources={}, planned_queries=['Actual map street building'], units_since_planned_query=2)
        async def find(session, args):
            assert args['query'] == 'Actual map street building'
            return {'status': 'completed', 'sources': [page(useful)['source']]}
        monkeypatch.setattr(adapter, '_find_place_articles', find)
    else:
        state['sources'] = {useful: page(useful)}
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1)
    assert acquired == [useful]
    assert reply['references'][0]['candidate_id'] == 'web:actual-article'
    assert len(state['queue']) == 40  # Gallery tail stays available.
    if planned_query:
        assert state['searches']['Actual map street building']['status'] == 'completed'
    with svc.store.connection() as db:
        assert db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', ('refused-unit',)).fetchone()[0] == frozen
    assert not svc.story(story['id'])['visual_identity'].get('visual_reference_verified')


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', [
    {'provider_send_state': 'possibly_sent'}, {'possibly_sent': True},
    {'message_id': 'original-message'}, {'turn_id': 'original-turn'},
    {'binding': {'visual_scope': True, 'generation': 0, 'message_id': 'original-message'}},
    {'binding': {'visual_scope': True, 'generation': 0, 'turn_id': 'original-turn'}},
    {'phase': 'submitted'}, {'phase': 'unknown'},
])
async def test_send_marked_created_or_unknown_remains_fenced(tmp_path, monkeypatch, marker):
    svc, adapter, story, session = gallery(tmp_path, 2)
    receipt = unsent_receipt()
    receipt.update(marker)
    frozen = insert_receipt(svc, story, receipt)
    queue = list(session.state['visual_comparison']['queue'])
    async def forbidden(*args, **kwargs):
        pytest.fail('Original possibly sent operation must not acquire/search/send a new unit')
    monkeypatch.setattr(adapter, '_find_place_articles', forbidden)
    from street_story import article_media
    monkeypatch.setattr(article_media, 'article_candidates', forbidden)
    svc._candidate_reference_images = forbidden
    with pytest.raises(RetryableProviderError, match='research_visual_outcome_unknown'):
        await adapter._compare_place_images(session, {})
    assert session.state['visual_comparison']['queue'] == queue
    with svc.store.connection() as db:
        assert db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', ('refused-unit',)).fetchone()[0] == frozen
