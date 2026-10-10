"""Controlled provider semantics over the normal public reader and fact ledger."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from street_story.errors import RetryableProviderError
from street_story.fact_ledger import set_owner_selection
from street_story.headless_facts import HeadlessFacts, reviewed_reference_articles
from street_story.providers import GeminiClient
from street_story.research_runs import (
    begin_research_run,
    chunk_checkpoint,
    run_manifest,
)
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
        self.expected_identity = 'wiki:77'
        self.identity_contexts = []

    async def search_articles(self, query, story):
        self.searches += 1
        assert story['_research_run_id']
        return {'sources': [{'url': URL, 'title': 'Public gate history'}], 'receipt': {'backend': 'controlled-search'}}

    async def extract_fact_page(self, page, story, context):
        self.pages.append(page)
        self.identity_contexts.append(context['confirmed_identity'])
        assert context['confirmed_identity']['candidate_id'] == self.expected_identity
        assert page['evidence_passages']
        supporting = next((p for p in page['evidence_passages'] if CLAIM in p['text']), None)
        unit = page['_unit_id']
        if unit not in self.model_units:
            self.model_units[unit] = {
                'result': {'facts': [{
                    'claim_key': 'museum-opening', 'existing_fact_id': '', 'text': CLAIM,
                    'confidence': .95, 'passage_ids': [supporting['passage_id']] if supporting else [],
                    'source_refs': [], 'evidence_refs': [], 'selected': True,
                    'verdict': 'supported', 'atomic': True, 'support_complete': True,
                    'qualifiers_preserved': True, 'review_reason': 'Controlled supported finding in its own literal passage.',
                }] if supporting else [],
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
@pytest.mark.parametrize('autonomous', [True, False])
async def test_autonomous_core_read_does_not_borrow_interactive_reply_byte_ceiling(tmp_path, monkeypatch, autonomous):
    text = CLAIM + ' ' + 'Architecture context sentence. ' * 180
    svc, job, researcher, reader, _ = await fixture(tmp_path, text=text)
    facts = HeadlessFacts(svc)
    original = facts.adapter._read_frozen_research_chunk
    reads = []
    def read(session, *args):
        session.state['headless_research'] = autonomous
        page = original(session, *args)
        reads.append((session.state.get('headless_research'), page['has_more_passages'], len(page['evidence_passages'])))
        return page
    monkeypatch.setattr(facts.adapter, '_read_frozen_research_chunk', read)
    try:
        try:
            await facts.run(job, 'headless-run', 'Find historical facts', 'history')
        except RetryableProviderError:
            pass  # Candidates still require the independent review operation.
        assert reads and all(flag == autonomous and more != autonomous for flag, more, _ in reads), reads
        assert all(page['has_more_passages'] != autonomous for page in researcher.pages)
        assert sum(count for _, _, count in reads) > 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_autonomous_frozen_core_includes_useful_tail_in_first_extraction(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path,
        text=('Page navigation without historical findings. ' * 85) + CLAIM)
    facts = HeadlessFacts(svc)
    try:
        await facts.run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.pages[0]['has_more_passages'] is False
        assert researcher.model_units[researcher.pages[0]['_unit_id']]['result']['facts'][0]['text'] == CLAIM
        assert svc.story(job['story_id'])['facts'][0]['eligibility'] == 'unreviewed'
        for _ in range(4):
            try:
                outcome = await facts.run(job, 'headless-run', 'Find historical facts', 'history')
            except RetryableProviderError as waiting:
                assert waiting.retry_at == pytest.approx(svc.store.now() + 1, abs=.1)
                continue
            assert outcome is None or outcome['coverage_complete'] is False
            with svc.store.connection() as db:
                assert not db.execute("SELECT 1 FROM research_chunk_runs WHERE run_id=? "
                    "AND status NOT IN ('extracted','no_claims')", ('headless-run',)).fetchone()
            break
        else:
            pytest.fail('All actual pages must exhaust finitely')
        assert len(fetches) == 1
        assert len(researcher.pages) == len({page['_unit_id'] for page in researcher.pages})
        assert [page['batch_index'] for page in researcher.pages] == list(range(len(researcher.pages)))
        facts_rows = svc.story(job['story_id'])['facts']
        assert len(facts_rows) == 1 and facts_rows[0]['text'] == CLAIM
        with svc.store.connection() as db:
            assert db.execute('SELECT eligibility FROM fact_assertions WHERE story_id=?',
                              (job['story_id'],)).fetchone()[0] == 'unreviewed'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unknown_core_keeps_original_wait_and_does_not_use_ready_continuation(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path, text='Unread public article. ' * 200)
    calls = []

    async def unknown(page, story, context):
        calls.append(page['_unit_id'])
        raise RetryableProviderError('original_response_unknown')

    researcher.extract_fact_page = unknown
    try:
        for _ in range(2):
            with pytest.raises(RetryableProviderError) as waiting:
                await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
            assert waiting.value.retry_at >= svc.store.now() + 299
        assert len(calls) == 1 and svc.story(job['story_id'])['facts'] == []
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('role,expected_terminal', [('facts_live', True), ('facts', False)])
async def test_only_closed_unobservable_live_unknown_finishes_without_resending(tmp_path, role, expected_terminal):
    from street_story.research_adapter import ProductResearchAdapter, canonical
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    calls = []

    async def unknown(page, story, context):
        calls.append(page['_unit_id'])
        binding, _ = ProductResearchAdapter.attempt(SimpleNamespace(service=svc), story, role, page['_unit_id'])
        receipt = {'binding': binding, 'phase': 'unknown', 'provider_send_state': 'submitted',
                   'provider_id': 'google-live' if role == 'facts_live' else 'opencode',
                   'error_code': 'live_research_timeout' if role == 'facts_live' else 'original_response_unknown',
                   'text_sends': 1, 'session_id': 'closed-live' if role == 'facts_live' else 'durable-thread'}
        with svc.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                       (canonical(receipt), binding['attempt_id']))
        raise RetryableProviderError('original_response_unknown')

    researcher.extract_fact_page = unknown
    try:
        for _ in range(2):
            if expected_terminal:
                result = await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
                assert result['outcome'] == 'resource_blocked'
                assert result['reason'] == 'live_research_original_outcome_unavailable'
            else:
                with pytest.raises(RetryableProviderError):
                    await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert len(calls) == 1 and svc.story(job['story_id'])['facts'] == []
        with svc.store.connection() as db:
            receipt = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0])
            assert receipt['phase'] == 'unknown' and receipt['text_sends'] == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_frozen_fact_preparation_does_not_starve_live_receipts(tmp_path, monkeypatch):
    svc, job, researcher, reader, _ = await fixture(tmp_path, text=(CLAIM + ' Historical detail.\n') * 400)
    facts = HeadlessFacts(svc)
    try:
        facts._bind_discovery(job, 'headless-run', 'Find historical facts', 'history',
            [{'url': URL, 'title': 'Public history'}], {}, 0)
        await facts._prepare_units(job, 'headless-run', 'configured-model', 0)
        original = facts.adapter._get_research_chunk
        prepared_at_receipt_count = []
        receipts = 0
        running = True

        async def live_receipts():
            nonlocal receipts
            while running:
                receipts += 1
                await asyncio.sleep(0)

        async def cached_page(*args, **kwargs):
            # Cached calls use real frozen pages and may return without yielding.
            prepared_at_receipt_count.append(receipts)
            return await original(*args, **kwargs)

        monkeypatch.setattr(facts.adapter, '_get_research_chunk', cached_page)
        heartbeat = asyncio.create_task(live_receipts())
        try:
            await facts._prepare_units(job, 'headless-run', 'configured-model', 0)
        finally:
            running = False
            await heartbeat
        assert len(prepared_at_receipt_count) >= 2
        assert all(b > a for a, b in zip(prepared_at_receipt_count, prepared_at_receipt_count[1:]))
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_one_slow_frozen_page_keeps_native_audio_receipts_running(tmp_path, monkeypatch):
    import time
    svc, job, _, reader, _ = await fixture(tmp_path, text=(CLAIM + ' Historical detail.\n') * 80)
    facts = HeadlessFacts(svc)
    try:
        facts._bind_discovery(job, 'headless-run', 'Find historical facts', 'history',
            [{'url': URL, 'title': 'Public history'}], {}, 0)
        await facts._prepare_units(job, 'headless-run', 'configured-model', 0)
        original = facts.adapter._core_passages
        receipts = 0
        running = True
        progress_during_page = []

        async def native_receipts():
            nonlocal receipts
            while running:
                receipts += 1
                await asyncio.sleep(.005)

        def slow_page(*args, **kwargs):
            before = receipts
            # Controlled synchronous reader/projection cost, not a provider mock.
            time.sleep(.08)
            result = original(*args, **kwargs)
            progress_during_page.append(receipts - before)
            return result

        monkeypatch.setattr(facts.adapter, '_core_passages', slow_page)
        heartbeat = asyncio.create_task(native_receipts())
        try:
            await facts._prepare_units(job, 'headless-run', 'configured-model', 0)
        finally:
            running = False
            await heartbeat
        assert progress_during_page and all(n > 0 for n in progress_during_page)
    finally:
        await reader.search_http.aclose()


async def review_candidates(svc, sid, run_id):
    """Controlled Live semantic decisions, distinct from extractor candidates."""
    with svc.store.connection() as db:
        if run_manifest(db, run_id)['run']['state'] == 'completed':
            return
    adapter = HeadlessFacts(svc).adapter
    session = SimpleNamespace(id='live_1234567890abcdef', resource_id=sid,
                              model='gemini-3.8-live', state={})
    number = 0
    while True:
        with svc.store.connection() as db:
            pending = db.execute("SELECT 1 FROM fact_assertions WHERE story_id=? AND eligibility='unreviewed' LIMIT 1", (sid,)).fetchone()
        if not pending:
            break
        page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'allow_partial_review': True}})
        rows = []
        while True:
            rows.extend(page['items'])
            if not page['has_more']:
                break
            page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': page['next_args']})
        decisions = []
        for f in sorted({item['fact'] for item in rows}):
            item = next(row for row in rows if row['fact'] == f)
            # The controlled reader fixture records each proposition literally.
            assert item['text'] in ''.join(row['passage'] for row in rows if row['fact'] == f)
            decision = {'fact': f, 'verdict': 'supported', 'evidence': [item['evidence']],
                        'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                        'claims': [item['text']], 'basis_quotes': [item['text']],
                        'reason': 'Controlled Live review checked this exact proposition against its own frozen passage.'}
            duplicate = next((claim for claim in page.get('nearby_existing_claims', []) if claim['text'] == item['text']), None)
            if duplicate:
                decision['equivalent_to_existing'] = duplicate['fact_id']
            decisions.append(decision)
        result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': f'controlled-live-{run_id}-{number}', 'args': {
            'packet_ref': page['packet_ref'], 'decisions': decisions, 'relations_complete': True,
            'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}})
        assert result['unreviewed_count'] < 100 and result['complete'] is False
        number += 1
    # Closing an extraction scope is a separate explicit Live coverage decision.
    facts = adapter._get_facts(sid, {'eligibility': 'all', 'limit': 50})['facts']
    if any(f['eligibility'] == 'withheld' for f in facts):
        # Duplicate candidate arbitration stays partial; never revive withheld rows.
        return
    return await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': f'controlled-coverage-{run_id}', 'args': {
        'run_id': run_id, 'reviewed_assertions': [
            {'fact_id': f['fact_id'], 'revision_digest': f['revision_digest'],
             'supporting_evidence_ids': [e['evidence_id'] for e in adapter._get_evidence(sid, {'fact_ids': [f['fact_id']]})['evidence']]}
            for f in facts if f['eligibility'] == 'eligible'],
        'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}})


@pytest.mark.asyncio
@pytest.mark.parametrize('sufficient,verified', [(True, True), (False, True), (True, False)])
@pytest.mark.parametrize('decision_origin', ['extractor', 'reviewer'])
async def test_model_sufficient_reviewed_result_finishes_before_independent_unknown_tail(tmp_path, monkeypatch, sufficient, verified, decision_origin):
    from test_headless_fact_review_parallel import ControlledReview
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    harness = HeadlessFacts(svc)
    tail_started, tail_release, reviewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = researcher.extract_fact_page
    calls = []
    async def search(query, story):
        return {'sources': [{'url': URL, 'title': 'Own history'},
                            {'url': URL + '-tail', 'title': 'Optional further history'}],
                'receipt': {'backend': 'controlled-search'}}
    async def extract(page, story, context):
        calls.append(page['_unit_id'])
        if page['_extractor_ordinal'] == 1:
            tail_started.set()
            await tail_release.wait()
        else:
            await tail_started.wait()
        value = await original(page, story, context)
        value['result']['research_sufficient'] = sufficient if decision_origin == 'extractor' else False
        value['result']['research_sufficient_basis'] = {'candidate_indices': [0], 'known_fact_ids': []}
        return value
    researcher.search_articles, researcher.extract_fact_page = search, extract
    ControlledReview.mode = 'positive'
    class Review(ControlledReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            args = await super()._infer(packet, job, unit, saved, ordinal)
            if decision_origin == 'reviewer':
                args.update(research_sufficient=sufficient,
                    research_sufficient_basis={'candidate_indices': [0], 'known_fact_ids': []})
            return args
    engine = Review(harness)
    async def review(job, run_id, revision, **kwargs):
        count = await engine.run(job, run_id, revision, **kwargs) if verified else 0
        reviewed.set()
        return count
    monkeypatch.setattr(harness, '_review_candidates', review)
    task = asyncio.create_task(harness.run(job, 'headless-run', 'Find historical facts', 'history'))
    try:
        await asyncio.wait_for(reviewed.wait(), 10)
        if sufficient and verified:
            outcome = await asyncio.wait_for(task, 10)
            assert not tail_release.is_set()
            assert outcome['outcome'] == 'useful_partial' and outcome['reason'] == 'model_goal_sufficient'
            assert outcome['coverage_complete'] is False and outcome['eligible_count'] == 1
            with svc.store.connection() as db:
                phases = [json.loads(r[0])['phase'] for r in db.execute(
                    "SELECT value_json FROM research_checkpoints WHERE stage LIKE 'headless_fact_unit:%'")]
            assert 'unknown' in phases
            assert await harness.run(job, 'headless-run', 'Find historical facts', 'history') == outcome
            assert len(calls) == 2  # Reopening never resends the cancelled original tail.
        else:
            assert not task.done()
            tail_release.set()
            if verified:
                await asyncio.wait_for(task, 10)
            else:
                with pytest.raises(RetryableProviderError, match='research_fact_review_partial'):
                    await asyncio.wait_for(task, 10)
            with svc.store.connection() as db:
                detail = db.execute('SELECT status_detail FROM research_runs WHERE run_id=?', ('headless-run',)).fetchone()[0]
            assert detail != 'model_goal_sufficient'
    finally:
        tail_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('basis', ['explicit', 'legacy', 'invalid'])
async def test_sufficiency_waits_for_exact_model_basis_after_first_review(tmp_path, monkeypatch, basis):
    from test_headless_fact_review_parallel import ControlledReview
    texts = [CLAIM, 'The gate has an independently documented stone arch.',
             'The gate housed a documented local exhibition in 2010.']
    svc, job, researcher, reader, _ = await fixture(tmp_path, text=' '.join(texts))
    harness = HeadlessFacts(svc)
    tail_started, tail_release = asyncio.Event(), asyncio.Event()
    first_checked, later_review = asyncio.Event(), asyncio.Event()
    original = researcher.extract_fact_page
    async def search(query, story):
        return {'sources': [{'url': URL, 'title': 'Own history'},
                            {'url': URL + '-tail', 'title': 'Independent optional history'}]}
    async def extract(page, story, context):
        if page['_extractor_ordinal'] == 1:
            tail_started.set()
            await tail_release.wait()
        else:
            await tail_started.wait()
        value = await original(page, story, context)
        seed = value['result']['facts'][0]
        value['result']['facts'] = [{**seed, 'text': text} for text in texts]
        value['result']['research_sufficient'] = True
        if basis != 'legacy':
            value['result']['research_sufficient_basis'] = {
                'candidate_indices': [0, 1, 2] if basis == 'explicit' else [31], 'known_fact_ids': []}
        return value
    researcher.search_articles, researcher.extract_fact_page = search, extract
    class Review(ControlledReview):
        MAX_PACKET_FACTS = 1
        calls_here = 0
        async def _infer(self, *args, **kwargs):
            self.calls_here += 1
            if self.calls_here > 1:
                await later_review.wait()
            return await super()._infer(*args, **kwargs)
    engine = Review(harness)
    monkeypatch.setattr(harness, '_review_candidates', engine.run)
    check = harness._model_sufficient
    def observed(*args):
        answer = check(*args)
        with svc.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]
        if count == 1:
            assert answer is False  # One closed packet is not the model's three-claim basis.
            first_checked.set()
        return answer
    monkeypatch.setattr(harness, '_model_sufficient', observed)
    task = asyncio.create_task(harness.run(job, 'headless-run', 'Find historical facts', 'history'))
    try:
        await asyncio.wait_for(first_checked.wait(), 10)
        assert not task.done() and not tail_release.is_set()
        later_review.set()
        if basis == 'invalid':
            tail_release.set()
        outcome = await asyncio.wait_for(task, 10)
        if basis != 'invalid':
            assert outcome['reason'] == 'model_goal_sufficient' and outcome['eligible_count'] == 3
            assert not tail_release.is_set()
        else:
            assert outcome['reason'] != 'model_goal_sufficient'
    finally:
        tail_release.set()
        later_review.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_no_audio_headless_page_requires_live_review_in_story_and_poi_ledger(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
    try:
        facts = svc.story(job['story_id'])['facts']
        assert len(facts) == 1 and facts[0]['eligibility'] == 'unreviewed' and not facts[0]['selected']
        assert facts[0]['supporting_evidence_keys']
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM voice_sessions').fetchone()[0] == 0
            assert db.execute('SELECT eligibility FROM fact_assertions').fetchone()[0] == 'unreviewed'
            assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'unreviewed'
            assert db.execute('SELECT model_name FROM fact_observations').fetchone()[0] == 'actual-controlled-model'
            assert db.execute('SELECT COUNT(*) FROM fact_evidence_spans').fetchone()[0] >= 1
            assert run_manifest(db, 'headless-run')['run']['state'] == 'verifying'
        await review_candidates(svc, job['story_id'], 'headless-run')
        with svc.store.connection() as db:
            assert db.execute('SELECT eligibility FROM fact_assertions').fetchone()[0] == 'eligible'
            assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'eligible'
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert svc.story(job['story_id'])['facts'][0]['supporting_evidence_keys'] == facts[0]['supporting_evidence_keys']
        assert researcher.searches == 1 and len(researcher.model_units) == 1 and len(fetches) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_next_attempt_reuses_completed_autonomous_core_without_source_or_model_resend(tmp_path):
    text = CLAIM + ' ' + 'Architecture context sentence. ' * 180
    svc, job, researcher, reader, fetches = await fixture(tmp_path, text=text)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        first = researcher.pages[0]
        with svc.store.connection() as db:
            checkpoint = chunk_checkpoint(db, 'headless-run', first['chunk_id'])
            assert checkpoint['next_batch_index'] == 1 and checkpoint['terminal']
            assert checkpoint['read_passage_ids']
            assert run_manifest(db, 'headless-run')['run']['state'] == 'verifying'
        for _ in range(10):
            try:
                await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
            except RetryableProviderError:
                continue
            break
        with svc.store.connection() as db:
            assert run_manifest(db, 'headless-run')['run']['state'] == 'verifying'
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
        passage_sets = [{p['passage_id'] for p in page['evidence_passages']} for page in researcher.pages]
        assert len(passage_sets) == 1 and len(passage_sets[0]) > 2
        assert len(fetches) == 1 and researcher.searches == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_completed_scope_reuses_bytes_and_checked_extraction_without_model_calls(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        await review_candidates(svc, job['story_id'], 'headless-run')
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
        await review_candidates(svc, job['story_id'], 'headless-run')
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
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 2
        assert len(fetches) == 1 and len(researcher.model_units) == 2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['generation', 'stop', 'photo', 'poi', 'control'])
@pytest.mark.parametrize('proof_kind', ['owner', 'geometry'])
async def test_stale_or_stopped_model_result_cannot_commit(tmp_path, guard, proof_kind):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    if proof_kind == 'geometry':
        from test_geometry_subject_articles import geometry_identity
        researcher.expected_identity = 'osm:way:7'
        with svc.store.tx() as db:
            row = svc._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            research['visual_identity'] = geometry_identity(photo=row['photo_sha256'], generation=0)
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
            db.execute("UPDATE research_runs SET poi_key='osm:way:7' WHERE run_id='headless-run'")

    def change(story):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            if guard == 'generation':
                research['identity_generation'] = 1
            elif guard == 'stop':
                research['fact_research_cancelled'] = True
            elif guard == 'poi':
                research['visual_identity']['candidate_id'] = 'wiki:88'
            elif guard == 'control':
                research.setdefault('research_controls', {}).setdefault('facts', {})['revision'] = 1
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
async def test_late_owner_edit_defers_durable_result_without_resending(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)

    def edit(story):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=?,revision=revision+1 WHERE id=?',
                       ('Owner draft', json.dumps(research), story['id']))

    researcher.after_extract = edit
    try:
        with pytest.raises(RetryableProviderError, match='research_fact_source_coverage_partial'):
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        assert svc.story(job['story_id'])['facts'] == []
        researcher.after_extract = None
        with pytest.raises(RetryableProviderError, match='research_fact_source_coverage_partial'):
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert svc.story(job['story_id'])['facts'] == []
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        assert len(researcher.pages) == len(researcher.model_units) == 1
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
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert svc.story(job['story_id'])['draft_text'] == 'Owner draft'
        # Acquisition completes before the extractor freezes its owner fence.
        assert len(researcher.pages) == 1
        assert svc.story(job['story_id'])['facts'][0]['eligibility'] == 'unreviewed'
        svc.providers.gemini._fetch_page_documents = original_fetch
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
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
            if invalid == 'navigation':
                assert run_manifest(db, 'headless-run')['run']['state'] == 'verifying'
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
        assert svc.story(job['story_id'])['facts'][0]['eligibility'] == 'unreviewed'
        with svc.store.connection() as db:
            assert run_manifest(db, 'headless-run')['run']['state'] == 'verifying'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('proved,subject,searches', [(True, 'wiki:77', 0), (False, 'wiki:77', 1), (True, 'wiki:neighbor', 1)])
async def test_verified_identity_article_starts_closed_client_facts_without_redundant_search(tmp_path, proved, subject, searches):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    try:
        with svc.store.tx() as db:
            story = svc._story_row(db, job['story_id'])
            research = json.loads(story['research_json'])
            research['visual_identity'].update(status='match', visual_reference_verified=proved,
                reference_evidence=[{'article_url': URL, 'reference_id': 'ref-real',
                    'subject_candidate_id': subject, 'source_url': 'https://archive.example/exterior.jpg'}])
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), job['story_id']))
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.searches == searches
        assert researcher.pages and fetches
        with svc.store.connection() as db:
            # An acquisition receipt cannot make extractor candidates eligible.
            assert db.execute("SELECT count(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM live_messages').fetchone()[0] == 0
        await review_candidates(svc, job['story_id'], 'headless-run')
        with svc.store.connection() as db:
            assert db.execute("SELECT count(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mapping_change,searches', [({}, 0),
    ({'reference_id': 'another-image'}, 1), ({'candidate_id': 'wiki:neighbor'}, 1),
    ({'source_url': 'https://archive.example/another.jpg'}, 1)])
async def test_direct_comparison_page_mapping_is_joined_only_to_reviewed_image(tmp_path, mapping_change, searches):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    try:
        image = 'https://archive.example/exterior.jpg'
        mapping = {'reference_id': 'ref-real', 'candidate_id': 'wiki:77',
                   'source_url': image, 'article_url': URL, **mapping_change}
        with svc.store.tx() as db:
            story = svc._story_row(db, job['story_id'])
            research = json.loads(story['research_json'])
            research['visual_identity'].update(status='match', visual_reference_verified=True,
                reference_evidence=[{'reference_id': 'ref-real', 'candidate_id': 'wiki:77', 'image_url': image}],
                provider_receipt={'reference_mapping': [mapping]})
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), job['story_id']))
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.searches == searches and fetches
        with svc.store.connection() as db:
            assert db.execute("SELECT count(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM live_messages').fetchone()[0] == 0
    finally:
        await reader.search_http.aclose()
def test_retained_direct_comparison_shape_recovers_its_actual_article():
    # Reduced real persisted identity: public reference provenance only,
    # no original image, GPS, credentials or provider prompt.
    identity = json.loads((Path(__file__).parent / 'fixtures' / 'reviewed-reference-article.json').read_text())
    expected = identity['provider_receipt']['reference_mapping'][0]['article_url']
    assert list(reviewed_reference_articles(identity)) == [expected]
    identity['receipt'] = identity.pop('provider_receipt')
    assert reviewed_reference_articles(identity) == {}


@pytest.mark.asyncio
async def test_completed_empty_discovery_finishes_without_polling_or_resending(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    async def empty(query, story):
        researcher.searches += 1
        return {'sources': [], 'outcome': 'completed_empty', 'receipt': {'backend': 'controlled-search'}}
    researcher.search_articles = empty
    try:
        harness = HeadlessFacts(svc)
        outcome = await harness.run(job, 'headless-run', 'Find historical facts', 'history')
        assert outcome == {'outcome': 'no_supported_facts', 'reason': 'search_exhausted',
                           'coverage_complete': False, 'eligible_count': 0}
        assert await harness.run(job, 'headless-run', 'Find historical facts', 'history') == outcome
        assert researcher.searches == 1 and not researcher.pages
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('displaced', [False, True])
async def test_saved_response_after_lease_expiry_commits_immediately_only_for_same_owner_fence(tmp_path, displaced):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    def expire(story):
        with svc.store.tx() as db:
            db.execute('UPDATE research_chunk_runs SET lease_until=0 WHERE run_id=?', ('headless-run',))
            if displaced:
                db.execute("UPDATE research_chunk_runs SET lease_owner='new-owner',lease_fence=lease_fence+1 WHERE run_id=?",
                           ('headless-run',))
    researcher.after_extract = expire
    try:
        try:
            await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        except RetryableProviderError:
            assert displaced
        assert len(svc.story(job['story_id'])['facts']) == (0 if displaced else 1)
        assert len(researcher.pages) == 1
        with svc.store.connection() as db:
            saved = db.execute("SELECT 1 FROM research_checkpoints WHERE stage LIKE 'headless_fact_result:%'").fetchone()
            assert saved
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_result_checkpoint_crash_retains_original_owner_fence_before_replay(tmp_path, monkeypatch):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    harness = HeadlessFacts(svc)
    original_put = svc.store.checkpoint_put
    def lose_result(job_id, stage, value):
        if stage.startswith('headless_fact_result:'):
            raise RuntimeError('controlled checkpoint interruption')
        return original_put(job_id, stage, value)
    def edit(story):
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET draft_text=?,revision=revision+1 WHERE id=?',
                       ('Changed owner draft', story['id']))
    researcher.after_extract = edit
    monkeypatch.setattr(harness, '_boundary_closed', lambda *_: True)
    monkeypatch.setattr(svc.store, 'checkpoint_put', lose_result)
    try:
        with pytest.raises(RetryableProviderError):
            await harness.run(job, 'headless-run', 'Find historical facts', 'history')
        monkeypatch.setattr(svc.store, 'checkpoint_put', original_put)
        researcher.after_extract = None
        with pytest.raises(RetryableProviderError):
            await harness.run(job, 'headless-run', 'Find historical facts', 'history')
        assert len(researcher.pages) == 1
        assert svc.story(job['story_id'])['facts'] == []
        assert svc.story(job['story_id'])['draft_text'] == 'Changed owner draft'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('source_matches', [True, False])
@pytest.mark.parametrize('proof_kind', ['geometry', 'architectural_text'])
async def test_geometry_without_ref_uses_subject_article_and_normal_canonical_review(tmp_path, source_matches, proof_kind):
    from test_geometry_subject_articles import geometry_identity
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    researcher.expected_identity = 'osm:way:7'
    researcher.source_matches = source_matches
    with svc.store.tx() as db:
        row = svc._story_row(db, job['story_id'])
        research = json.loads(row['research_json'])
        if proof_kind == 'architectural_text':
            from test_architectural_text_identity import architectural_identity
            identity = architectural_identity(photo=row['photo_sha256'], generation=0, url=URL)
        else:
            identity = geometry_identity(photo=row['photo_sha256'], generation=0, observed_buildings=120)
            assert len(json.dumps(identity['geometry_proof'])) > 24000
        proof = identity.get('geometry_proof') or identity['architectural_text_proof']
        identity['candidates'][0]['wikipedia_url'] = URL
        research['visual_identity'] = identity
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
        db.execute("UPDATE research_runs SET poi_key='osm:way:7' WHERE run_id='headless-run'")
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert researcher.searches == 0 and len(fetches) == 1
        confirmed = researcher.identity_contexts[0]
        assert len(json.dumps(confirmed)) < 5000 and 'geometry_proof' not in confirmed
        assert confirmed['physical_scope'] == proof['decision']['scope']
        assert confirmed['physical_identity_accepted'] is True
        assert fetches[0] == 'https://8.8.8.8/gate-history'
        current = svc.story(job['story_id'])
        assert current['visual_identity']['visual_reference_verified'] is False
        assert reviewed_reference_articles(current['visual_identity']) == {}
        assert len(current['facts']) == int(source_matches)
        if source_matches:
            assert current['facts'][0]['eligibility'] == 'unreviewed'
            await review_candidates(svc, job['story_id'], 'headless-run')
            assert svc.story(job['story_id'])['facts'][0]['eligibility'] == 'eligible'
            with svc.store.connection() as db:
                assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'eligible'
                assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 1
            context = HeadlessFacts(svc).adapter._compact_context(HeadlessFacts(svc).adapter._topic_state(job['story_id']))
            assert context['physical_identity_accepted'] is True
            assert context['visual_identity']['proof_kind'] == proof_kind
            assert context['visual_identity']['physical_scope'] == proof['decision']['scope']
            assert context['research_run']['identity_subject_article_sources'][0]['url'] == URL
        else:
            with svc.store.connection() as db:
                assert db.execute('SELECT COUNT(*) FROM poi_research_assertions').fetchone()[0] == 0
    finally:
        await reader.search_http.aclose()
