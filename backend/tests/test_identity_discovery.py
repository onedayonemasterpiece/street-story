from types import SimpleNamespace

import pytest

from street_story import identity_discovery as discovery
from street_story.identity_lifecycle import visual_match


def test_query_contract_is_bounded_and_html_is_not_executable():
    queries, commons = discovery.queries_from({'wikipedia_queries': [
        '<b>Tower</b>', 'Tower', 'Other tower', 'Ignored'], 'commons_query': 'x' * 500})
    assert queries == ['Tower', 'Other tower']
    assert len(commons) == 180
    assert discovery.queries_from({'wikipedia_queries': 'not-a-list'}) == ([], '')


def test_localized_file_namespace_and_safe_image_hosts():
    assert discovery.file_title('Файл:Tower.jpg') == 'File:Tower.jpg'
    assert discovery.file_title('File:Tower.jpg') == 'File:Tower.jpg'
    assert discovery.image_urls({'original': {'source': 'https://evil.invalid/ref.jpg'}}) == []
    assert discovery.image_urls({'imageinfo': [{'url': 'https://upload.wikimedia.org/tower.jpg'}]}) == [
        'https://upload.wikimedia.org/tower.jpg']


@pytest.mark.asyncio
async def test_discovery_uses_fetched_records_and_respects_rejections(monkeypatch):
    calls = []
    async def api(service, client, endpoint, params):
        calls.append((endpoint, params))
        if endpoint == discovery.WIKI:
            return [{'pageid': 1, 'title': 'Named tower', 'index': 1, 'extract': 'A documented tower',
                     'images': [{'title': 'Файл:Tower.jpg'}]}]
        assert params['titles'] == 'File:Tower.jpg'
        return [{'pageid': 2, 'title': 'File:Tower.jpg', 'imageinfo': [
            {'url': 'https://upload.wikimedia.org/tower.jpg'}]}]
    monkeypatch.setattr(discovery, 'api', api)
    candidates = await discovery.retrieve(SimpleNamespace(), ['Tower'], '', set())
    assert len(candidates) == 1
    assert candidates[0]['candidate_id'] == 'wiki:1'
    assert candidates[0]['name'] == 'Named tower'
    assert candidates[0]['reference_image_urls'] == ['https://upload.wikimedia.org/tower.jpg']
    assert await discovery.retrieve(SimpleNamespace(), ['Tower'], '', {'wiki:1'}) == []
    assert {endpoint for endpoint, _ in calls} == {discovery.WIKI, discovery.COMMONS}


def test_discovery_does_not_turn_a_search_hit_or_model_memory_into_proof():
    candidates = [{'candidate_id': 'wiki:1', 'reference_image_urls': ['https://upload.wikimedia.org/ref.jpg']}]
    raw = {'status': 'match', 'candidate_id': 'wiki:1', 'confidence': 1.0,
           'observations': ['A named tower'], 'alternative_candidate_ids': []}
    assert not visual_match(raw, candidates)
    assert visual_match({**raw, '_references_sent': ['wiki:1']}, candidates)


@pytest.mark.asyncio
async def test_test_double_without_live_provider_does_not_start_external_discovery():
    assert await discovery.recover(SimpleNamespace(providers=SimpleNamespace(gemini=object())), {}, '', [], set()) is None
