"""The model chooses a scope; one literal lookup supplies text without images."""
import hashlib
from types import SimpleNamespace

import pytest

from street_story.identity_architectural_context import acquire_regional_text
from street_story import prussia39


def inputs():
    entrance = {'candidate_id': 'osm:node:8', 'map_address': {'city': 'Город',
        'street': 'Тестовая улица', 'house_number': '22А'}, 'map_object': {'tags': {'entrance': 'yes'}}}
    building = {'candidate_id': 'osm:way:7', 'map_coordinates': {'latitude': 54.7, 'longitude': 20.5},
        'map_object': {'tags': {'building': 'yes'}, 'building_entrances': {
            'proof': 'osm_closed_way_node_membership', 'candidate_ids': ['osm:node:8']}}}
    story = {'id': 'fixture', 'latitude': 54.7, 'longitude': 20.5,
        '_identity_observed_candidates': [building, entrance]}
    request = {'route': 'address', 'candidate_ids': ['osm:way:7'], 'address_entry_id': 'osm:node:8',
        'reason': 'A literal description can distinguish the cropped facade.'}
    return story, building, request


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'completed_empty', 'parse_failed', 'transport_failed', 'partial'])
async def test_exact_received_entrance_lookup_and_truthful_body_admission(monkeypatch, outcome):
    story, building, request = inputs()
    calls = []
    text = 'Фасад имеет центральный эркер и полукруглое завершение.'
    class Adapter:
        def __init__(self, *args): pass
        async def address_search(self, city, address):
            calls.append(('address', city, address))
            return {'status': 'completed' if outcome == 'partial' else outcome,
                'inventory_complete': outcome != 'partial',
                'results': [{'canonical_url': 'https://www.prussia39.ru/sight/index.php?sid=7'}]}
        async def article(self, url):
            calls.append(('article', url))
            return {'status': 'completed', 'text': text, 'article_id': 'prussia39:sid:7',
                'canonical_url': url, 'raw_content_sha256': hashlib.sha256(text.encode('cp1251')).hexdigest(),
                'raw_body_sha256_verified': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, lookup = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert calls[0] == ('address', 'Город', 'Тестовая улица, 22А')
    assert len(calls) == (2 if outcome == 'completed' else 1)
    assert len(articles) == (1 if outcome == 'completed' else 0)
    if articles:
        assert articles[0]['text'] == text and articles[0]['lookup_candidate_ids'] == ['osm:way:7']
        assert articles[0]['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert lookup['status'] == ('completed' if outcome == 'partial' else outcome)


@pytest.mark.asyncio
async def test_neighbor_entrance_is_not_used_for_target_query():
    story, building, request = inputs()
    building['map_object']['building_entrances']['candidate_ids'] = ['osm:node:9']
    articles, lookup = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert not articles and lookup['status'] == 'not_sent'
    assert lookup['reason'] == 'address_entry_not_bound_to_nominated_footprint'
