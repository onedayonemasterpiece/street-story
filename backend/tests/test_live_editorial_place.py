from copy import deepcopy

import pytest

from street_story.live import StreetStoryLiveAdapter


def context_story():
    return {
        'story': {
            'id': 'editorial-house', 'photo_sha256': 'original-upload', 'identity_generation': 4,
            'visual_identity': {'candidate_id': 'osm:way:42'},
            'place_selection': {
                'candidate_id': 'osm:way:42', 'photo_sha256': 'original-upload', 'generation': 4,
                'candidate': {'search_anchor': {
                    'candidate_id': 'osm:way:42', 'city': 'Selected city', 'address': 'Selected street 8',
                    'city_provenance': 'osm.selected.addr_city', 'address_provenance': 'osm.tags',
                }},
            },
            'camera_point': {'address': 'Camera street 99', 'city': 'Other city'},
        },
        'editor': {},
    }


def test_editorial_context_exposes_confirmed_address_with_its_provenance(monkeypatch):
    monkeypatch.setattr('street_story.live.accepted_identity', lambda *args, **kwargs: True)
    state = context_story()
    original = deepcopy(state)
    result = StreetStoryLiveAdapter._compact_context(state)
    assert result['confirmed_place'] == {
        'candidate_id': 'osm:way:42', 'city': 'Selected city', 'address': 'Selected street 8',
        'city_provenance': 'osm.selected.addr_city', 'address_provenance': 'osm.tags',
    }
    assert StreetStoryLiveAdapter._capability_context(result, 'editor')['confirmed_place'] == result['confirmed_place']
    assert state == original


@pytest.mark.parametrize(('key', 'value'), [
    ('candidate_id', 'osm:way:other'), ('photo_sha256', 'other-upload'), ('generation', 3),
])
def test_editorial_context_never_imports_stale_selection_or_camera_address(monkeypatch, key, value):
    monkeypatch.setattr('street_story.live.accepted_identity', lambda *args, **kwargs: True)
    state = context_story()
    state['story']['place_selection'][key] = value
    assert StreetStoryLiveAdapter._compact_context(state)['confirmed_place'] is None


def test_editorial_context_does_not_promote_a_hypothesis_address(monkeypatch):
    monkeypatch.setattr('street_story.live.accepted_identity', lambda *args, **kwargs: False)
    assert StreetStoryLiveAdapter._compact_context(context_story())['confirmed_place'] is None


def test_editorial_context_missing_city_stays_missing(monkeypatch):
    monkeypatch.setattr('street_story.live.accepted_identity', lambda *args, **kwargs: True)
    state = context_story()
    del state['story']['place_selection']['candidate']['search_anchor']['city']
    assert 'city' not in StreetStoryLiveAdapter._compact_context(state)['confirmed_place']
