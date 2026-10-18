"""Ordinary worker preserves a research outcome through both product wrappers."""
import json

import pytest

from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.runtime import RuntimeStreetStoryService
from test_mvp_acceptance import service


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('owner_selected', [False, True])
async def test_runtime_worker_persists_closed_partial_or_complete_outcome(tmp_path, monkeypatch, complete, owner_selected):
    svc, story = service(tmp_path)
    svc.__class__ = RuntimeStreetStoryService
    outcome = {'outcome': 'no_supported_facts' if complete else 'resource_blocked',
        'reason': 'controlled_closed_sufficient' if complete else 'controlled_closed_insufficient',
        'coverage_complete': complete}

    async def controlled_research(self, job):
        return outcome

    monkeypatch.setattr(MvpResearchStreetStoryService, '_run_research', controlled_research)
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        if owner_selected:
            research['visual_identity'] = {'status': 'owner_confirmed'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
        svc._enqueue_job(db, story['id'], 'research', 'controlled-outcome',
            {'photo_sha256': story['photo_sha256'], 'identity_generation': 0, 'input_revision': 'controlled-outcome'})
    try:
        assert await svc.run_once(claim_kind='research', claim_story_id=story['id'])
        with svc.store.connection() as db:
            job = db.execute("SELECT state,last_error FROM jobs WHERE kind='research'").fetchone()
            current = json.loads(svc._story_row(db, story['id'])['research_json'])
        assert job['state'] == 'done' and job['last_error'] == outcome['reason']
        assert current['automatic_research_outcome']['coverage_complete'] is complete
        assert current['automatic_research_outcome']['reason'] == outcome['reason']
    finally:
        for observer in list(getattr(svc, '_terminal_observers', [])):
            await observer
