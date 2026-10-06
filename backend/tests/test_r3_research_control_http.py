"""Transport CAS and visual epoch fences; no external provider execution."""
import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.headless_identity import HeadlessIdentity
from street_story.service import ConflictError
from test_research_control import fixture, research, rows


@pytest.mark.asyncio
async def test_http_resume_fences_prior_visual_epoch_and_replay_cannot_reopen_later_stop(tmp_path):
    service, sid, photo = fixture(tmp_path)
    app = create_app(settings=service.settings, service=service)
    path = f'/v1/stories/{sid}/research-control'
    body = {'purpose': 'identity', 'expected_photo_sha256': photo, 'expected_identity_generation': 0}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        initial = (await client.get(f'/v1/stories/{sid}')).json()
        stopped = await client.post(path, headers={'Idempotency-Key': 'epoch-http-stop'},
            json={**body, 'action': 'stop', 'expected_control_revision': initial['research_control_revision']})
        assert stopped.status_code == 200
        stop_epoch = stopped.json()['research_control_revision']
        before = rows(service, sid)
        old_scope = {'photo_sha256': photo, 'generation': 0, 'control_revision': stop_epoch}
        resume_body = {**body, 'action': 'resume', 'expected_control_revision': stop_epoch}
        resumed = await client.post(path, headers={'Idempotency-Key': 'epoch-http-resume'}, json=resume_body)
        assert resumed.status_code == 200 and resumed.json()['changed'] == ['identity']
        new_epoch = resumed.json()['research_control_revision']
        assert new_epoch > stop_epoch
        state = research(service, sid)
        visual = state['visual_search_operation']
        assert visual['control_revision'] == state['research_controls']['identity']['revision'] == new_epoch
        assert visual['lease_owner'] is None and visual['lease_until'] == 0
        assert visual['queue'] == ['remaining-ref'] and visual['pending']['id'] == 'existing-comparison'
        runner = HeadlessIdentity(service)
        with service.store.connection() as db:
            story = dict(service._story_row(db, sid))
        with pytest.raises(ConflictError):
            runner._assert_visual_current(story, state, old_scope)
        runner._assert_visual_current(story, state, {**old_scope, 'control_revision': new_epoch})
        after = rows(service, sid)
        assert set(after) == set(before)
        later_stop = await client.post(path, headers={'Idempotency-Key': 'epoch-http-stop-again'},
            json={**body, 'action': 'stop', 'expected_control_revision': new_epoch})
        assert later_stop.status_code == 200
        paused = rows(service, sid)
        # A new mutation key carrying the old CAS must fail. A lost-response retry
        # with the original key observes its prior effect and never undoes newer Stop.
        stale = await client.post(path, headers={'Idempotency-Key': 'epoch-http-stale-resume'}, json=resume_body)
        assert stale.status_code == 409 and stale.json()['error']['code'] == 'research_control_stale'
        replay = await client.post(path, headers={'Idempotency-Key': 'epoch-http-resume'}, json=resume_body)
        assert replay.status_code == 200 and replay.json()['changed'] == []
        assert replay.json()['story']['research_controls']['identity']['stopped'] is True
        assert replay.json()['story']['draft_text'] == 'Owner draft'
        assert rows(service, sid) == paused
