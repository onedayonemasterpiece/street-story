"""Closed review refusals end unchanged scopes without promoting any claim."""
import json
from types import SimpleNamespace

import pytest

from street_story import headless_fact_review, review_packets
from street_story.errors import RetryableProviderError
from street_story.headless_fact_review import HeadlessFactReview
from street_story.research_runs import manifest_exhausted, run_manifest
from street_story.service import ConflictError
from test_headless_fact_review_parallel import ControlledReview, candidates, controlled_review_route, qualify_controlled_review
from test_headless_fact_pool import RUN


def session(job):
    return SimpleNamespace(id='closed-review', resource_id=job['story_id'], actor=None,
                           closed=False, model='fixture', state={})


def frozen_unit(engine, job, harness, ids=None):
    if ids is None:
        with engine.service.store.connection() as db:
            ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = engine._prepare_packet(job, RUN, session(job), ids)
    assert packet is not None
    return packet, unit, ids


class ClosedInvalidReview(HeadlessFactReview):
    """A completed semantic response rejected by the actual own-evidence gate."""
    def __init__(self, harness):
        super().__init__(harness)
        self.fixture_route = qualify_controlled_review(harness)
        self.calls = 0

    async def _infer(self, packet, job, unit, saved, ordinal=0):
        self.calls += 1
        args = {'packet_ref': packet['packet_ref'], 'decisions': [
            {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
             'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
             'claims': [item['text']], 'basis_quotes': ['A foreign passage absent from this source.'],
             'reason': 'Invalid supplied literal quote.'} for item in packet['items']],
            'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
        self._put(job, unit, {'phase': 'result', 'args': args,
                             'route_identity': self._route_identity(self.fixture_route)})
        return args


def use_review(harness, engine, monkeypatch):
    monkeypatch.setattr(HeadlessFactReview, '_qualified_routes', lambda self, **kwargs:
                        [controlled_review_route()])
    async def review(job, run_id, revision):
        return await engine.run(job, run_id, revision)
    monkeypatch.setattr(harness, '_review_candidates', review)


@pytest.mark.asyncio
async def test_closed_invalid_review_finishes_and_restarts_without_support_or_new_inference(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=2)
    engine = ClosedInvalidReview(harness)
    use_review(harness, engine, monkeypatch)

    outcome = await harness.run(job, RUN, 'History', 'history')

    assert outcome == {'outcome': 'no_supported_facts', 'reason': 'fact_review_exhausted',
                       'coverage_complete': False, 'eligible_count': 0}
    assert engine.calls == 1
    assert await harness.run(job, RUN, 'History', 'history') == outcome
    assert engine.calls == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (RUN,)).fetchone()[0] == 'completed'
        assert {row[0] for row in db.execute('SELECT eligibility FROM fact_assertions')} == {'unreviewed'}
        assert {row[0] for row in db.execute('SELECT eligibility FROM poi_research_assertions')} == {'unreviewed'}
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
        saved = [json.loads(row[0]) for row in db.execute("SELECT value_json FROM research_checkpoints "
                 "WHERE stage LIKE 'headless_fact_review:%'")]
    assert len(saved) == 1 and saved[0]['phase'] == 'rejected'
    assert saved[0]['error_code'] == 'live_fact_review_evidence_invalid'


