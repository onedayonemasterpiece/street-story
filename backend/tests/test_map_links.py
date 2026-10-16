from street_story.map_links import building_links, building_point, with_place_footer
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
    target = f'{lon:.6f}%2C{lat:.6f}'
    assert target in links['ЯндексКарты'] and links['2ГИС'] == f'https://2gis.ru/geo/{target}?m={target}%2F18'
    assert '20.514000' not in links['ЯндексКарты']


def test_footer_is_appended_once_and_replaces_typed_or_invented_links():
    research = tower()
    draft = 'Башня Врангеля.\n\nАдрес: улица Профессора Баранова, 2А, Калининград. #Калининград'
    saved = with_place_footer(draft, research)
    assert saved.startswith(draft + '\n\n[ЯндексКарты](https://yandex.ru/maps/?ll=')
    assert saved.count('ЯндексКарты') == 1 and saved.count('2ГИС') == 1
    assert with_place_footer(saved, research) == saved
    typed = draft + '\n\n[ЯндексКарты](https://yandex.ru/maps/invented) · [2ГИС](https://2gis.ru/invented)\n\nЕщё абзац.'
    assert with_place_footer(typed, research) == with_place_footer(draft + '\n\nЕщё абзац.', research)
    assert with_place_footer(draft + '\n\nЯндексКарты · 2ГИС', research) == saved


def test_without_an_exact_building_typed_links_are_removed_and_plain_text_kept():
    rough = {'place_selection': {'mode': 'rough', 'candidate': {}}}
    draft = 'Текст поста. #Калининград'
    assert with_place_footer(draft, rough) is draft
    assert with_place_footer(draft + '\n\n[2ГИС](https://2gis.ru/invented)', rough) == draft
    assert with_place_footer('', tower()) == ''


def test_footer_compiles_to_clickable_labels_without_raw_urls():
    saved = with_place_footer('Текст. #Калининград', tower())
    runs = publication_content(saved)['paragraphs'][0]
    links = [run for run in runs if run['kind'] == 'link']
    assert [run['label'] for run in links] == ['ЯндексКарты', '2ГИС']
    assert visible_text(saved) == 'Текст. #Калининград\n\nЯндексКарты · 2ГИС'


def test_missing_city_hashtag_is_added_before_links_and_never_duplicated():
    research = tower()
    research['place_selection']['candidate']['search_anchor'] = {'city': 'Калининград'}
    saved = with_place_footer('Башня Врангеля.\n\nУлица Профессора Баранова, 2А, Калининград.', research)
    assert visible_text(saved).endswith('Калининград.\n\n#Калининград\n\nЯндексКарты · 2ГИС')
    assert with_place_footer(saved, research) == saved
    written = with_place_footer('Башня. Калининград. #калининград', research)
    assert visible_text(written) == 'Башня. Калининград. #калининград\n\nЯндексКарты · 2ГИС'
    research['place_selection']['candidate']['search_anchor'] = {'city': 'Санкт-Петербург'}
    assert '#СанктПетербург' in with_place_footer('Дом.', research)


def test_selected_city_typo_takes_the_local_majority_spelling_but_not_another_city():
    from street_story.place_selection import snapshot_city
    def objects(city, count, prefix):
        return [{'candidate_id': f'{prefix}{n}', 'search_anchor': {'city': city}} for n in range(count)]
    snapshot = {'city_objects': objects('Калининград', 9, 'a') + objects('Калиининград', 2, 'b')}
    chosen = {'candidate_id': 'osm:relation:1', 'search_anchor': {'city': 'Калиининград'}}
    assert snapshot_city(snapshot, chosen)
    assert chosen['search_anchor'] == {'city': 'Калининград', 'city_provenance': 'osm.snapshot_majority_spelling',
                                       'city_selected_tag': 'Калиининград'}
    unnamed = {'candidate_id': 'osm:way:9', 'search_anchor': {}}  # no own city tag; nearest neighbour has the typo
    snapshot_city({'city_objects': [{**objects('Калиининград', 1, 'n')[0], 'search_anchor': {
        'city': 'Калиининград', 'city_provenance': 'osm.selected.addr_city'}}, *objects('Калининград', 9, 'd')]}, unnamed)
    assert unnamed['search_anchor']['city'] == 'Калининград'
    assert unnamed['search_anchor']['city_neighbour_tag'] == 'Калиининград'
    border = {'candidate_id': 'osm:way:2', 'search_anchor': {'city': 'Гурьевск'}}
    snapshot_city({'city_objects': objects('Калининград', 9, 'c')}, border)
    assert border['search_anchor'] == {'city': 'Гурьевск', 'city_provenance': 'osm.selected.addr_city'}


