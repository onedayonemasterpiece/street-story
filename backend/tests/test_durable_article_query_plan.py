import json

import pytest

from street_story import article_media, identity_discovery
from street_story.service import canonical
from test_independent_article_priority import gallery
from test_visual_search_continuation import prepared
from visual_queue_fixture import reference_receipt


PLAN = ['Первый переулок здание', 'Вторая улица здание', 'Вторая улица prussia39']


def retain_plan(svc, story):
    snapshot = svc._identity_snapshot(story['id'])[0]
    return identity_discovery._retain_article_discovery(svc, snapshot, [], planned_queries=PLAN,
        query_results={PLAN[0]: {'status': 'completed', 'sources': []}})


def readers(monkeypatch, svc):
    searches, pages = [], []
    async def discover(service, entity_name, visual_query, *, story):
        searches.append(story['_identity_search_query'])
        assert story['_identity_search_query'] == PLAN[1]
        return [{'url': 'https://news.example/second-street', 'discovery_provider': 'opencode'}]
    async def articles(service, snapshot, sources, excluded, *, receipts):
        pages.append(sources[0]['url'])
        receipts.append({'status': 'completed'})
        return [{'candidate_id': 'web:second', 'name': 'Article facade', 'url': sources[0]['url'],
            'reference_image_urls': ['https://news.example/new-facade.jpg'], 'discovery': 'web_article_media'}]
    async def images(batch, limit, *, story_id, evidence):
        c = batch[0]
        evidence.append(reference_receipt(c))
        return [(c['candidate_id'], 'image/jpeg', c['reference_image_urls'][0])]
    monkeypatch.setattr(identity_discovery, 'web_image_sources', discover)
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    svc._candidate_reference_images = images
    return searches, pages


@pytest.mark.asyncio
async def test_second_model_street_query_and_ready_ref_do_not_wait_first_gallery(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    retain_plan(svc, story)
    state = session.state['visual_comparison']
    state['units_since_planned_query'] = 2
    state['sources'] = {'https://news.example/first-gallery': {'source': {'url': 'https://news.example/first-gallery'},
        'status': 'partial', 'attempts': 1, 'retry_at': svc.store.now()+100}}
    searches, pages = readers(monkeypatch, svc)
    reply = await adapter._compare_place_images(session, {})
    assert searches == [PLAN[1]]
    assert pages == ['https://news.example/second-street']
    assert reply['references'][0]['candidate_id'] == 'web:second'
    assert len(state['queue']) == 40
    assert state['sources']['https://news.example/first-gallery']['status'] == 'partial'
    assert svc._identity_snapshot(story['id'])[1]['identity_article_discovery']['queries'][PLAN[1]]['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('cooling', [True, False])
async def test_failed_pages_do_not_starve_untried_plan_when_no_ready_ref(tmp_path, monkeypatch, cooling):
    svc, adapter, story, session = gallery(tmp_path, 0)
    retain_plan(svc, story)
    state = session.state['visual_comparison']
    state['queue'] = []
    # Existing catalog is context only for this test; no other ready physical gallery.
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': []},
            'identity_article_discovery': svc._identity_snapshot(story['id'])[1]['identity_article_discovery']}), story['id']))
    state['sources'] = {'https://news.example/failing': {'source': {'url': 'https://news.example/failing'},
        'status': 'temporary_failure', 'attempts': 2, 'retry_at': svc.store.now()+100 if cooling else 0}}
    searches, pages = readers(monkeypatch, svc)
    reply = await adapter._compare_place_images(session, {})
    assert searches == [PLAN[1]]
    assert pages == ['https://news.example/second-street']
    assert reply['references'][0]['candidate_id'] == 'web:second'


