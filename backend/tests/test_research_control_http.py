import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.service import ConflictError
from test_research_control import fixture, rows


def test_idempotent_research_control_does_not_repeat_old_stop_after_resume(tmp_path):
    service, sid, photo = fixture(tmp_path)
    body = {'action': 'stop', 'purpose': 'identity', 'expected_photo_sha256': photo,
            'expected_identity_generation': 0}
    first = service.mutate_research_control(sid, 'first-stop', body)
    assert first['changed'] == ['identity']
    assert first['story']['photo_sha256'] == photo
    assert first['story']['identity_generation'] == 0
    assert first['story']['research_controls']['identity']['stopped'] is True
    assert first['story']['research_controls']['facts']['stopped'] is False
    service.mutate_research_control(sid, 'resume', {**body, 'action': 'resume'})
    before = rows(service, sid)
    replay = service.mutate_research_control(sid, 'first-stop', body)
    assert replay['changed'] == []
    assert replay['story']['research_controls']['identity']['stopped'] is False
    assert rows(service, sid) == before
    with pytest.raises(ConflictError, match='different content'):
        service.mutate_research_control(sid, 'first-stop', {**body, 'purpose': 'all'})


@pytest.mark.asyncio
async def test_research_control_route_requires_auth_idempotency_and_current_scope(tmp_path):
    service, sid, photo = fixture(tmp_path)
    app = create_app(settings=service.settings, service=service)
    url = f'/v1/stories/{sid}/research-control'
    body = {'action': 'stop', 'purpose': 'facts', 'expected_photo_sha256': photo,
            'expected_identity_generation': 0}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post(url, json=body)).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        assert (await client.post(url, json=body)).status_code == 400
        client.headers['Idempotency-Key'] = 'http-stop'
        assert (await client.post(url, json={**body, 'expected_photo_sha256': 'changed'})).status_code == 409
        first = await client.post(url, json=body)
        assert first.status_code == 200 and first.json()['changed'] == ['facts']
        assert (await client.post(url, json=body)).json()['changed'] == []
        with service.store.connection() as db:
            state = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
            assert state['research_controls']['facts']['stopped'] is True
            assert state['publication_concept'] == 'Owner concept'
