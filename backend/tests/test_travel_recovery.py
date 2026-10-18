"""Outage recovery through existing durable workers, HTTP intake and fences."""
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story import identity_discovery
from street_story.errors import RetryableProviderError
from street_story.identity_telemetry import record_identity_event
from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.research_control import stop_research
from test_identity_lifecycle import PHOTO, create, make_service


@pytest.mark.asyncio
async def test_outage_restart_resumes_saved_original_without_closed_client(tmp_path, monkeypatch):
    service, gemini = make_service(tmp_path)
    clock = [1000.0]
    service.store.now = lambda: clock[0]
    normal_lookup = service.providers.osm.lookup
    normal_wiki = service.providers.wikipedia.nearby
    down = True

    async def lookup(*args):
        if down:
            raise RetryableProviderError('osm_transport_waiting')
        return await normal_lookup(*args)

    async def wiki(*args):
        if down:
            raise RetryableProviderError('wiki_transport_waiting')
        return await normal_wiki(*args)

    async def discover(*args):
        if down:
            raise RetryableProviderError('discovery_transport_waiting')
        return None

    service.providers.osm.lookup = lookup
    service.providers.wikipedia.nearby = wiki
    monkeypatch.setattr(identity_discovery, 'recover', discover)
    story = create(service, client='travel-preserve')
    sid = story['id']
    unknown = {'operation_id': 'old-unknown', 'status': 'unknown', 'request_key': 'do-not-resend'}
    with service.store.tx() as db:
        research = {'publication_concept': 'Угол автора', 'native_visual_operation': unknown}
        db.execute('UPDATE stories SET research_json=?,draft_text=? WHERE id=?', (json.dumps(research), 'Черновик автора', sid))
        db.execute('INSERT INTO facts VALUES(?,?,?,?,?,?,?)', (sid, 'kept', 'Проверенный факт', .95, 1, 1, '[{"url":"https://example.org/source"}]'))
    service.ensure_identity(sid)
    assert await service.run_once()
    with service.store.connection() as db:
        first_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity'", (sid,)).fetchone())
        assert first_job['state'] == 'retry' and first_job['available_at'] >= clock[0] + 60
    _, saved = service._identity_snapshot(sid)
    assert 'identity_attempted_generation' not in saved
    assert saved['identity_sources_waiting'] is True
    assert saved['native_visual_operation'] == unknown
    assert service.story(sid)['state'] == 'identifying'
    assert not await service.run_once()  # no hot loop before the durable deadline

    # A closed client is no longer needed to restore SOURCE after a restart.
    # The same durable job and unknown external operation remain authoritative.
    restarted = MvpResearchStreetStoryService(service.settings, service.providers)
    restarted.store.now = lambda: clock[0]
    clock[0] = first_job['available_at'] + 1
    restarted.recover_jobs()
    assert await restarted.run_once()
    with restarted.store.connection() as db:
        wait = dict(db.execute('SELECT * FROM jobs WHERE id=?', (first_job['id'],)).fetchone())
        assert wait['state'] == 'retry'
    assert restarted._source_photo_bytes(sid) == PHOTO
    assert not gemini.identity_calls
    down = False
    clock[0] = wait['available_at'] + 1
    assert await restarted.run_once()
    result = restarted.story(sid)
    assert result['state'] == 'identity_ready' and result['visual_identity']['status'] == 'match'
    assert result['publication_concept'] == 'Угол автора' and result['draft_text'] == 'Черновик автора'
    _, saved = restarted._identity_snapshot(sid)
    assert saved['native_visual_operation'] == unknown
    with restarted.store.connection() as db:
        assert db.execute('SELECT count(*) FROM stories').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        job = db.execute('SELECT id,state FROM jobs').fetchone()
        assert (job['id'], job['state']) == (first_job['id'], 'done')
        fact = db.execute('SELECT selected,text FROM facts WHERE story_id=? AND fact_id=?', (sid, 'kept')).fetchone()
        assert fact['selected'] == 1 and fact['text'] == 'Проверенный факт'


@pytest.mark.asyncio
async def test_discovery_transport_failure_is_not_a_completed_negative(monkeypatch, tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service, client='discovery-wait')
    gemini._generate = lambda: None
    gemini.executor = SimpleNamespace(execute=lambda *args: None)

    async def failed(*args):
        raise httpx.ReadTimeout('temporary provider timeout')

    monkeypatch.setattr(identity_discovery, 'suggest', failed)
    with pytest.raises(RetryableProviderError, match='identity_discovery_waiting'):
        await identity_discovery.recover(service, story, '', [], set())


@pytest.mark.asyncio
async def test_failed_wikimedia_branches_are_not_treated_as_no_search_hits(monkeypatch):
    async def failed(*args):
        raise httpx.ReadTimeout('temporary Wikimedia timeout')

    monkeypatch.setattr(identity_discovery, 'api', failed)
    with pytest.raises(RetryableProviderError, match='identity_discovery_sources_waiting'):
        await identity_discovery.retrieve(SimpleNamespace(), ['object query'], 'visible geometry', set())


@pytest.mark.asyncio
@pytest.mark.parametrize('paused,outage', [(False, True), (True, True), (False, False)])
async def test_legacy_outage_recovery_reuses_job_and_respects_stop_and_genuine_uncertainty(tmp_path, monkeypatch, paused, outage):
    service, gemini = make_service(tmp_path)

    async def uncertain(*args):
        return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0, 'observations': [], '_references_sent': []}

    gemini.identify_photo = uncertain
    story = create(service, client='legacy-wait')
    sid = story['id']
    service.ensure_identity(sid)
    assert await service.run_once()
    assert service.story(sid)['state'] == 'needs_review'
    if outage:
        record_identity_event(service, sid, 'identity_osm_unavailable', {'generation': 0, 'error_type': 'RetryableProviderError'})
    if paused:
        stop_research(service, sid, purpose='identity')
    with service.store.connection() as db:
        before = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity'", (sid,)).fetchone())
    repaired = service.recover_jobs()
    assert bool(repaired) == (outage and not paused)
    with service.store.connection() as db:
        after = db.execute('SELECT * FROM jobs WHERE id=?', (before['id'],)).fetchone()
        assert after['state'] == ('retry' if outage and not paused else 'done')
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
    assert service.recover_jobs() == 0  # migration is not a repeated scheduler loop
