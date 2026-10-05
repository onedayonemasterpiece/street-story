import json
from types import SimpleNamespace

import pytest

from street_story.research_control import resume_research, stop_research
from test_research_control import fixture


def test_confirmed_photo_schedules_one_fact_request_without_voice_and_preserves_editorial_state(tmp_path):
    service, sid, photo = fixture(tmp_path)
    service.providers.research = SimpleNamespace(facts_available=True)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='done' WHERE story_id=?", (sid,))
        db.execute("UPDATE stories SET state='identity_ready' WHERE id=?", (sid,))
    stop_research(service, sid, purpose='facts')
    service._schedule_confirmed_facts()
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE semantic_key LIKE 'automatic-facts:%'").fetchone()[0] == 0
    resume_research(service, sid, purpose='facts')
    service._schedule_confirmed_facts()
    service._schedule_confirmed_facts()
    with service.store.tx() as db:
        jobs = list(db.execute("SELECT * FROM jobs WHERE semantic_key LIKE 'automatic-facts:%'"))
        assert len(jobs) == 1
        payload = json.loads(jobs[0]['payload_json'])
        assert payload['voice_session_ids'] == []
        assert payload['photo_sha256'] == photo and payload['identity_generation'] == 0
        row = service._story_row(db, sid)
        assert row['draft_text'] == 'Owner draft'
        assert json.loads(row['research_json'])['publication_concept'] == 'Owner concept'
        db.execute("UPDATE jobs SET state='done' WHERE id=?", (jobs[0]['id'],))
        db.execute("UPDATE stories SET state='identity_ready' WHERE id=?", (sid,))
    service._schedule_confirmed_facts()
    assert service.story(sid)['state'] == 'identity_ready'


@pytest.mark.asyncio
async def test_configured_research_defers_legacy_unverified_match_into_shared_visual_queue(tmp_path):
    service, sid, photo = fixture(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True)
    calls = []
    async def legacy(*args):
        calls.append(args)
        return {'status': 'match', 'candidate_id': 'wiki:1', 'confidence': 1}
    service.providers.gemini.identify_photo = legacy
    candidates = [{'candidate_id': 'wiki:1', 'name': 'Test'}]
    result = await service._identify_photo({'id': sid, 'photo_sha256': photo}, '', candidates)
    batch = await service._identify_photo_batch({'id': sid}, '', candidates)
    assert result['_comparison_deferred'] and batch['_comparison_deferred']
    assert result['status'] == batch['status'] == 'uncertain'
    assert not calls
