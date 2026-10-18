from street_story.publication_content import publication_content


def test_named_links_preserve_label_target_and_surrounding_caption_exactly():
    caption = '🏠 Дом\n\nАдрес #Город\n[ЯндексКарты](https://maps.example/house?a=1&b=2) · [2ГИС](https://2gis.example/house)'
    assert publication_content(caption) == {'paragraphs': [[
        {'kind': 'text', 'text': '🏠 Дом\n\nАдрес #Город\n'},
        {'kind': 'link', 'label': 'ЯндексКарты', 'url': 'https://maps.example/house?a=1&b=2'},
        {'kind': 'text', 'text': ' · '},
        {'kind': 'link', 'label': '2ГИС', 'url': 'https://2gis.example/house'},
    ]]}


def test_plain_caption_and_unsupported_markup_are_not_rewritten():
    for caption in ('Обычный текст.', 'https://maps.example/house',
                    '[Карта](javascript:alert)', '[Карта](https://user:password@maps.example/house)',
                    '[Карта](https:///missing-host)', '[Карта](https://[invalid)',
                    '[Карта](https://maps.example/house'):
        assert publication_content(caption) == {'text': caption, 'format': 'plain'}


def test_rejected_link_between_valid_links_stays_literal():
    content = publication_content('[Первая](https://one.example) [Не ссылка](javascript:x) [Вторая](https://two.example) хвост')
    assert content['paragraphs'][0][2] == {'kind': 'link', 'label': 'Вторая', 'url': 'https://two.example'}
    assert content['paragraphs'][0][1] == {'kind': 'text', 'text': ' [Не ссылка](javascript:x) '}
    assert content['paragraphs'][0][-1] == {'kind': 'text', 'text': ' хвост'}
