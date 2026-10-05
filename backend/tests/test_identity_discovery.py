from types import SimpleNamespace

import pytest

from street_story import identity_discovery as discovery
from street_story.identity_lifecycle import visual_match


def test_query_contract_is_bounded_and_html_is_not_executable():
    entity, queries, visual, commons = discovery.queries_from({'entity_name': '<b>Named Tower</b>',
        'wikipedia_queries': ['<b>Tower</b>', 'Tower', 'Other tower', 'Ignored'],
        'visual_query': 'red brick clock tower', 'commons_query': 'x' * 500})
    assert entity == 'Named Tower'
    assert queries == ['Tower', 'Other tower']
    assert visual == 'red brick clock tower'
    assert len(commons) == 180
    assert discovery.queries_from({'wikipedia_queries': 'not-a-list'}) == ('', [], '', '')


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
            assert 'pageprops' in params['prop'].split('|')
            return [{'pageid': 1, 'title': 'Named tower', 'index': 1, 'extract': 'A documented tower',
                     'pageprops': {'wikibase_item': 'Q123'},
                     'images': [{'title': 'Файл:Tower.jpg'}]}]
        if params.get('generator') == 'search' and params.get('gsrnamespace') == 14:
            return []
        assert params['titles'] == 'File:Tower.jpg'
        return [{'pageid': 2, 'title': 'File:Tower.jpg', 'imageinfo': [
            {'url': 'https://upload.wikimedia.org/tower.jpg'}]}]
    monkeypatch.setattr(discovery, 'api', api)
    candidates = await discovery.retrieve(SimpleNamespace(), ['Tower'], '', set())
    assert len(candidates) == 1
    assert candidates[0]['candidate_id'] == 'wiki:1'
    assert candidates[0]['name'] == 'Named tower'
    assert candidates[0]['wikidata'] == 'Q123'
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

def test_same_physical_object_aliases_are_clustered_by_reference_or_specific_category():
    shared = 'https://upload.wikimedia.org/wikipedia/commons/a/ab/shared.jpg'
    candidates = [
        {'candidate_id':'wiki:1','name':'Кирха памяти королевы Луизы',
         'url':'https://ru.wikipedia.org/wiki/a','reference_image_urls':[shared],
         'entity_keys':['category:queen church kaliningrad'],'discovery':'wikipedia_text_search'},
        {'candidate_id':'wiki:2','name':'Калининградский областной театр кукол',
         'url':'https://ru.wikipedia.org/wiki/b','reference_image_urls':[shared],
         'entity_keys':['category:queen church kaliningrad'],'discovery':'wikipedia_text_search'},
    ]
    merged = discovery.merge_candidates(candidates, 'Кирха памяти королевы Луизы')
    assert len(merged) == 1
    assert merged[0]['candidate_id'] == 'wiki:1'
    assert merged[0]['multi_view'] is False
    assert set(merged[0]['source_urls']) == {'https://ru.wikipedia.org/wiki/a','https://ru.wikipedia.org/wiki/b'}


def test_commons_views_of_one_object_become_one_multiview_candidate():
    candidates = [
        {'candidate_id':'commons:1','name':'Water Tower p1.jpg',
         'url':'https://commons.wikimedia.org/wiki/File:1','reference_image_urls':['https://upload.wikimedia.org/1.jpg'],
         'entity_keys':['heritage:3930625000'],'discovery':'commons_text_search'},
        {'candidate_id':'commons:2','name':'Water Tower p2.jpg',
         'url':'https://commons.wikimedia.org/wiki/File:2','reference_image_urls':['https://upload.wikimedia.org/2.jpg'],
         'entity_keys':['heritage:3930625000'],'discovery':'commons_text_search'},
    ]
    merged = discovery.merge_candidates(candidates, 'Водонапорная башня Зеленоградска')
    assert len(merged) == 1
    assert merged[0]['name'] in {'Water Tower p1.jpg', 'Water Tower p2.jpg'}
    assert merged[0]['multi_view'] is True
    assert merged[0]['reference_image_urls'] == ['https://upload.wikimedia.org/1.jpg','https://upload.wikimedia.org/2.jpg']


