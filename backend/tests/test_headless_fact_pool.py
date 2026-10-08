"""Real frozen chunk leases, async extractors, serial candidate ledger commits."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from street_story.errors import MalformedProviderResponse, RetryableProviderError
from street_story.headless_facts import HeadlessFacts
from street_story.research_runs import begin_research_run, persist_source_version
from test_live_editor import make_service, mark_identity_ready

RUN = 'parallel-headless-run'


def fixture(tmp_path, count=3):
    svc, _, original_session, _ = make_service(tmp_path)
    sid = original_session.resource_id
    mark_identity_ready(svc, sid)
    with svc.store.tx() as db:
        story = svc._story_row(db, sid)
        jid = svc._enqueue_job(db, sid, 'research', 'frozen-parallel',
                              {'identity_generation': 0, 'photo_sha256': story['photo_sha256']})
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (jid,))
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
        begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='History', scope='history',
                           expected_story_revision=story['revision'], identity_generation=0,
                           run_id=RUN, now=svc.store.now())
        for i in range(count):
            url = f'https://archive.example/history-{i}'
            persist_source_version(db, run_id=RUN, requested_url=url, final_url=url, title=f'History {i}',
                                   content_type='text/html', http_status=200, redirect_chain=[],
                                   normalized_text=f'The gate housed documented exhibit number {i} in 2005.',
                                   read_status='complete', now=svc.store.now())
    return svc, job


def result(page):
    return {'result': {'facts': [{'claim_key': page['source_url'], 'existing_fact_id': '',
                'text': page['evidence_passages'][0]['text'], 'confidence': .95,
                'passage_ids': [page['evidence_passages'][0]['passage_id']],
                'source_refs': [], 'evidence_refs': [], 'selected': True,
                'verdict': 'supported', 'atomic': True, 'support_complete': True,
                'qualifiers_preserved': True, 'review_reason': 'Literal frozen passage.'}],
            'source_matches_poi': True, 'source_content_valid': True, 'continuation_needed': False},
            'receipt': {'provider_id': 'controlled', 'model_id': 'controlled-model'}}


def count_facts(svc, sid):
    with svc.store.connection() as db:
        return db.execute('SELECT COUNT(*) FROM facts WHERE story_id=?', (sid,)).fetchone()[0]


async def partial(harness, job):
    try:
        await harness.run(job, RUN, 'History', 'history')
    except RetryableProviderError as exc:
        assert str(exc) in {'research_fact_source_coverage_partial', 'research_fact_next_page'}


@pytest.mark.asyncio
async def test_three_frozen_cores_extract_in_parallel_first_candidate_saved_before_slow_units(tmp_path):
    svc, job = fixture(tmp_path)
    release, started, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, active, peak, saves = [], 0, 0, 0

    async def extract(page, story, context):
        nonlocal active, peak
        calls.append((page['_extractor_ordinal'], page['chunk_id']))
        active += 1
        peak = max(peak, active)
        if len(calls) == 3:
            started.set()
        if page['_extractor_ordinal']:
            await release.wait()
        active -= 1
        return result(page)

    svc.providers.research = SimpleNamespace(client=SimpleNamespace(model_id='controlled-model'), extract_fact_page=extract)
    harness = HeadlessFacts(svc)
    original = harness.adapter._save_research_facts
    saving = 0

    async def serial_save(session, command_id, args):
        nonlocal saving, saves
        saving += 1
        assert saving == 1
        assert args['batch_reviewed'] is False and args['extractor_candidates'] is True
        await asyncio.sleep(0)
        value = await original(session, command_id, args)
        saving -= 1
        saves += 1
        saved.set()
        return value

    harness.adapter._save_research_facts = serial_save
    task = asyncio.create_task(partial(harness, job))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(saved.wait(), 2)
        assert not task.done() and count_facts(svc, job['story_id']) == 1
        with svc.store.connection() as db:
            owners = [r[0] for r in db.execute('SELECT lease_owner FROM research_chunk_runs WHERE run_id=? AND lease_owner IS NOT NULL', (RUN,))]
            assert len(owners) == 2 and len(set(owners)) == 2
        release.set()
        await asyncio.wait_for(task, 3)
        assert peak >= 2 and saves == 3 and count_facts(svc, job['story_id']) == 3
        await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
        assert len(calls) == 3  # Intake replay neither resends nor waits on Mira.
        with svc.store.connection() as db:
            assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (RUN,)).fetchone()[0] == 'verifying'
        assert {ordinal for ordinal, _ in calls} == {0, 1, 2}
        assert len({chunk for _, chunk in calls}) == 3
        with svc.store.connection() as db:
            assert {r[0] for r in db.execute('SELECT eligibility FROM fact_assertions')} == {'unreviewed'}
            assert {r[0] for r in db.execute('SELECT eligibility FROM poi_research_assertions')} == {'unreviewed'}
            assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM research_checkpoints WHERE stage LIKE 'headless_fact_result:%'").fetchone()[0] == 3
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_unknown_one_core_does_not_stall_siblings_or_resend_on_restart(tmp_path):
    svc, job = fixture(tmp_path)
    calls = []

    async def extract(page, story, context):
        calls.append(page['_extractor_ordinal'])
        if page['_extractor_ordinal'] == 0:
            raise RetryableProviderError('research_attempt_outcome_unknown')
        return result(page)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    harness = HeadlessFacts(svc)
    await partial(harness, job)
    assert count_facts(svc, job['story_id']) == 2
    await partial(HeadlessFacts(svc), job)
    assert calls.count(0) == 1 and len(calls) == 3
    with svc.store.connection() as db:
        phases = [json.loads(row[0])['phase'] for row in db.execute("SELECT value_json FROM research_checkpoints WHERE stage LIKE 'headless_fact_unit:%'")]
        assert sorted(phases) == ['committed', 'committed', 'unknown']


@pytest.mark.asyncio
async def test_closed_malformed_core_can_take_new_available_route_without_blocking_siblings(tmp_path):
    svc, job = fixture(tmp_path)
    calls, available = [], False

    async def extract(page, story, context):
        calls.append(page['_extractor_ordinal'])
        if page['_extractor_ordinal'] == 0 and not available:
            raise MalformedProviderResponse('closed_malformed_response')
        return result(page)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    await partial(HeadlessFacts(svc), job)
    assert count_facts(svc, job['story_id']) == 2
    available = True
    await partial(HeadlessFacts(svc), job)
    assert count_facts(svc, job['story_id']) == 3
    assert calls.count(0) == 2 and calls.count(1) == calls.count(2) == 1


@pytest.mark.asyncio
async def test_owner_edit_defers_durable_results_without_new_model_send(tmp_path):
    svc, job = fixture(tmp_path)
    calls = []

    async def extract(page, story, context):
        calls.append(page['_unit_id'])
        if len(calls) == 1:
            with svc.store.tx() as db:
                row = svc._story_row(db, job['story_id'])
                research = json.loads(row['research_json'])
                research['publication_concept'] = 'New owner concept'
                db.execute('UPDATE stories SET research_json=?,draft_text=?,revision=revision+1 WHERE id=?',
                           (json.dumps(research), 'New owner draft', job['story_id']))
        return result(page)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    await partial(HeadlessFacts(svc), job)
    assert count_facts(svc, job['story_id']) == 0
    await partial(HeadlessFacts(svc), job)
    assert len(calls) == 3 and count_facts(svc, job['story_id']) == 0
    assert svc.story(job['story_id'])['draft_text'] == 'New owner draft'


@pytest.mark.asyncio
async def test_worker_loses_job_fence_results_remain_durable_but_never_commit(tmp_path):
    svc, job = fixture(tmp_path)
    calls = []

    async def extract(page, story, context):
        calls.append(page['_unit_id'])
        with svc.store.tx() as db:
            db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job['id'],))
        return result(page)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
    assert count_facts(svc, job['story_id']) == 0
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_checkpoints WHERE stage LIKE 'headless_fact_result:%'").fetchone()[0] == 3
    await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_more_than_three_sources_accumulate_with_bounded_pool_not_a_source_limit(tmp_path):
    svc, job = fixture(tmp_path, count=5)
    calls = []

    async def extract(page, story, context):
        calls.append(page['_unit_id'])
        return result(page)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    with pytest.raises(RetryableProviderError, match='research_fact_next_page') as wait:
        await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
    assert wait.value.retry_at <= svc.store.now() + 11
    assert len(calls) == 3 and count_facts(svc, job['story_id']) == 3
    await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
    assert len(calls) == 5 and count_facts(svc, job['story_id']) == 5
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (RUN,)).fetchone()[0] == 'verifying'
        assert {row[0] for row in db.execute('SELECT eligibility FROM fact_assertions')} == {'unreviewed'}


@pytest.mark.asyncio
@pytest.mark.parametrize('useful', [False, True])
async def test_exhausted_article_ends_automatic_run_without_suppressing_other_supported_facts(tmp_path, useful):
    from street_story.errors import PermanentProviderError
    from test_headless_fact_review_parallel import ControlledReview
    svc, job = fixture(tmp_path, count=2 if useful else 1)
    calls = []
    async def extract(page, story, context):
        calls.append(page['_unit_id'])
        if not useful or page['_extractor_ordinal'] == 1:
            raise PermanentProviderError('research_fact_routes_exhausted')
        return result(page)
    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    harness = HeadlessFacts(svc)
    try:
        outcome = await harness.run(job, RUN, 'History', 'history')
    except RetryableProviderError:
        assert useful
        outcome = None
    if useful:
        ControlledReview.mode = 'positive'
        assert await ControlledReview(harness).run(job, RUN, 0) == 1
        outcome = await harness.run(job, RUN, 'History', 'history')
    assert outcome['outcome'] == ('useful_partial' if useful else 'no_supported_facts')
    assert outcome['coverage_complete'] is False
    assert outcome['eligible_count'] == (1 if useful else 0)
    before = list(calls)
    assert await harness.run(job, RUN, 'History', 'history') == outcome
    assert calls == before
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research_chunk_runs WHERE status='failed'").fetchone()[0] == 1
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] in {0, None}
