import pytest

from street_story.destination_names import resolve_destination
from street_story.service import ConflictError

DESTINATIONS = [
    {'alias': 'street_story_e2e_20260928_tg', 'label': 'Тестовый чат', 'test': True},
    {'alias': 'lovekenig_tg', 'label': 'Полюбить Калининград', 'test': False},
]


@pytest.mark.parametrize('spoken, alias', [
    ('street_story_e2e_20260928_tg', 'street_story_e2e_20260928_tg'),
    ('Тестовый чат', 'street_story_e2e_20260928_tg'),
    ('ну тестовая группа', 'street_story_e2e_20260928_tg'),
    ('тестовый канал', 'street_story_e2e_20260928_tg'),
    ('Полюбить Калининград', 'lovekenig_tg'),
    ('канал Полюбить Калининград', 'lovekenig_tg'),
    ('lovekenig_tg', 'lovekenig_tg'),
])
def test_owner_names_resolve_to_aliases(spoken, alias):
    # Owner test 19.10.2026: Mira demanded a system alias and publishing stalled.
    assert resolve_destination(spoken, DESTINATIONS) == alias


def test_unknown_name_lists_the_choices():
    with pytest.raises(ConflictError) as error:
        resolve_destination('Street Story 여의도점', DESTINATIONS)
    assert '«Тестовый чат»' in str(error.value) and '«Полюбить Калининград»' in str(error.value)
