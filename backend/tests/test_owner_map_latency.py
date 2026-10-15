import asyncio
import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.place_selection import place_map
from test_identity_lifecycle import create, make_service
from test_owner_map_selection import MapOSM


@pytest.mark.asyncio
async def test_slow_optional_geometry_does_not_withhold_complete_selectable_contours(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    story = create(service)

    class SlowExtras(MapOSM):
        cancelled = False

        async def _map_nearby(self, *args):
            return {**await super()._map_nearby(*args), 'incomplete_building_ids': ['osm:relation:9']}

        async def _overpass_query(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    service.providers.osm = SlowExtras()
    monkeypatch.setattr('street_story.place_selection.OWNER_MAP_ENRICHMENT_SECONDS', .01)
    result = await place_map(service, story['id'])
    assert service.providers.osm.cancelled
    assert result['partial'] and result['incomplete_building_count'] == 1
    assert {f['id'] for f in result['buildings']['features']} == {'osm:way:1', 'osm:way:2'}
    assert all(f['geometry']['type'] == 'Polygon' for f in result['buildings']['features'])
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs WHERE story_id=?', (story['id'],)).fetchone()[0] == 0
    assert service.story(story['id'])['place_selection'] is None


@pytest.mark.asyncio
async def test_same_view_reads_join_and_disappearing_view_does_not_cancel_other_waiter(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedMap(MapOSM):
        calls = 0

        async def _map_nearby(self, *args):
            self.calls += 1
            entered.set()
            await release.wait()
            return await super()._map_nearby(*args)

    service.providers.osm = DelayedMap()
    first = asyncio.create_task(place_map(service, story['id']))
    await entered.wait()
    second = asyncio.create_task(place_map(service, story['id']))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second
    assert service.providers.osm.calls == 1
    assert not service._owner_map_requests
    cached = await place_map(service, story['id'])
    assert cached['snapshot_id'] == result['snapshot_id']
    assert service.providers.osm.calls == 1
    with service.store.connection() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
    assert len(research['place_map_snapshots']) == 1


@pytest.mark.asyncio
async def test_map_source_deadline_returns_normal_retryable_http_error_before_client_timeout(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    story = create(service)

    class UnavailableMap(MapOSM):
        async def _map_nearby(self, *args):
            await asyncio.Event().wait()

    service.providers.osm = UnavailableMap()
    monkeypatch.setattr('street_story.place_selection.OWNER_MAP_REQUEST_SECONDS', .01)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.settings, service)), base_url='http://test') as client:
        response = await client.get(f"/v1/stories/{story['id']}/place-map",
                                    headers={'Authorization': 'Bearer ' + reveal(service.settings.device_token)})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'place_map_unavailable'
    assert not service._owner_map_requests
    current = service.story(story['id'])
    assert current['place_map'] is None and current['place_selection'] is None
    assert current['state'] == story['state']
