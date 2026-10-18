import pytest

from street_story import article_media
from street_story.live_visual_comparison import _article_acquisition_rank
from test_independent_article_priority import gallery
from visual_queue_fixture import reference_receipt


@pytest.mark.parametrize(('source', 'rank'), [
    ({'url': 'https://editor.example/news/society/item/123'}, 0),
    ({'url': 'https://photos.example/story/a-facade'}, 0),
    ({'url': 'https://other.example/detail/42', 'kind': 'photo_gallery'}, 0),
    ({'url': 'https://maps.example/geo/123'}, 2),
    ({'url': 'https://maps.example/firm/123'}, 2),
    ({'url': 'https://other.example/streets/22', 'title': 'Улица на карте с номерами домов'}, 2),
    ({'url': 'https://other.example/'}, 2),
    ({'url': 'https://other.example/index.php'}, 2),
    ({'url': 'https://other.example/index.php?sid=123'}, 1),
    ({'url': 'https://other.example/unknown-detail'}, 1),
])
def test_reader_role_priority_uses_metadata_and_path_without_host_or_house_rules(source, rank):
    assert _article_acquisition_rank(source) == rank


def patch_reader(svc, monkeypatch, acquired, useful_url):
    async def articles(service, snapshot, sources, excluded, *, receipts):
        url = sources[0]['url']
        acquired.append(url)
        receipts.append({'status': 'completed'})
        if url != useful_url:
            return []
        return [{'candidate_id': 'web:actual-article', 'name': 'Article facade', 'url': url,
                 'reference_image_urls': ['https://images.example/actual-facade.jpg'],
                 'discovery': 'web_article_media', 'identity_eligible': False}]
    async def images(batch, limit, *, story_id, evidence):
        candidate = batch[0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'], 'image/jpeg', candidate['reference_image_urls'][0])]
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    svc._candidate_reference_images = images


def page(url, *, attempts=0, status='pending', **source_fields):
    return {'source': {'url': url, 'discovery_provider': 'public', **source_fields},
            'attempts': attempts, 'status': status}


@pytest.mark.asyncio
async def test_empty_ready_queue_reads_later_article_before_inserted_maps_and_homepage(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    state = session.state['visual_comparison']
    state['queue'] = []
    nav = ['https://maps.example/geo/42', 'https://directory.example/firm/51', 'https://archive.example/']
    useful = 'https://editor.example/news/society/item/123'
    state['sources'] = {url: page(url) for url in [*nav, useful]}
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1, search_budget=0)
    assert acquired == [useful]
    assert reply['references'][0]['candidate_id'] == 'web:actual-article'
    assert all(state['sources'][url]['status'] == 'pending' for url in nav)
    assert all(state['sources'][url]['attempts'] == 0 for url in nav)
    assert not svc.story(story['id'])['visual_identity'].get('visual_reference_verified')


@pytest.mark.asyncio
async def test_planned_query_first_reader_uses_same_rank_and_preserves_all_results(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    state = session.state['visual_comparison']
    state.update(sources={}, planned_queries=['Actual map street building'], units_since_planned_query=2)
    nav = 'https://maps.example/geo/42'
    useful = 'https://editor.example/news/society/item/123'
    async def find(session, args):
        assert args['query'] == 'Actual map street building'
        return {'status': 'completed', 'sources': [page(nav)['source'], page(useful)['source']]}
    monkeypatch.setattr(adapter, '_find_place_articles', find)
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1)
    assert acquired == [useful]
    assert reply['references'][0]['candidate_id'] == 'web:actual-article'
    assert state['sources'][nav]['status'] == 'pending'
    assert len(state['queue']) == 40
    assert state['searches']['Actual map street building']['status'] == 'completed'


@pytest.mark.asyncio
async def test_unread_navigation_gets_turn_before_retrying_partial_article(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    state = session.state['visual_comparison']
    useful = 'https://editor.example/news/society/item/123'
    nav = 'https://maps.example/geo/42'
    state['sources'] = {useful: page(useful, attempts=1, status='partial'), nav: page(nav)}
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1, search_budget=0)
    assert acquired == [nav]
    assert state['sources'][useful]['attempts'] == 1
    assert state['sources'][useful]['status'] == 'partial'
    assert state['sources'][nav]['status'] == 'completed'
    assert reply['references'][0]['candidate_id'] == 'wiki:0'


@pytest.mark.asyncio
async def test_unread_selected_page_reaches_vision_before_new_planned_search(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    state = session.state['visual_comparison']
    useful = 'https://photos.example/category/building'
    state.update(sources={useful: page(useful)},
                 planned_queries=['Alternative street view'], units_since_planned_query=2)
    async def forbidden(*args, **kwargs):
        pytest.fail('An unread selected page must not wait for another search provider')
    monkeypatch.setattr(adapter, '_find_place_articles', forbidden)
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1)
    assert acquired == [useful]
    assert reply['references'][0]['candidate_id'] == 'web:actual-article'
    assert len(state['queue']) == 40
    assert 'Alternative street view' not in state['searches']


@pytest.mark.asyncio
async def test_recent_reader_turn_allows_feedback_query_without_draining_all_unread_pages(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    state = session.state['visual_comparison']
    state.update(sources={'https://maps.example/geo/42': page('https://maps.example/geo/42')},
                 units_since_acquisition=0, planned_queries=['Alternative street view'],
                 units_since_planned_query=2)
    useful = 'https://photos.example/new-facade'
    async def find(session, args):
        assert args['query'] == 'Alternative street view'
        return {'status': 'completed', 'sources': [page(useful)['source']]}
    monkeypatch.setattr(adapter, '_find_place_articles', find)
    acquired = []
    patch_reader(svc, monkeypatch, acquired, useful)
    reply = await adapter._compare_place_images(session, {}, page_budget=1)
    assert acquired == [useful]
    assert reply['references'][0]['candidate_id'] == 'web:actual-article'
    assert state['sources']['https://maps.example/geo/42']['status'] == 'pending'