def test_category_member_file_is_clustered_with_its_physical_object():
    member = 'https://commons.wikimedia.org/wiki/File:Holy_Family.jpg'
    candidates = [
        {'candidate_id':'commonscat:1','name':'Church of the Holy Family (Kaliningrad)',
         'url':'https://commons.wikimedia.org/wiki/Category:Church_of_the_Holy_Family_(Kaliningrad)',
         'source_urls':['https://commons.wikimedia.org/wiki/Category:Church_of_the_Holy_Family_(Kaliningrad)', member],
         'reference_image_urls':['https://upload.wikimedia.org/category-view.jpg'],
         'discovery':'commons_category'},
        {'candidate_id':'commons:2','name':'Holy Family.jpg','url':member,
         'reference_image_urls':['https://upload.wikimedia.org/member-view.jpg'],
         'discovery':'commons_text_search'},
        {'candidate_id':'wiki:3','name':'Кирха Святого Семейства (Калининград)',
         'url':'https://ru.wikipedia.org/wiki/Holy_Family',
         'reference_image_urls':['https://upload.wikimedia.org/member-view.jpg'],
         'discovery':'wikipedia_text_search'},
    ]
    merged = discovery.merge_candidates(candidates, 'Кирха Святого Семейства')
    assert len(merged) == 1
    assert merged[0]['candidate_id'] == 'wiki:3'
    assert merged[0]['name'] == 'Кирха Святого Семейства (Калининград)'
    assert merged[0]['multi_view'] is True



@pytest.mark.asyncio
async def test_web_search_titles_expand_visual_discovery_without_becoming_proof():
    async def search_web(query, context):
        assert 'red brick clock tower' in query
        assert context['purpose'] == 'identity_candidate_discovery'
        return SimpleNamespace(grounding_sources=[
            {'type':'web','title':'Кирха Святого Семейства — официальный сайт','url':'https://example.com/1'},
            {'type':'web','title':'Кирха Святого Семейства — официальный сайт','url':'https://example.com/2'},
            {'type':'web','title':'https://example.com/no-title','url':'https://example.com/3'},
        ])
    service = SimpleNamespace(providers=SimpleNamespace(
        gemini=SimpleNamespace(search_web=search_web)))
    hints = await discovery.web_search_hints(service, 'red brick clock tower')
    assert hints == ['Кирха Святого Семейства — официальный сайт']



def test_wikipedia_current_use_page_clusters_with_explicit_building_alias():
    candidates = [
        {'candidate_id':'wiki:church','name':'Кирха Святого Семейства (Калининград)',
         'url':'https://ru.wikipedia.org/wiki/church',
         'extract':'Католический храм Святого Семейства. Сейчас концертный зал.',
         'reference_image_urls':['https://upload.wikimedia.org/church.jpg'],
         'discovery':'wikipedia_text_search'},
        {'candidate_id':'wiki:hall','name':'Калининградская областная филармония',
         'url':'https://ru.wikipedia.org/wiki/hall',
         'extract':'Филармония расположена в здании бывшей кирхи Святого Семейства в Калининграде.',
         'reference_image_urls':['https://upload.wikimedia.org/hall.jpg'],
         'discovery':'wikipedia_text_search'},
    ]
    merged = discovery.merge_candidates(candidates, 'Кирха Святого Семейства')
    assert len(merged) == 1
    assert merged[0]['candidate_id'] == 'wiki:church'
    assert set(merged[0]['source_urls']) == {
        'https://ru.wikipedia.org/wiki/church',
        'https://ru.wikipedia.org/wiki/hall',
    }


def test_distinct_explicit_entities_cannot_merge_through_shared_commons_bridge():
    shared = 'https://upload.wikimedia.org/shared.jpg'
    candidates = [
        {'candidate_id': 'commons:1', 'name': 'Shared view', 'reference_image_urls': [shared]},
        {'candidate_id': 'wiki:2', 'name': 'Physical building', 'wikidata': 'Q123',
         'reference_image_urls': [shared], 'discovery': 'wikipedia_text_search'},
        {'candidate_id': 'wiki:3', 'name': 'Institution using the building', 'wikidata': 'Q456',
         'reference_image_urls': [shared], 'discovery': 'wikipedia_text_search'},
    ]
    for ordering in (candidates, list(reversed(candidates))):
        merged = discovery.merge_candidates(ordering, 'Physical building')
        assert len(merged) == 2
        assert {item.get('wikidata') for item in merged} == {'Q123', 'Q456'}
        assert all(not ({'wiki:2', 'wiki:3'} <= {item['candidate_id'], *item.get('alias_candidate_ids', [])})
                   for item in merged)


def test_known_entity_metadata_survives_selection_of_a_commons_representative():
    shared = 'https://upload.wikimedia.org/shared.jpg'
    merged = discovery.merge_candidates([
        {'candidate_id': 'commons:1', 'name': 'Exact target name', 'reference_image_urls': [shared]},
        {'candidate_id': 'wiki:2', 'name': 'Alternate documented name', 'wikidata': 'Q123',
         'reference_image_urls': [shared], 'discovery': 'wikipedia_text_search'},
    ], 'Exact target name')
    assert len(merged) == 1
    assert merged[0]['candidate_id'] == 'commons:1'
    assert merged[0]['wikidata'] == 'Q123'
