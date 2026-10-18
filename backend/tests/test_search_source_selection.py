import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from test_opencode_research import Harness
from street_story import article_media


def test_inventory_indices_preserve_exact_urls_order_and_provenance():
    from street_story.identity_source_selection import indexed_model_selection
    observed = [{'url': 'https://example.org/%D0%94%D0%BE%D0%BC?x=a%2Fb', 'search_call_id': 'one'},
                {'url': 'https://example.org/second', 'search_call_id': 'two'}]
    chosen, selection = indexed_model_selection(observed, {'summary': 'Useful pages', 'selected_sources': [
        {'source_index': 1, 'reason': 'Exterior view'}, {'source_index': 0, 'reason': 'Another view'},
        {'source_index': 1, 'reason': 'Duplicate'}]})
    assert [item['url'] for item in chosen] == [observed[1]['url'], observed[0]['url']]
    assert [item['search_call_id'] for item in chosen] == ['two', 'one']
    assert selection['status'] == 'model_selected'


@pytest.mark.parametrize('index', [-1, 1, '0', True])
def test_unobserved_or_malformed_inventory_index_never_promotes_a_page(index):
    from street_story.identity_source_selection import indexed_model_selection
    chosen, selection = indexed_model_selection([{'url': 'https://example.org/one'}], {
        'summary': 'Choice', 'selected_sources': [{'source_index': index, 'reason': 'Useful view'}]})
    assert chosen == [] and selection['status'] == 'selection_unavailable'


def test_empty_inventory_selection_remains_empty():
    from street_story.identity_source_selection import indexed_model_selection
    chosen, selection = indexed_model_selection([{'url': 'https://example.org/one'}], {
        'summary': 'Only irrelevant pages', 'selected_sources': []})
    assert chosen == [] and selection['status'] == 'model_selected'


@pytest.mark.asyncio
async def test_semantic_selection_retains_tool_provenance_and_rejects_invented_url():
    h = Harness()
    h.result = {'summary': 'Use the exterior view', 'selected_sources': [
        {'url': 'https://invented.example/facade', 'reason': 'Not observed'},
        {'url': 'https://example.org/gallery', 'reason': 'Current exterior'},
        {'url': 'https://example.org/gallery', 'reason': 'Duplicate choice'}]}
    response = await h.adapter().search_articles(json.dumps({'purpose': 'identity'}), {'request_id': 'r', 'purpose': 'identity'})
    assert len(response['sources']) == 1
    assert response['sources'][0]['search_call_id'] == 'call_actual'
    assert response['sources'][0]['source_selection_reason'] == 'Current exterior'
    assert response['receipt']['source_selection'] == {
        'status': 'model_selected', 'discovered_count': 1, 'selected_count': 1, 'unobserved_count': 1}
    assert h.checkpoints[-1][1]['sources'] == response['sources']


@pytest.mark.asyncio
async def test_irrelevant_results_are_retained_without_filling_visual_queue():
    h = Harness()
    h.result = {'summary': 'Only interiors found', 'selected_sources': []}
    response = await h.adapter().search_articles(json.dumps({'purpose': 'identity'}), {'request_id': 'r', 'purpose': 'identity'})
    assert response['sources'] == []
    assert response['receipt']['discovered_sources'][0]['url'] == 'https://example.org/gallery'
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_malformed_identity_summary_does_not_enqueue_all_search_hits():
    h = Harness()
    h.result = None
    response = await h.adapter().search_articles(json.dumps({'purpose': 'identity'}), {'request_id': 'r', 'purpose': 'identity'})
    assert response['sources'] == []
    assert response['receipt']['source_selection']['status'] == 'selection_unavailable'
    assert response['receipt']['discovered_sources']


@pytest.mark.asyncio
async def test_article_browser_reserve_serializes_across_independent_reads(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    visits = []

    @asynccontextmanager
    async def reserve(url):
        visits.append(url)
        entered.set()
        await release.wait()
        yield object()

    monkeypatch.setattr(article_media, '_article_browser', reserve)

    async def read(url):
        async with article_media.article_browser(url):
            pass

    first = asyncio.create_task(read('https://example.org/first'))
    await entered.wait()
    second = asyncio.create_task(read('https://example.org/second'))
    await asyncio.sleep(0)
    assert visits == ['https://example.org/first']
    release.set()
    await asyncio.gather(first, second)
    assert visits == ['https://example.org/first', 'https://example.org/second']
