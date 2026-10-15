from street_story.map_links import building_links, building_point, with_map_links
from street_story.publication_content import publication_content, visible_text


def ring(cx, cy, r, n=24):
    import math
    points = [{'lat': cy + r * math.sin(2 * math.pi * k / n), 'lon': cx + r * math.cos(2 * math.pi * k / n)} for k in range(n)]
    return {'closed': True, 'points': [*points, points[0]]}


def tower(center_provenance='osm.center'):
    # A ring-shaped tower: the OSM centre lies in the open courtyard, not in the building.
    return {'place_selection': {'mode': 'exact', 'candidate': {
        'map_coordinates': {'latitude': 54.7225, 'longitude': 20.5144, 'provenance': center_provenance},
        'map_geometry': {'rings': [ring(20.5144, 54.7225, 0.0003), ring(20.5144, 54.7225, 0.0002)]}}},
        'camera_point': {'latitude': 54.7219, 'longitude': 20.5140}}


def test_ring_building_link_point_lies_in_the_walls_not_the_courtyard_or_camera():
    lat, lon = building_point(tower()['place_selection']['candidate'])
    distance = ((lat - 54.7225) ** 2 + (lon - 20.5144) ** 2) ** .5
    assert 0.0002 < distance < 0.0003
    links = building_links(tower())
    assert set(links) == {'ЯндексКарты', '2ГИС'}
    assert f'{lon:.6f}%2C{lat:.6f}' in links['ЯндексКарты'] and links['2ГИС'].endswith(f'{lon:.6f}%2C{lat:.6f}')
    assert '20.514000' not in links['ЯндексКарты']


def test_footer_is_appended_once_and_replaces_typed_or_invented_links():
    research = tower()
    draft = 'Башня Врангеля.\n\nАдрес: улица Профессора Баранова, 2А, Калининград. #Калининград'
    saved = with_map_links(draft, research)
    assert saved.startswith(draft + '\n\n[ЯндексКарты](https://yandex.ru/maps/?whatshere%5Bpoint%5D=')
    assert saved.count('ЯндексКарты') == 1 and saved.count('2ГИС') == 1
    assert with_map_links(saved, research) == saved
    typed = draft + '\n\n[ЯндексКарты](https://yandex.ru/maps/invented) · [2ГИС](https://2gis.ru/invented)\n\nЕщё абзац.'
    assert with_map_links(typed, research) == with_map_links(draft + '\n\nЕщё абзац.', research)
    assert with_map_links(draft + '\n\nЯндексКарты · 2ГИС', research) == saved


def test_without_an_exact_building_typed_links_are_removed_and_plain_text_kept():
    rough = {'place_selection': {'mode': 'rough', 'candidate': {}}}
    draft = 'Текст поста. #Калининград'
    assert with_map_links(draft, rough) is draft
    assert with_map_links(draft + '\n\n[2ГИС](https://2gis.ru/invented)', rough) == draft
    assert with_map_links('', tower()) == ''


def test_footer_compiles_to_clickable_labels_without_raw_urls():
    saved = with_map_links('Текст. #Калининград', tower())
    runs = publication_content(saved)['paragraphs'][0]
    links = [run for run in runs if run['kind'] == 'link']
    assert [run['label'] for run in links] == ['ЯндексКарты', '2ГИС']
    assert visible_text(saved) == 'Текст. #Калининград\n\nЯндексКарты · 2ГИС'
