"""Small G/T evidence-supply regressions; no provider sends or answer hints to models."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from street_story.identity_architectural_context import (
    acquire_regional_text,
    literal_address_card_selection,
)
from street_story.identity_model_context import (
    _outline_angular_scale,
    physical_decision_context,
)
from street_story.identity_scene import render_scene
from street_story import prussia39
from test_identity_architectural_context import inputs
from test_identity_scene import building


def test_frozen_photo102_geometry_contrast_is_an_advisory_scale_not_identification():
    # Independent source: frozen OSM canary measurements and selected original EXIF.
    # Truth labels/physical target IDs are intentionally absent from the function.
    diagonal = 84.1  # 24mm 35mm-equivalent nominal reference diagonal.
    nearer = _outline_angular_scale([282.9, 318.5, 35.6], diagonal)
    farther = _outline_angular_scale([269.2, 274.7, 5.4], diagonal)
    assert nearer == pytest.approx(.423, abs=.001)
    assert farther == pytest.approx(.064, abs=.001)
    assert nearer > 6 * farther
    # Ratios do NOT recover camera distance, the visible portion of a building,
    # a correct building ID or actual sensor/crop calibration.


def test_physical_scene_includes_all_received_candidates_and_exif_outline_context():
    from street_story.identity_map_context import geometry_camera_context
    first, second, remote = building(2, 20), building(3, 50), building(4, 200)
    # The normal OSM provider supplies measured boundary bearing intervals.
    measured = [{**item, **geometry_camera_context(item, 54.7, 20.5)}
        for item in (first, second, remote)]
    source = {'latitude': 54.7, 'longitude': 20.5, '_camera_position_verified': True,
        '_camera_hints': {'diagonal_fov_35mm_deg': 84.1, 'focal_length_35mm': 24,
            'digital_zoom_ratio': 3, 'direction_status': 'missing'},
        '_identity_map_snapshot': {'observed_pool': measured}}
    saved = copy.deepcopy(source)
    rendered = render_scene(source, [])
    capsule = physical_decision_context(source, [], rendered['manifest'])
    rows = [dict(zip(capsule['columns'], row)) for row in capsule['rows']]
    assert {row['candidate_id'] for row in rows} == {
        'osm:way:2', 'osm:way:3', 'osm:way:4'}
    assert capsule['received_body_count'] == 3
    prior = capsule['source_angular_reference']
    assert prior['diagonal_fov_35mm_deg'] == 84.1
    assert prior['camera_position_status'] == 'original_exif'
    assert 'NOT the fraction of image pixels' in prior['policy']
    assert all(row['outline_span_over_exif_diagonal'] is not None for row in rows)
    assert not any('identity' in row for row in rows)
    assert source == saved
    source['_camera_position_verified'] = False
    no_camera = render_scene(source, [])
    result = physical_decision_context(source, [], no_camera['manifest'])
    assert result['source_angular_reference']['diagonal_fov_35mm_deg'] is None
    angular_index = result['columns'].index('outline_span_over_exif_diagonal')
    assert all(row[angular_index] is None for row in result['rows'])
    shape_index = result['columns'].index('plan_morphology')
    assert all(row[shape_index][0] == 'observed_closed_outer' for row in result['rows'])


@pytest.mark.parametrize('span,diagonal', [
    ([None, None, None], 84.1), ([0, 10, True], 84.1),
    ([0, 10, float('nan')], 84.1), ([0, 10, 10], None),
    ([], 84.1),
])
def test_missing_or_invalid_outline_does_not_invent_scale(span, diagonal):
    assert _outline_angular_scale(span, diagonal) is None


def card(sid, address):
    return {'article_id': f'prussia39:sid:{sid}',
        'canonical_url': f'https://www.prussia39.ru/sight/index.php?sid={sid}',
        'address_text': address}


def test_literal_catalogue_metadata_filter_is_exact_and_not_a_house_number_guess():
    received = [
        card(1, 'Город, ул. Тестовая, д. 22А'),
        card(2, 'Город, Тестовая улица, 22/24'),
        card(3, 'Город, Тестовая улица, 122А'),
        card(4, 'Город, Иная улица, 22А'),
        card(5, 'Город, Тестовая, 22'),
        card(6, ''),  # Empty article body/metadata is not an absent result.
    ]
    assert [row['article_id'] for row in literal_address_card_selection(
        received, 'Тестовая улица', '22А')] == ['prussia39:sid:1']
    assert [row['article_id'] for row in literal_address_card_selection(
        received, 'Тестовая', '22')] == ['prussia39:sid:5']
    assert literal_address_card_selection(received, 'Несуществующая улица', '22А') == []


@pytest.mark.asyncio
async def test_broad_partial_catalogue_remains_for_model_selection_without_postal_parser(monkeypatch):
    story, physical, request = inputs()
    body = 'На фасаде выделяются три эркера с разной формой завершения.'
    received = [
        card(21, 'Город, Тестовая улица, 222А'),
        card(22, 'Город, Другая улица, 22А'),
        card(23, 'Город, Тестовая улица, 22А'),
        card(24, 'Город, Тестовая улица, 22/24'),
    ]
    calls = []

    class Adapter:
        def __init__(self, *args): pass

        async def address_search(self, city, query):
            calls.append(('catalogue', city, query))
            return {'status': 'completed', 'inventory_complete': False,
                'results': copy.deepcopy(received), 'total_count': 20}

        async def article(self, url):
            calls.append(('body', url))
            assert url == received[2]['canonical_url']
            return {'status': 'completed', 'text': body,
                'article_id': 'prussia39:sid:23', 'canonical_url': url,
                'raw_content_sha256': hashlib.sha256(body.encode('cp1251')).hexdigest(),
                'raw_body_sha256_verified': True}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [physical], request)
    assert calls == [('catalogue', 'Город', 'Тестовая улица, 22А')]
    assert len(receipt['results']) == 4 and receipt['inventory_complete'] is False
    assert 'literal_address_selection' not in receipt
    assert articles == []
    assert 'model source selection' in receipt['limitation']


@pytest.mark.asyncio
async def test_multiple_exact_addresses_remain_unselected_if_more_than_two(monkeypatch):
    story, physical, request = inputs()
    cards = [card(sid, 'Город, Тестовая, 22А') for sid in range(10, 14)]

    class Adapter:
        def __init__(self, *args): pass

        async def address_search(self, *args):
            return {'status': 'completed', 'results': cards, 'inventory_complete': True}

        async def article(self, url):
            pytest.fail('Ambiguous publisher selection cannot acquire a body')

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [physical], request)
    assert not articles and len(receipt['results']) == 4
    assert 'limitation' in receipt
