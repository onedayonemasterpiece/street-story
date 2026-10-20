from street_story.fact_agents import agent_context


def test_fact_text_never_names_its_source():
    # Owner, 20.10.2026: «Подпись к фотографии сообщает…», «В записи OpenStreetMap от…», «В карточке 2ГИС…» are
    # not facts about the house; every fact already keeps its sources and their dates separately.
    goal = agent_context({'candidate_id': 'osm:way:1'}, 'Исследуй дом.', 'run', 0)['editorial_goal']
    assert 'Never name the source' in goal and 'подпись к фотографии сообщает' in goal
    assert 'посетители пишут…», with the review year' not in goal
    assert 'dated by the record' not in goal