def test_surroundings_flag_a_tag_street_away_from_the_contour_and_list_nearby_addresses():
    # The contour's address tag names a street that is not beside it.
    from street_story.fact_agents import agent_context
    from street_story.place_selection import snapshot_surroundings
    def box(cid, lat, lon, street='', number=''):
        pts = [(lat, lon), (lat + .0002, lon), (lat + .0002, lon + .0002), (lat, lon + .0002), (lat, lon)]
        return {'candidate_id': cid, 'search_anchor': {'city': 'Калининград', 'street': street, 'house_number': number},
                'map_address': {'street': street, 'house_number': number} if street else {},
                'map_geometry': {'rings': [{'role': 'outer', 'closed': True, 'points': [{'lat': a, 'lon': o} for a, o in pts]}]}}
    def street(name, lat):
        return {'type': 'Feature', 'properties': {'name': name},
                'geometry': {'type': 'MultiLineString', 'coordinates': [[[20.5060, lat], [20.5090, lat]]]}}
    chosen = box('osm:way:1', 54.7149, 20.5073, 'Университетская улица', '3')
    snapshot = {'snapshot_id': 'map_1', 'candidates': [chosen,
        box('osm:way:2', 54.7149, 20.5078, 'улица Профессора Севастьянова', '3/5к2'),
        box('osm:way:3', 54.7200, 20.5073, 'Далёкая улица', '1'), box('osm:way:4', 54.7149, 20.5068)],
        'streets': {'features': [street('улица Профессора Севастьянова', 54.7148), street('Университетская улица', 54.7158)]}}
    snapshot_surroundings(snapshot, chosen)
    seen = chosen['search_anchor']['map_surroundings']
    assert seen['scope'] == 'search_discovery_only' and seen['snapshot_id'] == 'map_1'
    assert seen['streets_within_35m'] == [{'name': 'улица Профессора Севастьянова', 'distance_m': 11}]
    assert seen['tagged_street_within_35m'] is False and seen['tagged_street_distance_m'] == 78
    assert 'not proof' in seen['meaning']
    assert [(n['address'], n['candidate_id']) for n in seen['neighbour_addresses']] == [
        ('улица Профессора Севастьянова 3/5к2', 'osm:way:2')]
    # The observed tag stays the search address; surroundings only add discovery routes.
    assert chosen['search_anchor']['street'] == 'Университетская улица'
    selected = agent_context({'candidate_id': 'osm:way:1', 'search_anchor': chosen['search_anchor']}, 'goal', 'run', 0)['selected_object']
    assert selected['map_search_hints'] == seen and 'map_surroundings' not in selected['search_anchor']
    # Source verifiers see the selected building, never the neighbouring addresses.
    from street_story.identity_model_context import compact_physical_identity
    verifier = compact_physical_identity({'status': 'owner_confirmed', 'candidate_id': 'osm:way:1',
                                          'search_anchor': chosen['search_anchor']})
    assert 'map_surroundings' not in verifier.get('search_anchor', {}) and '3/5к2' not in str(verifier)
    lonely = box('osm:way:9', 54.7300, 20.5300, 'Тихая улица', '1')
    snapshot_surroundings({'candidates': [lonely], 'streets': {'features': []}}, lonely)
    assert 'map_surroundings' not in lonely['search_anchor']