@pytest.mark.asyncio
async def test_restart_hydrates_ordered_plan_and_completed_query_never_repeats(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    retain_plan(svc, story)
    state = session.state['visual_comparison']
    state.update(units_since_planned_query=2, sources={})
    # Completed article cursor survives alongside the initial large gallery.
    for i in range(4):
        url = f'https://ru.wikipedia.org/wiki/Physical_{i}'
        state['sources'][url] = {'source': {'url': url}, 'status': 'completed', 'attempts': 1}
    state.update(lease_owner=session.id, lease_until=svc.store.now()+300, control_revision=0)
    with svc.store.tx() as db:
        row = db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()
        research = json.loads(row[0])
        research['visual_search_operation'] = state
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    session.state.clear()
    searches, _pages = readers(monkeypatch, svc)
    await adapter._compare_place_images(session, {})
    assert searches == [PLAN[1]]
    assert session.state['visual_comparison']['planned_queries'] == PLAN
    cached = await adapter._find_place_articles(session, {'query': PLAN[1]})
    assert cached['status'] == 'completed' and searches == [PLAN[1]]
    assert identity_discovery.next_visual_query({}, 'Other object', {PLAN[0]: {'status': 'completed'},
        PLAN[1]: {'status': 'completed'}}, PLAN) == PLAN[2]


@pytest.mark.asyncio
async def test_recovery_persists_plan_before_first_search_and_records_actual_completed_query(tmp_path, monkeypatch):
    svc, _adapter, story, _session = prepared(tmp_path)
    svc.providers.gemini._generate = lambda: None
    svc.providers.gemini.executor = object()
    async def suggest(service, snapshot, transcript, candidates):
        snapshot['_identity_article_queries'] = PLAN
        return 'Unproved object', [], 'facade features', ''
    calls = []
    async def search(service, entity, visual, *, story):
        history = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']
        assert history['planned_queries'] == PLAN
        calls.append(story['_identity_search_query'])
        return [{'url': 'https://news.example/first', 'discovery_provider': 'opencode'}]
    async def articles(service, snapshot, sources, excluded, *, receipts, first_ready):
        assert first_ready
        receipts.append({'url': sources[0]['url'], 'status': 'completed'})
        return [{'candidate_id': 'web:first', 'url': sources[0]['url'], 'discovery': 'web_article_media',
                 'reference_image_urls': ['https://news.example/first.jpg']}]
    monkeypatch.setattr(identity_discovery, 'suggest', suggest)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    snapshot = svc._identity_snapshot(story['id'])[0]
    result = await identity_discovery.recover(svc, snapshot, '', [], set())
    assert result and calls == [PLAN[0]]
    history = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']
    assert history['planned_queries'] == PLAN
    assert history['queries'][PLAN[0]]['status'] == 'completed'
    assert PLAN[1] not in history['queries']
    assert identity_discovery.next_visual_query({}, 'Wrong old seed', history['queries'], history['planned_queries']) == PLAN[1]


@pytest.mark.asyncio
async def test_in_progress_query_claim_survives_restart_and_excludes_duplicate_send(tmp_path, monkeypatch):
    svc, adapter, story, sessions = prepared(tmp_path)
    retain_plan(svc, story)
    snapshot = svc._identity_snapshot(story['id'])[0]
    token, first = identity_discovery._claim_article_query(svc, snapshot, PLAN[1])
    assert token and first['status'] == 'in_progress'
    repeated, saved = identity_discovery._claim_article_query(svc, snapshot, PLAN[1].upper())
    assert repeated is None and saved['claim_id'] == token
    async def forbidden(*args, **kwargs):
        pytest.fail('An in-progress query is addressed by its existing provider receipt, never resent')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    observed = await adapter._find_place_articles(sessions(), {'query': PLAN[1]})
    assert observed['claim_id'] == token and observed['status'] == 'unknown'
    assert observed['code'] == 'research_article_query_dispatch_unknown'
    history = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']
    assert identity_discovery.next_visual_query({}, '', history['queries'], history['planned_queries']) == PLAN[2]


def test_stopped_generation_cannot_claim_new_planned_query(tmp_path):
    from street_story.service import ConflictError
    svc, _adapter, story, _sessions = prepared(tmp_path)
    snapshot = svc._identity_snapshot(story['id'])[0]
    with svc.store.tx() as db:
        research = json.loads(snapshot['research_json'])
        research['identity_generation'] = 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    with pytest.raises(ConflictError):
        identity_discovery._claim_article_query(svc, snapshot, PLAN[1])
    assert not svc._identity_snapshot(story['id'])[1].get('identity_article_discovery')


@pytest.mark.asyncio
async def test_query_completion_does_not_overwrite_concurrent_other_query_result(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 2)
    retain_plan(svc, story)
    state = session.state['visual_comparison']
    state.update(units_since_planned_query=2, sources={})
    readers(monkeypatch, svc)
    actual_search = identity_discovery.web_image_sources
    newer = {'status': 'completed', 'sources': [{'url': 'https://news.example/concurrent-result'}]}
    async def search(*args, **kwargs):
        found = await actual_search(*args, **kwargs)
        identity_discovery._retain_article_discovery(svc, svc._identity_snapshot(story['id'])[0], [],
            query_results={PLAN[0]: newer})
        return found
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    await adapter._compare_place_images(session, {})
    history = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']
    assert history['queries'][PLAN[0]] == newer
    assert history['queries'][PLAN[1]]['status'] == 'completed'


@pytest.mark.asyncio
async def test_original_unknown_visual_pending_precedes_new_query_plan(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 0)
    async def images(batch, limit, *, story_id, evidence):
        c = batch[0]
        evidence.append(reference_receipt(c))
        return [(c['candidate_id'], 'image/jpeg', c['reference_image_urls'][0])]
    svc._candidate_reference_images = images
    first = await adapter._compare_place_images(session, {})
    retain_plan(svc, story)
    session.state['visual_comparison']['units_since_planned_query'] = 2
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            ('pending-attempt', 'pending-logical', story['id'], 'vision', canonical({
                'phase': 'unknown', 'comparison_id': first['comparison_id'],
                'binding': {'visual_scope': True, 'generation': 0}}), svc.store.now(), svc.store.now()))
    async def forbidden(*args, **kwargs):
        pytest.fail('Original pending UNKNOWN takes precedence over every new planned query')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    monkeypatch.setattr(article_media, 'article_candidates', forbidden)
    repeated = await adapter._compare_place_images(session, {})
    assert repeated['comparison_id'] == first['comparison_id']
    with svc.store.connection() as db:
        saved = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', ('pending-attempt',)).fetchone()[0])
    assert saved['phase'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['submitted', 'completed'])
