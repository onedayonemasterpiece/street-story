import json
from types import SimpleNamespace
import pytest
from street_story import prussia39, identity_architectural_context
from street_story.identity_architectural_context import acquire_regional_text
from street_story.identity_source_selection import observed_address_context

@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_id,city", [(17, "Первый город"), (813, "Второй город")])
@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("fresh", [False, True])
async def test_cold_fresh_snapshot_restores_only_own_literal_city(monkeypatch, candidate_id, city, reverse_order, fresh):
    cid = f'osm:way:{candidate_id}'
    raw = [{'type': 'way', 'id': candidate_id, 'tags': {'building': 'yes', 'addr:city': city,
        'addr:street': 'Тестовая улица', 'addr:housenumber': '22А'}},
        {'type': 'way', 'id': candidate_id + 1, 'tags': {'building': 'yes', 'addr:city': 'Чужой город',
            'addr:street': 'Другая улица', 'addr:housenumber': '99'}}]
    if reverse_order:
        raw.reverse()
    candidate = {'candidate_id': cid, 'map_coordinates': {'latitude': 54.7, 'longitude': 20.5},
        'map_object': {'tags': {'building': 'yes'}}, 'map_address': {
            'street': 'Тестовая улица', 'house_number': '22А'}}
    story = {'id': 'cold-owner', 'research_json': json.dumps({}),
        '_identity_map_snapshot': {'observed_pool': raw}, '_identity_observed_candidates': [candidate]}
    if not fresh:
        story['research_json'] = json.dumps({'osm': story.pop('_identity_map_snapshot')})
    frozen = json.dumps(story, ensure_ascii=False, sort_keys=True)
    ctx = observed_address_context(story, [candidate])
    own = next(row for row in ctx['address_anchors'] if row['mapped_entry_id'] == cid)
    assert own['address'].get('city') == city
    calls = []
    class Adapter:
        def __init__(self, *args): pass
        async def address_search(self, locality, address):
            calls.append((locality, address))
            return {'status': 'completed', 'results': [], 'inventory_complete': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    monkeypatch.setattr(identity_architectural_context, 'record_identity_event', lambda *a, **kw: None)
    articles, receipt = await acquire_regional_text(SimpleNamespace(store=SimpleNamespace()), story, [candidate], {
        'route': 'address', 'candidate_ids': [cid], 'reason': 'Read this received literal address.'})
    assert calls == [(city, 'Тестовая улица, 22А')]
    assert not articles and receipt['query_scope']['target_identity_established'] is False
    assert json.dumps(story, ensure_ascii=False, sort_keys=True) == frozen


def test_foreign_snapshot_city_does_not_fill_unaddressed_body():
    candidate = {'candidate_id': 'osm:way:57', 'map_address': {
        'street': 'Тестовая улица', 'house_number': '22А'}}
    story = {'research_json': '{}', '_identity_map_snapshot': {'observed_pool': [
        {'type': 'way', 'id': 58, 'tags': {'addr:city': 'Чужой город',
            'addr:street': 'Тестовая улица', 'addr:housenumber': '23'}}]}}
    own = next(row for row in observed_address_context(story, [candidate])['address_anchors']
        if row['mapped_entry_id'] == 'osm:way:57')
    assert not own['address'].get('city')
