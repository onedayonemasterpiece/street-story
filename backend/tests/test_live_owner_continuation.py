from copy import deepcopy
from types import SimpleNamespace

import pytest

from street_story.live import FUNCTIONS, StreetStoryLiveAdapter
from street_story.live_author_intent import begin_turn


@pytest.mark.parametrize('origin', ['text', 'audio'])
@pytest.mark.parametrize('long_request', [False, True])
def test_stage_handoff_preserves_exact_owner_request_over_a_broader_model_summary(origin, long_request):
    owner = ('Для аудитории любителей архитектуры выбери интересные пригодные факты. '
             + ('Учитывай мои предпочтения. ' * 100 if long_request else '')
             + 'Сначала сохрани только выбор; замысел, текст, изображение и публикацию не меняй.')
    adapter = object.__new__(StreetStoryLiveAdapter)
    adapter.service = SimpleNamespace(story=lambda _: {'identity_mode': 'manual'})
    adapter.initialize = lambda **_: {
        'configuration': {'functions': FUNCTIONS}, 'context': {'physical_identity_accepted': True},
    }
    session = SimpleNamespace(resource_id='same-story', actor=None, model='controlled',
                              capability='publication', state={})
    begin_turn(session, owner, origin=origin)
    before = deepcopy(session.state)
    call = {'name': 'continue_story', 'args': {'stage': 'editor', 'intent': 'Сделать городской пост и картинку'}}
    result = adapter.resolve_capability(session, call)
    assert result['capability'] == 'editor'
    assert result['context']['pending_owner_request'] == {'text': owner, 'origin': origin, 'serial': 1}
    assert len(result['continuation']) <= 1200
    if long_request:
        assert 'context.pending_owner_request.text' in result['continuation']
    else:
        assert result['continuation'] == owner
    assert session.state == before  # Resolving a transition grants/mutates nothing.


def test_a_later_owner_turn_replaces_the_previous_handoff_request():
    adapter = object.__new__(StreetStoryLiveAdapter)
    adapter.service = SimpleNamespace(story=lambda _: {'identity_mode': 'manual'})
    adapter.initialize = lambda **_: {
        'configuration': {'functions': FUNCTIONS}, 'context': {'physical_identity_accepted': True},
    }
    session = SimpleNamespace(resource_id='same-story', actor=None, model='controlled',
                              capability='publication', state={})
    begin_turn(session, 'Выбери факты; пока не меняй текст.', origin='text')
    begin_turn(session, 'Теперь исправь только текст, сохрани мой выбор.', origin='text')
    result = adapter.resolve_capability(session, {
        'name': 'continue_story', 'args': {'stage': 'editor', 'intent': 'Выбрать факты'},
    })
    assert result['context']['pending_owner_request']['serial'] == 2
    assert result['continuation'] == 'Теперь исправь только текст, сохрани мой выбор.'
