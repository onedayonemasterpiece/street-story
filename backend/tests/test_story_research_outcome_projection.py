"""Ordinary owner research outcomes remain visible through the REST projection."""
import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.place_selection import confirm_place
from test_identity_lifecycle import create, make_service
from test_owner_map_selection import command, map_for


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic', [None, {}, {'outcome': 'useful_complete', 'reason': 'automatic_complete'}])
async def test_owner_blocked_outcome_visible_and_automatic_precedence_preserved(tmp_path, automatic):
    service, gemini = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    selected = confirm_place(service, story['id'], 'select-house',
        command(story, snapshot, 'osm:way:1', 'select-house'))
    outcome = {'outcome': 'resource_blocked', 'reason': 'research_agent_route_failed',
        'coverage_complete': False, 'eligible_count': 0, 'purpose': 'facts',
        'pipeline': 'research-agent-verification-v1', 'run_id': 'original-owner-run',
        'photo_sha256': selected['photo_sha256'], 'identity_generation': selected['identity_generation']}
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['research_outcome'] = outcome
        research['automatic_research_outcome'] = automatic
        db.execute('UPDATE stories SET state=?,research_json=? WHERE id=?',
            ('facts_ready', json.dumps(research), story['id']))
        db.execute('UPDATE jobs SET state=? WHERE story_id=?', ('done', story['id']))
    app = create_app(service.settings, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://projection',
            headers={'Authorization': 'Bearer ' + reveal(service.settings.device_token)}) as client:
        response = await client.get('/v1/stories/' + story['id'])
    assert response.status_code == 200
    visible = response.json()
    assert visible['research_outcome'] == (outcome if automatic is None else automatic)
    assert visible['visual_identity']['status'] == 'owner_confirmed'
    assert visible['visual_identity']['candidate_id'] == 'osm:way:1'
    assert visible['identity_generation'] == selected['identity_generation']
    assert visible['facts'] == []
    assert not gemini.identity_calls
