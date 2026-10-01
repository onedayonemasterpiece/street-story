import hashlib
import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.mvp_research import MvpResearchStreetStoryService
from test_identity_lifecycle import make_service
from test_identity_recovery_policy import photo, create_photo


@pytest.mark.asyncio
async def test_photo_diagnostics_are_bounded_authorized_and_idempotent(tmp_path):
    service, _ = make_service(tmp_path)
    story = create_photo(service, photo(redacted=True))
    app = create_app(settings=service.settings, service=service)
    url = f"/v1/stories/{story['id']}/diagnostics"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.post(url, json={})).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        payload = {'event_id': 'fixture-import', 'status': 'gps_redacted_or_invalid', 'gps_present': False,
                   'uri': 'content://private/original', 'token': 'private', 'latitude': 54.7}
        assert (await client.post(url, json=payload)).status_code == 200
        assert (await client.post(url, json=payload)).status_code == 200
        assert (await client.post(url, content='x' * 8193)).status_code == 413
        assert (await client.post(url, json={'event': 'publish'})).status_code == 400
    with service.store.connection() as db:
        rows = db.execute("SELECT payload_json FROM live_diagnostics WHERE story_id=? AND source='android_import'", (story['id'],)).fetchall()
    assert len(rows) == 1
    saved = json.loads(rows[0][0])
    assert saved == {'event_id': 'fixture-import', 'status': 'gps_redacted_or_invalid', 'gps_present': False}


@pytest.mark.asyncio
async def test_same_photo_gps_recovery_route_keeps_original_story_hash(tmp_path):
    service, _ = make_service(tmp_path)
    redacted = photo(redacted=True)
    story = create_photo(service, redacted)
    app = create_app(settings=service.settings, service=service)
    url = f"/v1/stories/{story['id']}/photo-location"
    payload = {'expected_photo_sha256': hashlib.sha256(redacted).hexdigest()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.post(url, data=payload, files={'photo': ('original.jpg', photo(), 'image/jpeg')})).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        wrong = await client.post(url, data=payload, files={'photo': ('other.jpg', photo(shade=5), 'image/jpeg')})
        assert wrong.status_code == 409
        assert wrong.json()['error']['code'] == 'photo_recovery_mismatch'
        result = await client.post(url, data=payload, files={'photo': ('original.jpg', photo(), 'image/jpeg')})
        assert result.status_code == 200, result.text
        assert result.json()['id'] == story['id']
    with service.store.connection() as db:
        row = db.execute('SELECT photo_sha256,latitude,longitude FROM stories WHERE id=?', (story['id'],)).fetchone()
    assert row['photo_sha256'] == payload['expected_photo_sha256']
    assert row['latitude'] is not None and row['longitude'] is not None


def test_rejected_candidates_do_not_consume_shortlist_slots():
    osm = {'reverse': {}, 'nearby': [
        {'type': 'way', 'id': i, 'distance_m': i * 10,
         'selection_bucket': 'nearby', 'tags': {'name': f'Facade {i}', 'building': 'yes'}}
        for i in range(1, 26)
    ]}
    first = MvpResearchStreetStoryService._candidate_catalog(osm, [])
    assert len(first) == 16
    rejected = {item['candidate_id'] for item in first}
    following = MvpResearchStreetStoryService._candidate_catalog(osm, [], excluded_ids=rejected)
    assert len(following) == 9
    assert rejected.isdisjoint({item['candidate_id'] for item in following})
    assert [item['distance_m'] for item in following] == sorted(item['distance_m'] for item in following)
