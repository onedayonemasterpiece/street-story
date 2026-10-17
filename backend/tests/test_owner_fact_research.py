"""Exact author identity survives lookup, prompt compaction and empty discovery."""
import json
from types import SimpleNamespace

import pytest

from street_story.headless_facts import HeadlessFacts
from street_story.identity_model_context import compact_physical_identity, fact_review_subject
from street_story.research_adapter import fact_page_capsule
from test_headless_facts import fixture, URL
from test_live_editor import make_service, mark_identity_ready


@pytest.mark.asyncio
@pytest.mark.parametrize('stopped', [False, True])
async def test_photo_only_agent_input_initializes_live_schema_before_writer_lock(tmp_path, stopped):
    from street_story.service import ConflictError
    service, job, _, reader, _ = await fixture(tmp_path)
    try:
        with service.store.tx() as db:
            # A photo-only intake has not used the interactive Live adapter yet.
            db.execute('DROP TABLE live_diagnostics')
            row = service._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            if stopped:
                research['research_controls'] = {'facts': {'stopped': True,
                    'photo_sha256': row['photo_sha256'], 'identity_generation': 0, 'revision': 1}}
                db.execute('UPDATE stories SET research_json=? WHERE id=?',
                    (json.dumps(research), row['id']))
        if stopped:
            with pytest.raises(ConflictError) as failure:
                service._persist_agent_input_revision(job, 'headless-run', 'photo-input', research)
            assert failure.value.code == 'agent_research_stale'
        else:
            service._persist_agent_input_revision(job, 'headless-run', 'photo-input', research)
        with service.store.connection() as db:
            current = json.loads(service._story_row(db, job['story_id'])['research_json'])
            assert (current.get('input_revision') == 'photo-input') is not stopped
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='live_diagnostics'").fetchone()
            assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 0
    finally:
        await reader.search_http.aclose()