@pytest.mark.parametrize('phase', ['rejected', 'exhausted'])
@pytest.mark.parametrize('mutation', ['candidate_revision', 'verifier_contract', 'owner_context', 'eligible_ledger'])
@pytest.mark.asyncio
async def test_closed_refusal_reopens_only_changed_current_review_recipe(tmp_path, monkeypatch, phase, mutation):
    svc, job, harness = await candidates(tmp_path, count=2)
    engine = HeadlessFactReview(harness)
    packet, unit, ids = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': phase, 'packet_ref': packet['packet_ref']})
    assert engine.exhausted_candidates(job) == set(ids)
    assert not harness._unreviewed_actionable(job, RUN)

    if mutation == 'verifier_contract':
        monkeypatch.setattr(headless_fact_review, 'VERIFIER_CONTRACT_ID', 'changed-verifier-contract')
    else:
        with svc.store.tx() as db:
            if mutation == 'candidate_revision':
                db.execute('UPDATE fact_assertions SET revision_digest=? WHERE story_id=? AND assertion_id=?',
                           ('repaired-revision', job['story_id'], ids[0]))
            elif mutation == 'owner_context':
                db.execute('UPDATE stories SET draft_text=? WHERE id=?', ('New owner context', job['story_id']))
            else:
                db.execute("UPDATE fact_assertions SET eligibility='eligible' WHERE story_id=? AND assertion_id=?",
                           (job['story_id'], ids[0]))
    assert engine.exhausted_candidates(job) == set()
    assert harness._unreviewed_actionable(job, RUN)
    new_packet, new_unit, _ = engine._prepare_packet(job, RUN, session(job), ids)
    assert new_packet and new_unit != unit


@pytest.mark.parametrize('phase', ['started', 'unknown', 'stale'])
@pytest.mark.asyncio
async def test_unresolved_or_stale_review_never_counts_as_closed_exhaustion(tmp_path, phase):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, unit, _ = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': phase, 'packet_ref': packet['packet_ref']})
    assert engine.exhausted_candidates(job) == set()
    assert harness._unreviewed_actionable(job, RUN)


@pytest.mark.asyncio
async def test_unknown_original_still_fences_new_contract_on_restart(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=1)
    original_engine = HeadlessFactReview(harness)
    packet, unit, ids = frozen_unit(original_engine, job, harness)
    original_engine._put(job, unit, {'phase': 'unknown', 'packet_ref': packet['packet_ref']})
    monkeypatch.setattr(headless_fact_review, 'VERIFIER_CONTRACT_ID', 'changed-verifier-contract')
    engine = ClosedInvalidReview(harness)
    use_review(harness, engine, monkeypatch)

    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='research_fact_review_partial'):
            await harness.run(job, RUN, 'History', 'history')
    assert engine.calls == 0
    assert engine.exhausted_candidates(job) == set()
    with svc.store.connection() as db:
        assert engine._unknown_candidates(job, review_packets.bundle(db, job['story_id'])) == set(ids)
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_outcome:' + RUN) is None


@pytest.mark.asyncio
async def test_unknown_review_scope_allows_other_candidates_without_resending_or_stale_commit(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=4)
    class IndependentReview(ControlledReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            if saved.get('phase') == 'observe_original':
                return None
            return await super()._infer(packet, job, unit, saved, ordinal)
    engine = IndependentReview(harness)
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = frozen_unit(engine, job, harness, ids[:1])
    original = {'phase':'unknown','packet_ref':packet['packet_ref'], 'frozen_packet':packet,
        'route':'facts_review_fixture','route_identity':engine._route_identity(engine.fixture_route)}
    engine._put(job, unit, original)
    use_review(harness, engine, monkeypatch)
    assert await engine.run(job, RUN, 0) == 1
    with svc.store.connection() as db:
        eligible = {row[0] for row in db.execute("SELECT assertion_id FROM fact_assertions WHERE eligibility='eligible'")}
        assert eligible == set(ids[1:])
        assert engine._unknown_candidates(job, review_packets.bundle(db, job['story_id'])) == {ids[0]}
        with pytest.raises(ConflictError, match='Revisions changed'):
            review_packets.load(harness.adapter, session(job), db, packet['packet_ref'])
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit) == original
    assert IndependentReview.calls == 1


