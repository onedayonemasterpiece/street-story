"""Controlled provider semantics over the normal public reader and fact ledger."""
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story.errors import RetryableProviderError
from street_story.fact_ledger import set_owner_selection
from street_story.headless_facts import HeadlessFacts
from street_story.providers import GeminiClient
from street_story.research_runs import begin_research_run, chunk_checkpoint, run_manifest
from test_live_editor import make_service, mark_identity_ready

URL = 'https://archive.example/gate-history'
CLAIM = 'The gate opened as a documented museum in 2005.'


@pytest.fixture(autouse=True)
def controlled_public_dns(monkeypatch):
    # Inject only DNS, while the reader still validates/pins a public address
    # and processes the actual bytes supplied by its controlled HTTP transport.
    from street_story import article_media
    original = article_media.cached_public_page

    async def public(host):
        return '8.8.8.8'

    async def acquire(store, client, raw):
        return await original(store, client, raw, resolver=public)

    monkeypatch.setattr(article_media, 'cached_public_page', acquire)


class Researcher:
    def __init__(self):
        self.client = SimpleNamespace(model_id='configured-model')
        self.searches = 0
        self.pages = []
        self.model_units = {}
        self.source_matches = True
        self.content_valid = True
        self.after_extract = None

    async def search_articles(self, query, story):
        self.searches += 1
        assert story['_research_run_id']
        return {'sources': [{'url': URL, 'title': 'Public gate history'}], 'receipt': {'backend': 'controlled-search'}}

    async def extract_fact_page(self, page, story, context):
        self.pages.append(page)
        assert context['confirmed_identity']['candidate_id'] == 'wiki:77'
        assert page['evidence_passages']
        unit = page['_unit_id']
        if unit not in self.model_units:
            self.model_units[unit] = {
                'result': {'facts': [{
                    'claim_key': 'museum-opening', 'existing_fact_id': '', 'text': CLAIM,
                    'confidence': .95, 'passage_ids': [page['evidence_passages'][0]['passage_id']],
                    'source_refs': [], 'evidence_refs': [], 'selected': True,
                    'verdict': 'supported', 'atomic': True, 'support_complete': True,
                    'qualifiers_preserved': True, 'review_reason': 'Controlled supported finding in its own literal passage.',
                }] if CLAIM in page['evidence_passages'][0]['text'] else [],
                    'source_matches_poi': self.source_matches, 'source_content_valid': self.content_valid,
                    'continuation_needed': bool(page.get('has_more_passages'))},
                'receipt': {'backend': 'controlled-text', 'assistants': [{
                    'provider_id': 'controlled', 'model_id': 'actual-controlled-model',
                    'tokens': {'input': 100, 'output': 30}, 'cost': 0,
                }]},
            }
        if self.after_extract:
            self.after_extract(story)
        return self.model_units[unit]


async def fixture(tmp_path, *, text=CLAIM + ' This is an inspectable public article about the history of the gate.'):
    svc, _, session, _ = make_service(tmp_path)
    sid = session.resource_id
    mark_identity_ready(svc, sid)
    researcher = Researcher()
    svc.providers.research = researcher
    fetches = []

    async def transport(request):
        fetches.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/html'}, text='<main><p>' + text + '</p></main>')

    reader = GeminiClient(svc.settings, svc.store)
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    svc.providers.gemini._fetch_page_documents = reader._fetch_page_documents
    with svc.store.tx() as db:
        story = svc._story_row(db, sid)
        jid = svc._enqueue_job(db, sid, 'research', 'controlled-headless-facts', {
            'identity_generation': 0, 'photo_sha256': story['photo_sha256'],
        })
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (jid,))
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
        begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='Find historical facts', scope='history',
                           expected_story_revision=story['revision'], identity_generation=0, run_id='headless-run', now=svc.store.now())
    return svc, job, researcher, reader, fetches