async def test_restart_in_progress_query_reads_exact_original_attempt_without_new_route(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace
    from street_story.service import digest
    svc, adapter, story, sessions = prepared(tmp_path)
    retain_plan(svc, story)
    snapshot = svc._identity_snapshot(story['id'])[0]
    token, _ = identity_discovery._claim_article_query(svc, snapshot, PLAN[1])
    logical = digest([story['id'], snapshot['photo_sha256'], 0, 'search', canonical([PLAN[1], None])])
    original = {'phase': phase, 'session_id': 'original-session', 'message_id': 'original-message',
        'sources': [{'url': 'https://news.example/readback'}]}
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            ('original-query-attempt', logical, story['id'], 'search', canonical(original), svc.store.now(), svc.store.now()))
    calls = []
    async def readback(query, addressed):
        calls.append(query)
        assert addressed['_identity_generation'] == 0
        with svc.store.connection() as db:
            receipt = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', ('original-query-attempt',)).fetchone()[0])
        assert receipt['session_id'] == 'original-session' and receipt['message_id'] == 'original-message'
        return {'sources': original['sources'], 'receipt': {**original, 'phase': 'completed'}}
    svc.providers.research = SimpleNamespace(search_articles=readback)
    async def forbidden(*args, **kwargs):
        pytest.fail('Resume must use original provider address, never a fresh web route/fallback')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    result = await adapter._find_place_articles(sessions(), {'query': PLAN[1]})
    assert result['status'] == 'completed' and result['claim_id'] == token
    assert result['sources'] == original['sources']
    assert calls == ([PLAN[1]] if phase == 'submitted' else [])
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts WHERE story_id=?', (story['id'],)).fetchone()[0] == 1
