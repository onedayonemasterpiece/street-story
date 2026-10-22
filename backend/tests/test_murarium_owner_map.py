"""Regression: the actual Murarium circle is way 303/406 in an OSM map viewport.

OSM geometry/tags were read 2026-10-22 from openstreetmap.org/api/0.6/map.json;
the real nearby Paradox hotel is a DIFFERENT building (way 382545007).
No network or personal topic data is used by these tests.
"""
from __future__ import annotations

import json

import pytest

from street_story.place_selection import place_map, confirm_place
from street_story.service import digest, canonical
from test_identity_lifecycle import make_service, create
from test_owner_map_selection import command

LAT, LON = 54.95979, 20.48254
MURARIUM = [
    (54.9598162, 20.4825858),
    (54.9598097, 20.4825949),
    (54.9598023, 20.4826014),
    (54.9597942, 20.4826052),
    (54.9597859, 20.4826059),
    (54.9597777, 20.4826036),
    (54.9597699, 20.4825983),
    (54.9597629, 20.4825904),
    (54.959757, 20.4825801),
    (54.9597525, 20.4825679),
    (54.9597495, 20.4825543),
    (54.9597483, 20.48254),
    (54.9597487, 20.4825255),
    (54.9597509, 20.4825114),
    (54.9597547, 20.4824985),
    (54.95976, 20.4824872),
    (54.9597665, 20.4824781),
    (54.9597739, 20.4824716),
    (54.959782, 20.4824678),
    (54.9597903, 20.4824671),
    (54.9597985, 20.4824694),
    (54.9598063, 20.4824747),
    (54.9598133, 20.4824826),
    (54.9598192, 20.4824929),
    (54.9598237, 20.4825051),
    (54.9598267, 20.4825187),
    (54.959828, 20.4825331),
    (54.9598275, 20.4825476),
    (54.9598253, 20.4825616),
    (54.9598215, 20.4825745),
    (54.9598162, 20.4825858),
]
PARADOX = [
    (54.9600336, 20.4825716),
    (54.9599172, 20.4826883),
    (54.959865, 20.4827406),
    (54.9597656, 20.4827379),
    (54.9597599, 20.4828711),
    (54.9597541, 20.4830074),
    (54.9598769, 20.4830137),
    (54.9600998, 20.4827674),
    (54.9600336, 20.4825716),
]


def way(osm_id, name, geometry, *, extra=None):
    return {'type': 'way', 'id': osm_id,
            'tags': {'building': 'yes', 'name': name, **(extra or {})},
            'geometry': [{'lat': lat, 'lon': lon} for lat, lon in geometry]}


def rectangle(lat, lon, height=.00004, width=.00004):
    return [(lat, lon), (lat+height, lon), (lat+height, lon+width),
            (lat, lon+width), (lat, lon)]


class OverLimitOSM:
    requests = 0

    async def _map_nearby(self, client, latitude, longitude, radius):
        self.requests += 1
        # Provider order is not map proximity. Every filler is a valid closed
        # building but all are farther from the author's selected map center.
        filler = [way(i + 1000, f'Other building {i}',
                      rectangle(LAT + .001 + (i % 15) * .00005,
                                LON + .001 + (i // 15) * .00006))
                  for i in range(302)]
        tower = way(133724496, 'Мурариум', MURARIUM, extra={
            'man_made': 'water_tower', 'tourism': 'museum',
            'addr:street': 'Саратовская улица', 'addr:housenumber': '2А'})
        distinct_neighbor = way(382545007, 'Парадокс', PARADOX,
                                extra={'tourism': 'hotel'})
        # A second legitimately circular, nonrectangular building elsewhere,
        # constructed from the same observed circle translated 40m east.
        other_tower = way(133724497, 'Другая круглая башня',
                          [(lat, lon+.0006) for lat,lon in MURARIUM])
        ordinary = way(900000000, 'Обычный дом',
                       rectangle(LAT-.00012, LON-.00022))
        return {'elements': [*filler, tower, distinct_neighbor, other_tower, ordinary]}


@pytest.mark.asyncio
async def test_murarium_31_vertex_footprint_survives_250_limit_and_is_confirmable(tmp_path):
    svc, _ = make_service(tmp_path)
    story = create(svc)
    osm = OverLimitOSM()
    svc.providers.osm = osm
    snapshot = await place_map(svc, story['id'], latitude=LAT, longitude=LON)
    features = {f['id']:f for f in snapshot['buildings']['features']}
    tower_id = 'osm:way:133724496'
    assert len(features) == 250
    assert snapshot['map_schema_version'] == 2
    assert tower_id in features  # Regression: the old first-250 algorithm LOST it.
    assert 'osm:way:382545007' in features
    assert features[tower_id]['properties']['name'] == 'Мурариум'
    assert len(features[tower_id]['geometry']['coordinates'][0]) == 31
    assert features[tower_id]['geometry']['coordinates'][0][0] == [20.4825858, 54.9598162]
    assert 'osm:way:133724497' in features  # another nonrectangular, 31-vertex building
    assert 'osm:way:900000000' in features  # ordinary rectangle still selectable
    owner = confirm_place(svc, story['id'], 'confirm-murarium',
                          command(story, snapshot, tower_id, 'confirm-murarium'))
    assert owner['place_selection']['candidate_id'] == tower_id
    assert owner['place_selection']['candidate']['osm_observation']['id'] == 133724496
    assert owner['place_selection']['candidate_id'] != 'osm:way:382545007'


@pytest.mark.asyncio
async def test_old_truncated_cached_snapshot_reloads_new_nearest_buildings_without_reset(tmp_path):
    svc, _ = make_service(tmp_path)
    story = create(svc)
    osm = OverLimitOSM()
    svc.providers.osm = osm
    old_key = digest(['related-map-observations-v1',
        (story['photo_sha256'], story['identity_generation']), LAT, LON, 350, 17])
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'] or '{}')
        research['place_map_snapshots'] = [
            {'cache_key': old_key, 'snapshot_id': 'old-truncated', 'buildings': {
                'type': 'FeatureCollection', 'features': []}}]
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (canonical(research), story['id']))
    current = await place_map(svc, story['id'], latitude=LAT, longitude=LON)
    assert current['snapshot_id'] != 'old-truncated'
    assert 'osm:way:133724496' in {f['id'] for f in current['buildings']['features']}
    assert osm.requests == 1
    with svc.store.connection() as db:
        records = json.loads(svc._story_row(db, story['id'])['research_json'])['place_map_snapshots']
    assert any(r['snapshot_id'] == 'old-truncated' for r in records)