@pytest.mark.asyncio
async def test_only_closed_empty_live_review_finishes_without_replacing_unknown(tmp_path, monkeypatch):
    from street_story.service import canonical
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, unit, ids = frozen_unit(engine, job, harness)
    role = 'facts_review_gemini-3.8-live'
    original = {'phase':'unknown','packet_ref':packet['packet_ref'], 'frozen_packet':packet,
        'route':role,'route_identity':{'provider_id':'google-live','model_id':'gemini-3.8-live',
                                     'endpoint':'fixture:closed-live'}}
    engine._put(job, unit, original)
    receipt = {'phase':'unknown','provider_id':'google-live','provider_send_state':'submitted',
        'error_code':'live_research_timeout','text_sends':1,'binding':{'fact_unit_id':unit}}
    with svc.store.tx() as db:
        now = svc.store.now()
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('review-unknown','review-unknown',job['story_id'],role,canonical(receipt),now,now))
    async def no_replacement(*args):
        return 0
    monkeypatch.setattr(harness, '_review_candidates', no_replacement)
    outcome = await harness.run(job, RUN, 'History', 'history')
    assert outcome['outcome'] == 'resource_blocked'
    assert outcome['reason'] == 'live_research_original_outcome_unavailable'
    assert not outcome['coverage_complete']
    with svc.store.connection() as db:
        assert json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0]) == receipt
        assert db.execute('SELECT eligibility FROM fact_assertions').fetchone()[0] == 'unreviewed'
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit) == original


@pytest.mark.asyncio
async def test_closed_refusal_with_foreign_unit_does_not_exhaust_current_candidates(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, _, _ = frozen_unit(engine, job, harness)
    engine._put(job, 'foreign-or-old-contract', {'phase': 'rejected', 'packet_ref': packet['packet_ref']})
    assert engine.exhausted_candidates(job) == set()
    assert harness._unreviewed_actionable(job, RUN)


@pytest.mark.parametrize('pending', ['deferred_source', 'unknown_chunk'])
@pytest.mark.asyncio
async def test_closed_reviews_do_not_finish_deferred_source_or_unknown_page(tmp_path, monkeypatch, pending):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, unit, _ = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': 'rejected', 'packet_ref': packet['packet_ref']})
    if pending == 'deferred_source':
        with svc.store.tx() as db:
            db.execute("UPDATE research_run_sources SET status='deferred' WHERE run_id=?", (RUN,))
    else:
        with svc.store.tx() as db:
            db.execute("UPDATE research_chunk_runs SET status='deferred' WHERE run_id=?", (RUN,))
            for row in db.execute("SELECT stage,value_json FROM research_checkpoints WHERE job_id=? "
                                  "AND stage LIKE 'headless_fact_unit:%'", (job['id'],)).fetchall():
                state = json.loads(row['value_json'])
                state['phase'] = 'unknown'
                db.execute('UPDATE research_checkpoints SET value_json=? WHERE job_id=? AND stage=?',
                           (json.dumps(state), job['id'], row['stage']))
    async def no_new_pages(*args):
        return []  # Reproduce the retry branch while the existing read/UNKNOWN remains pending.
    monkeypatch.setattr(harness, '_prepare_units', no_new_pages)
    with svc.store.connection() as db:
        assert not manifest_exhausted(run_manifest(db, RUN))
    with pytest.raises(RetryableProviderError, match='research_fact_source_coverage_partial'):
        await harness.run(job, RUN, 'History', 'history')
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_outcome:' + RUN) is None
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (RUN,)).fetchone()[0] != 'completed'


@pytest.mark.asyncio
async def test_finished_closed_scope_preserves_joined_continuation(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    with svc.store.tx() as db:
        story = svc._story_row(db, job['story_id'])
        research = json.loads(story['research_json'])
        research['pending_fact_request'] = {'photo_sha256': story['photo_sha256'], 'identity_generation': 0,
                                            'input_revision': 'existing-joined-request', 'goal': 'Another source'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), job['story_id']))
    engine = HeadlessFactReview(harness)
    packet, unit, _ = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': 'rejected', 'packet_ref': packet['packet_ref']})

    assert await harness.run(job, RUN, 'History', 'history') is None
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_outcome:' + RUN) is None
    with svc.store.connection() as db:
        assert db.execute('SELECT state FROM research_runs WHERE run_id=?', (RUN,)).fetchone()[0] == 'completed'
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (job['story_id'],)).fetchone()[0])
    assert research['pending_fact_request']['input_revision'] == 'existing-joined-request'


