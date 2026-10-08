from types import SimpleNamespace

import pytest

from street_story import headless_fact_review, review_packets
from street_story.headless_fact_review import HeadlessFactReview, VERIFIER_PROMPT
from test_headless_fact_review_parallel import candidates, ControlledReview, RUN


@pytest.fixture(autouse=True)
def reset_reviews():
    ControlledReview.calls = ControlledReview.active = ControlledReview.peak = 0
    ControlledReview.mode = 'positive'


def test_closed_prompt_preserves_semantics_without_foreground_tool_instructions():
    for required in ('qualifiers_preserved', 'basis_quotes', 'equivalent_to_existing', 'conflicts_with_existing',
                     'coverage_complete=false', 'one closed JSON operation', 'insufficient'):
        assert required in VERIFIER_PROMPT
    for forbidden in ('get_review_context', 'repair_research_fact', 'bash'):
        assert forbidden not in VERIFIER_PROMPT
    assert 'get_review_context' in review_packets.REVIEW_CHECKS
    assert 'repair_research_fact' in review_packets.REVIEW_CHECKS


@pytest.mark.asyncio
async def test_old_unknown_scope_not_resent_after_contract_change_and_conflicting_ledger_waits(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=6)
    session = SimpleNamespace(id='legacy-review', resource_id=job['story_id'], actor=None, closed=False, state={})
    with svc.store.connection() as db:
        pending = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN,
        '_candidate_ids': pending[:3], '_parallel_candidate_review': True})
    engine = ControlledReview(harness)
    old = {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'route': 'old-contract-route'}
    engine._put(job, 'old-verifier-contract-unit', old)
    assert await engine.run(job, RUN, 0) == 0
    assert ControlledReview.calls == 0
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:old-verifier-contract-unit') == old
    with svc.store.connection() as db:
        eligible = {row[0] for row in db.execute("SELECT assertion_id FROM fact_assertions WHERE eligibility='eligible'")}
        assert eligible == set()
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_closed_old_scope_can_use_distinct_frozen_contract_unit(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=3)
    captured = []
    engine = HeadlessFactReview(harness)
    async def capture(packet, job, unit, saved, ordinal=0):
        captured.append(unit)
        return None
    monkeypatch.setattr(engine, '_infer', capture)
    engine._put(job, 'closed-old-unit', {'phase': 'closed_error'})
    assert await engine.run(job, RUN, 0) == 0
    first = captured[-1]
    monkeypatch.setattr(headless_fact_review, 'VERIFIER_CONTRACT_ID', 'changed-frozen-contract')
    assert await engine.run(job, RUN, 0) == 0
    assert captured[-1] != first
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:closed-old-unit') == {'phase': 'closed_error'}


@pytest.mark.asyncio
async def test_unknown_scope_without_packet_is_not_assumed_closed(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=3)
    engine = ControlledReview(harness)
    engine._put(job, 'missing-packet-unit', {'phase': 'started'})
    assert await engine.run(job, RUN, 0) == 0
    assert ControlledReview.calls == 0
