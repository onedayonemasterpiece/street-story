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


def test_concept_receipt_exposes_text_revision_separately_and_conflict_guard_remains(tmp_path):
    from test_live_editor import make_service
    from street_story.service import ConflictError
    service, adapter, session, _ = make_service(tmp_path)
    with service.store.tx() as db:
        adapter._editor_row(db, session.resource_id)
        db.execute('UPDATE stories SET revision=95 WHERE id=?', (session.resource_id,))
        db.execute('UPDATE live_editor_state SET text_revision=13 WHERE story_id=?', (session.resource_id,))
    receipt = adapter._set_concept(session.resource_id, 'new-angle', {'concept': 'Another angle'})
    assert receipt['text_revision'] == 13
    assert receipt['story']['revision'] == 96
    with pytest.raises(ConflictError, match='Expected text revision 96'):
        adapter._edit_text(session.resource_id, 'wrong-version', {
            'expected_text_revision': 96, 'new_text': 'Must not be saved.', 'change_summary': 'wrong version',
        })
    with service.store.connection() as db:
        assert db.execute('SELECT text_revision FROM live_editor_state WHERE story_id=?',
                          (session.resource_id,)).fetchone()[0] == 13
