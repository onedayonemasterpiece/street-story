import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.research_budget import ensure_budget
from street_story.research_control import stop_research
from test_identity_lifecycle import make_service, create


@pytest.mark.asyncio
@pytest.mark.parametrize('stopped', [False, True])
async def test_http_owner_hint_is_durable_idempotent_and_preserves_source_stop(tmp_path, stopped):
    service, gemini = make_service(tmp_path)
    story = create(service, client='http-hint', lat=None, lon=None)
    sid = story['id']
    ensure_budget(service, sid)
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        research['identity_clarification'] = {'question': 'Где сделан снимок?',
            'photo_sha256': row['photo_sha256'], 'generation': 0, 'control_revision': 0,
            'background_continuation_pending': True}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
    if stopped:
        stop_research(service, sid, purpose='identity')
    url = f'/v1/stories/{sid}/identity-hint'
    body = {'text': 'Калининград, восточнее Молодой Гвардии, севернее Московского проспекта.'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.settings, service)),
                                 base_url='http://test') as client:
        assert (await client.post(url, json=body)).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        assert (await client.post(url, json=body)).status_code == 400
        client.headers['Idempotency-Key'] = 'owner-hint-http'
        first = await client.post(url, json=body)
        if stopped:
            assert first.status_code == 409
            assert first.json()['error']['code'] == 'identity_owner_stopped'
            _, research = service._identity_snapshot(sid)
            assert not research.get('identity_owner_hint')
            assert int(research.get('identity_generation') or 0) == 0
        else:
            assert first.status_code == 200
            replay = await client.post(url, json=body)
            assert replay.status_code == 200
            for key in ('id', 'photo_sha256', 'identity_generation', 'state'):
                assert replay.json()[key] == first.json()[key]
            assert (await client.post(url, json={'text': 'Другой ответ'})).status_code == 409
            _, research = service._identity_snapshot(sid)
            assert research['identity_clarification']['answered'] is True
            assert research['identity_owner_hint']['text'] == body['text']
            assert research['identity_owner_hint']['scope'] == 'search_context_only'
            assert research['identity_generation'] == 1
            with service.store.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM jobs WHERE story_id=? AND kind='identity'", (sid,)).fetchone()[0] == 1
    assert service.story(sid)['photo_sha256'] == story['photo_sha256']
    assert gemini.identity_calls == []