@pytest.mark.asyncio
async def test_no_audio_headless_page_is_immediately_eligible_in_story_and_poi_ledger(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
    try:
        facts = svc.story(job['story_id'])['facts']
        assert len(facts) == 1 and facts[0]['evidence_supported'] and not facts[0]['selected']
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM voice_sessions').fetchone()[0] == 0
            assert db.execute('SELECT eligibility FROM fact_assertions').fetchone()[0] == 'eligible'
            assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'eligible'
            assert db.execute('SELECT model_name FROM fact_observations').fetchone()[0] == 'actual-controlled-model'
            assert db.execute('SELECT COUNT(*) FROM fact_evidence_spans').fetchone()[0] >= 1
            assert run_manifest(db, 'headless-run')['run']['state'] == 'completed'
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.searches == 1 and len(researcher.model_units) == 1 and len(fetches) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_next_attempt_resumes_unread_passages_from_frozen_snapshot(tmp_path):
    text = CLAIM + ' ' + 'Architecture context sentence. ' * 180
    svc, job, researcher, reader, fetches = await fixture(tmp_path, text=text)
    try:
        with pytest.raises(RetryableProviderError):
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        first = researcher.pages[0]
        with svc.store.connection() as db:
            checkpoint = chunk_checkpoint(db, 'headless-run', first['chunk_id'])
            assert checkpoint['next_batch_index'] == 1 and not checkpoint['terminal']
            assert checkpoint['read_passage_ids']
            assert run_manifest(db, 'headless-run')['run']['state'] == 'partial'
        for _ in range(10):
            try:
                await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
            except RetryableProviderError:
                continue
            break
        with svc.store.connection() as db:
            assert run_manifest(db, 'headless-run')['run']['state'] == 'completed'
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
        passage_sets = [{p['passage_id'] for p in page['evidence_passages']} for page in researcher.pages]
        assert len(passage_sets) > 1 and passage_sets[0].isdisjoint(passage_sets[1])
        assert len(fetches) == 1 and researcher.searches == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_completed_scope_reuses_bytes_and_checked_extraction_without_model_calls(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.tx() as db:
            story = svc._story_row(db, job['story_id'])
            begin_research_run(db, story_id=job['story_id'], poi_key='wiki:77', goal='More historical facts', scope='history',
                               expected_story_revision=story['revision'], identity_generation=0, run_id='more-run', now=svc.store.now())
        await HeadlessFacts(svc).run(job, 'more-run', 'More historical facts', 'history')
        with svc.store.connection() as db:
            manifest = run_manifest(db, 'more-run')
            assert manifest['run']['state'] == 'completed' and manifest['counts']['chunks_skipped_completed'] == 1
            assert db.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 1
        assert len(researcher.model_units) == 1 and len(fetches) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_new_scope_adds_support_without_changing_owner_selection_concept_or_draft(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.tx() as db:
            fact_id = db.execute('SELECT fact_id FROM facts').fetchone()[0]
            set_owner_selection(db, job['story_id'], [fact_id], svc.store.now())
            story = svc._story_row(db, job['story_id'])
            research = json.loads(story['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?',
                       ('Owner draft', json.dumps(research), job['story_id']))
            begin_research_run(db, story_id=job['story_id'], poi_key='wiki:77', goal='Museum chronology', scope='museum chronology',
                               expected_story_revision=story['revision'], identity_generation=0, run_id='museum-run', now=svc.store.now())
        await HeadlessFacts(svc).run(job, 'museum-run', 'Museum chronology', 'museum chronology')
        with svc.store.connection() as db:
            assert db.execute('SELECT owner_selected FROM fact_assertions').fetchone()[0] == 1
            story = svc._story_row(db, job['story_id'])
            assert story['draft_text'] == 'Owner draft'
            assert json.loads(story['research_json'])['publication_concept'] == 'Owner concept'
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 2
        assert len(fetches) == 1 and len(researcher.model_units) == 2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['generation', 'stop', 'photo', 'poi'])
async def test_stale_or_stopped_model_result_cannot_commit(tmp_path, guard):
    svc, job, researcher, reader, _ = await fixture(tmp_path)

    def change(story):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            if guard == 'generation':
                research['identity_generation'] = 1
            elif guard == 'stop':
                research['fact_research_cancelled'] = True
            elif guard == 'poi':
                research['visual_identity']['candidate_id'] = 'wiki:88'
            else:
                db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b' * 64, story['id']))
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))

    researcher.after_extract = change
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 0
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_late_owner_edit_is_preserved_and_page_can_retry_with_fresh_recipe(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)

    def edit(story):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=?,revision=revision+1 WHERE id=?',
                       ('Owner draft', json.dumps(research), story['id']))

    researcher.after_extract = edit
    try:
        with pytest.raises(RetryableProviderError, match='research_fact_owner_revision_changed'):
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        assert svc.story(job['story_id'])['facts'] == []
        researcher.after_extract = None
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.tx() as db:
            fact_id = db.execute('SELECT fact_id FROM facts').fetchone()[0]
            set_owner_selection(db, job['story_id'], [fact_id], svc.store.now())
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        assert len(researcher.model_units) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_owner_edit_during_fetch_preserves_acquisition_and_retries_fresh_page(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    original_fetch = svc.providers.gemini._fetch_page_documents

    async def changed_fetch(*args):
        documents = await original_fetch(*args)
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET draft_text=?,revision=revision+1 WHERE id=?', ('Owner draft', job['story_id']))
        return documents

    svc.providers.gemini._fetch_page_documents = changed_fetch
    try:
        with pytest.raises(RetryableProviderError, match='research_fact_owner_revision_changed'):
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert not researcher.pages
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        svc.providers.gemini._fetch_page_documents = original_fetch
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert len(fetches) == 1 and len(researcher.model_units) == 1
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['wrong_poi', 'navigation'])
async def test_wrong_subject_or_unreadable_source_never_imports_claims(tmp_path, invalid):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    researcher.source_matches = invalid != 'wrong_poi'
    researcher.content_valid = invalid != 'navigation'
    try:
        if invalid == 'navigation':
            with pytest.raises(RetryableProviderError):
                await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        else:
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
            if invalid == 'navigation':
                assert run_manifest(db, 'headless-run')['run']['state'] == 'partial'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_known_poi_article_is_read_without_fresh_search_when_search_is_unavailable(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    async def unavailable(*args):
        raise AssertionError('Fresh search must not gate a known unread article')
    researcher.search_articles = unavailable
    with svc.store.tx() as db:
        db.execute('INSERT INTO poi_research_sources(poi_key,url,title,last_query,supports_json,first_seen_at,last_seen_at) '
                   'VALUES(?,?,?,?,?,?,?)', ('wiki:77', URL, 'Remembered article', 'prior query', '[]', svc.store.now(), svc.store.now()))
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.searches == 0 and len(fetches) == 1
        assert svc.story(job['story_id'])['facts'][0]['evidence_supported']
        with svc.store.connection() as db:
            assert run_manifest(db, 'headless-run')['run']['state'] == 'completed'
    finally:
        await reader.search_http.aclose()
