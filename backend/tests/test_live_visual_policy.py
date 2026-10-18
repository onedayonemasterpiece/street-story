from street_story.live import SYSTEM_INSTRUCTION


def test_visual_description_does_not_force_place_resolution():
    assert 'Для новой темы сначала вызови resolve_place' not in SYSTEM_INSTRUCTION
    assert 'что видно/что ты видишь на фото' in SYSTEM_INSTRUCTION
    assert 'не вызывай resolve_place/search_web' in SYSTEM_INSTRUCTION
    assert 'однословный или явно обрывочный ввод' in SYSTEM_INSTRUCTION