def test_address_point_inside_a_nameless_contour_names_the_chosen_house():
    # The owner taps a contour without address tags; an OSM address point inside it is its address.
    from street_story.place_selection import snapshot_inner_address
    ring = [{'lat': 54.7265, 'lon': 20.4840}, {'lat': 54.7267, 'lon': 20.4840}, {'lat': 54.7267, 'lon': 20.4843},
            {'lat': 54.7265, 'lon': 20.4843}, {'lat': 54.7265, 'lon': 20.4840}]
    chosen = {'candidate_id': 'osm:way:1', 'name': 'Здание osm:way:1', 'name_provenance': 'map.placeholder',
              'search_anchor': {'city': 'Калининград', 'street': '', 'house_number': ''},
              'map_geometry': {'rings': [{'role': 'outer', 'closed': True, 'points': ring}]}}
    def node(cid, lat, lon, number):
        return {'candidate_id': cid, 'url': 'https://www.openstreetmap.org/node/' + cid.rsplit(':', 1)[1],
                'map_coordinates': {'latitude': lat, 'longitude': lon},
                'map_address': {'street': 'Зоологическая улица', 'house_number': number}}
    snapshot = {'city_objects': [node('osm:node:7', 54.72657, 20.48415, '47А'), node('osm:node:8', 54.72663, 20.48492, '48')]}
    snapshot_inner_address(snapshot, chosen)
    anchor = chosen['search_anchor']
    assert (anchor['street'], anchor['house_number']) == ('Зоологическая улица', '47А')
    assert anchor['address_provenance'] == 'osm_address_point_inside_contour'
    assert [p['candidate_id'] for p in anchor['address_points']] == ['osm:node:7']
    assert chosen['name'] == 'Зоологическая улица 47А' and chosen['map_label'] == 'Здание osm:way:1'
    tagged = {**chosen, 'name': 'Дом', 'search_anchor': {'street': 'Другая улица', 'house_number': '1'}}
    snapshot_inner_address(snapshot, tagged)
    assert tagged['search_anchor'] == {'street': 'Другая улица', 'house_number': '1'}  # own tags win


def test_both_links_round_trip_to_one_point_inside_an_l_shaped_building():
    # The reader opens either link to find, save and visit the building: the point must be on the
    # building itself, also for a concave outline whose centre falls outside it.
    import re
    from urllib.parse import unquote
    pts = [(54.7200, 20.5000), (54.7206, 20.5000), (54.7206, 20.5003), (54.7202, 20.5003),
           (54.7202, 20.5010), (54.7200, 20.5010), (54.7200, 20.5000)]
    ring = {'closed': True, 'points': [{'lat': a, 'lon': o} for a, o in pts]}
    centre = {'latitude': sum(a for a, _ in pts[:-1]) / 6, 'longitude': sum(o for _, o in pts[:-1]) / 6,
              'provenance': 'osm.center'}
    research = {'place_selection': {'mode': 'exact', 'candidate': {'map_coordinates': centre,
                                                                     'map_geometry': {'rings': [ring]}}}}
    links = building_links(research)
    y = re.search(r'whatshere\[point\]=([\d.]+),([\d.]+)&whatshere\[zoom\]=18$', unquote(links['ЯндексКарты']))
    g = re.search(r'^https://2gis\.ru/geo/([\d.]+),([\d.]+)\?m=([\d.]+),([\d.]+)/18$', unquote(links['2ГИС']))
    # 2ГИС needs the explicit zoom: without it the point opens the city card, not the building.
    assert y and g and y.groups() == g.groups()[:2] == g.groups()[2:]
    lon, lat = map(float, y.groups())
    def inside(lat, lon):
        c = False
        for (a1, o1), (a2, o2) in zip(pts, pts[1:]):
            if (a1 > lat) != (a2 > lat) and lon < o1 + (lat - a1) * (o2 - o1) / (a2 - a1):
                c = not c
        return c
    assert inside(lat, lon)