def test_address_provenance_survives_reader_and_review_compaction():
    anchor = {'city': 'Калининград', 'street': 'Житомирская', 'house_number': '22–24',
              'names': ['Дом'], 'source': 'osm.observed_tags',
              'provenance': {'house_number': 'osm:way:133035113'}}
    identity = {'status': 'owner_confirmed', 'candidate_id': 'osm:way:133035113',
                'candidate_name': 'Дом', 'generation': 2, 'search_anchor': anchor,
                'selection_basis': 'owner_exact', 'owner_selected': True}
    compact = compact_physical_identity(compact_physical_identity(identity))
    assert compact['search_anchor'] == anchor
    assert fact_review_subject(identity)['search_anchor'] == anchor
    page = {'evidence_passages': [], 'chunk_id': 'unit',
            'source_version_id': 'version', 'source_url': URL}
    capsule = fact_page_capsule(page, {'confirmed_identity': compact, 'known_facts': []})
    assert anchor['city'] in json.dumps(capsule, ensure_ascii=False)
    assert anchor['house_number'] in json.dumps(capsule, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('gps', [True, False])
async def test_exact_owner_research_skips_osm_wikipedia_and_identification(tmp_path, monkeypatch, gps):
    service, _, session, _ = make_service(tmp_path)
    sid = session.resource_id
    mark_identity_ready(service, sid, name='Selected house')
    async def forbidden(*_args, **_kwargs):
        pytest.fail('Author-confirmed research must not reacquire or re-identify its object')
    service.providers.osm.lookup = forbidden
    service.providers.wikipedia.nearby = forbidden
    service._identify_photo = forbidden
    service._extract_place_query = forbidden
    service._resolve_place_query = forbidden
    service.providers.research = SimpleNamespace(extract_fact_page=forbidden, facts_available=True)
    calls = []
    async def run(_self, job, run_id, goal, scope):
        calls.append((job['id'], run_id))
    monkeypatch.setattr(HeadlessFacts, 'run', run)
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        # A previous generation's Stop cannot suppress this explicit new work.
        research['identity_generation'] = 1
        research['fact_research_cancelled'] = True
        research['research_controls'] = {'facts': {'stopped': True, 'identity_generation': 0,
                                                 'photo_sha256': row['photo_sha256']}}
        research['visual_identity'].update(generation=1, photo_sha256=row['photo_sha256'],
                                          selection_basis='owner_exact')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
        if not gps:
            db.execute('UPDATE stories SET latitude=NULL,longitude=NULL WHERE id=?', (sid,))
        jid = service._enqueue_job(db, sid, 'research', 'owner-selected-test', {
            'input_revision': 'owner-request', 'identity_generation': 1,
            'photo_sha256': row['photo_sha256'], 'coverage_goal': 'Source-backed facts'})
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
    await service._run_research(job)
    assert len(calls) == 1
    with service.store.connection() as db:
        story = service._story_repr(db, service._story_row(db, sid))
        assert story['visual_identity']['candidate_name'] == 'Selected house'


@pytest.mark.asyncio
async def test_empty_first_discovery_continues_with_short_address_query(tmp_path):
    service, job, researcher, reader, _ = await fixture(tmp_path)
    queries = []
    async def search(query, story):
        queries.append(query)
        assert 'Do not invent' not in query
        return {'sources': [] if len(queries) == 1 else [{'url': URL, 'title': 'House history'}],
                'outcome': 'completed_empty' if len(queries) == 1 else 'completed', 'receipt': {}}
    researcher.search_articles = search
    with service.store.tx() as db:
        row = service._story_row(db, job['story_id'])
        research = json.loads(row['research_json'])
        research['visual_identity']['search_anchor'] = {'city': 'Калининград', 'street': 'Житомирская',
                                                      'house_number': '22–24'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
    try:
        await HeadlessFacts(service).run(job, 'headless-run', 'Do not invent facts; read sources and preserve all qualifiers', 'history')
        assert len(queries) == 2 and queries[0] != queries[1]
        assert all('Калининград' in query and '22–24' in query and len(query) < 150 for query in queries)
        assert researcher.pages
        assert researcher.identity_contexts[0]['search_anchor']['house_number'] == '22–24'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_initial_warm_poi_facts_are_ready_at_once_and_the_building_research_still_runs(tmp_path):
    import hashlib
    from street_story.fact_ledger import accept_fact_basis, refresh_review_status
    from street_story.poi_memory import hydrate_story_facts, persist_research_memory, sync_poi_review_from_story
    from test_fact_ledger import fact, persist
    service, _, session, _ = make_service(tmp_path)
    original_id = session.resource_id
    mark_identity_ready(service, original_id)
    item = fact('museum', 'Музей открыт в 2005 году.', 'https://source.example/history', 'Museum opened in 2005.')
    fact_id = persist(service.store, original_id, [item], 'original-run', 'original-batch')[0]
    with service.store.tx() as db:
        own = [row[0] for row in db.execute('SELECT evidence_id FROM fact_evidence_spans')]
        accept_fact_basis(db, original_id, fact_id, own, operation={'confirmation_model': 'controlled'}, now=service.store.now())
        refresh_review_status(db, original_id, service.store.now())
        identity = json.loads(service._story_row(db, original_id)['research_json'])['visual_identity']
        persist_research_memory(db, identity, [{**item, 'fact_id': fact_id}], item['sources'], 'original', service.store.now())
        assert sync_poi_review_from_story(db, original_id, service.store.now()) == 1
    photo = b'new-photo-same-poi'
    sid = service.create_story(key='warm-new', client_story_id='warm-new',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)['id']
    mark_identity_ready(service, sid)
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        research['poi_reused_fact_count'] = hydrate_story_facts(db, research['visual_identity'], sid)
        assert research['poi_reused_fact_count'] == 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
        payload = {'input_revision': 'warm-initial', 'identity_generation': 0,
            'photo_sha256': row['photo_sha256'], 'mode': 'initial'}
        jid = service._enqueue_job(db, sid, 'research', 'warm-initial', payload)
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (jid,))
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
    # Remembered facts are ready at once; they are a starting point, not the end of research:
    # the single session still looks for the building's own history (owner decision 2026-10-17).
    assert service.story(sid)['facts'][0]['eligibility'] == 'eligible'
    calls = []
    async def research(*args):
        calls.append(args)
    service.providers.research = SimpleNamespace(research_agent_facts=research)
    await service._run_research(job)
    assert len(calls) == 1
    job['payload_json'] = json.dumps({**payload, 'mode': 'followup', 'input_revision': 'explicit-followup'})
    await service._run_research(job)
    assert len(calls) == 2
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        research['owner_research_waiting_context'] = {'photo_sha256': row['photo_sha256'],
            'generation': 0, 'candidate_id': 'wiki:77'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
    await service._run_research(job)
    assert len(calls) == 2
    with service.store.connection() as db:
        row = service._story_row(db, sid)
        assert row['state'] == 'needs_review'
        assert json.loads(row['research_json'])['research_outcome']['outcome'] == 'awaiting_owner_context'