@pytest.mark.asyncio
async def test_closed_packet_leaves_independent_unreviewed_candidate_actionable(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=2)
    engine = HeadlessFactReview(harness)
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = frozen_unit(engine, job, harness, ids[:1])
    engine._put(job, unit, {'phase': 'rejected', 'packet_ref': packet['packet_ref']})
    assert engine.exhausted_candidates(job) == set(ids[:1])
    assert harness._unreviewed_actionable(job, RUN)
    assert await harness.run(job, RUN, 'History', 'history') is None
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_outcome:' + RUN) is None


@pytest.mark.asyncio
async def test_exhausted_input_without_review_support_finishes_honestly(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, unit, _ = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': 'exhausted', 'packet_ref': packet['packet_ref'],
                            'error_code': 'review_input_limit'})
    outcome = await harness.run(job, RUN, 'History', 'history')
    assert outcome['outcome'] == 'no_supported_facts'
    assert outcome['coverage_complete'] is False and outcome['eligible_count'] == 0


@pytest.mark.asyncio
async def test_closed_review_finishes_partial_after_independent_supported_claim(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=2)
    class SingleReview(ControlledReview):
        MAX_PACKET_FACTS = 1
    positive = SingleReview(harness)
    assert await positive._run_one(job, RUN, 0) == 1
    engine = ClosedInvalidReview(harness)
    use_review(harness, engine, monkeypatch)

    outcome = await harness.run(job, RUN, 'History', 'history')

    assert outcome == {'outcome': 'useful_partial', 'reason': 'fact_review_exhausted',
                       'coverage_complete': False, 'eligible_count': 1}
    assert engine.calls == 1
    with svc.store.connection() as db:
        assert sorted(row[0] for row in db.execute('SELECT eligibility FROM fact_assertions')) == ['eligible', 'unreviewed']


@pytest.mark.parametrize('repair', ['candidate_revision', 'verifier_contract'])
@pytest.mark.asyncio
async def test_repaired_revision_or_contract_can_receive_new_supported_review(tmp_path, monkeypatch, repair):
    svc, job, harness = await candidates(tmp_path, count=1)
    rejected = HeadlessFactReview(harness)
    packet, unit, ids = frozen_unit(rejected, job, harness)
    rejected._put(job, unit, {'phase': 'rejected', 'packet_ref': packet['packet_ref']})
    if repair == 'candidate_revision':
        with svc.store.tx() as db:
            db.execute('UPDATE fact_assertions SET revision_digest=? WHERE story_id=? AND assertion_id=?',
                       ('new-evidence-revision', job['story_id'], ids[0]))
    else:
        monkeypatch.setattr(headless_fact_review, 'VERIFIER_CONTRACT_ID', 'fresh-contract')
    engine = ControlledReview(harness)
    use_review(harness, engine, monkeypatch)

    outcome = await harness.run(job, RUN, 'History', 'history')

    assert outcome['outcome'] == 'useful_complete'
    assert outcome['coverage_complete'] is True and outcome['eligible_count'] == 1
    with svc.store.connection() as db:
        closed = [json.loads(row[0])['phase'] for row in db.execute("SELECT value_json FROM research_checkpoints "
                  "WHERE stage LIKE 'headless_fact_review:%'")]
    assert sorted(closed) == ['committed', 'rejected']


@pytest.mark.asyncio
async def test_incomplete_but_terminal_source_manifest_finishes_closed_review_scope(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    packet, unit, _ = frozen_unit(engine, job, harness)
    engine._put(job, unit, {'phase': 'rejected', 'packet_ref': packet['packet_ref']})
    with svc.store.tx() as db:
        db.execute("UPDATE research_run_sources SET status='failed' WHERE run_id=?", (RUN,))

    outcome = await harness.run(job, RUN, 'History', 'history')

    assert outcome == {'outcome': 'no_supported_facts', 'reason': 'source_manifest_exhausted',
                       'coverage_complete': False, 'eligible_count': 0}
