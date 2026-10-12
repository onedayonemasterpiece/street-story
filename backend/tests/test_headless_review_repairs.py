"""Mechanical replacement ownership; controlled verdicts are not model evidence."""
import json

import pytest

from street_story.headless_fact_review import HeadlessFactReview
from street_story.headless_review_quotes import OWN_REFS_CONTRACT, response_schema
from street_story.live import FUNCTIONS
from test_headless_fact_review_parallel import ControlledReview, candidates
from test_headless_fact_pool import RUN


class ReplacingReview(ControlledReview):
    foreign = False
    unchanged = False

    async def _infer(self, packet, job, unit, saved, ordinal=0):
        args = await super()._infer(packet, job, unit, saved, ordinal)
        if args is None:
            return args
        with self.service.store.connection() as db:
            repaired = db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0]
        for decision in args['decisions']:
            rows = [r for r in packet['items'] if r['fact'] == decision['fact']]
            decision['basis_quotes'] = [r['quote_ref'] for r in rows]
            if not repaired:
                decision.update(verdict='repair_needed', atomic=False, support_complete=False,
                    repair_facts=[{'text': rows[0]['text'] if self.unchanged else 'In 2005 the gate housed an exhibit.', 'claim_key': 'exhibit-2005',
                                   **({'quote_refs': ['@own_quote:foreign'] if self.foreign else [rows[0]['quote_ref']]}
                                      if packet.get('evidence_addressing') == OWN_REFS_CONTRACT
                                      else {'evidence': [999] if self.foreign else [0]})}])
        old = self.service.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
        self._put(job, unit, {**old, 'args': args})
        return args


@pytest.fixture(autouse=True)
def reset_replacement_host():
    ReplacingReview.calls = ReplacingReview.active = ReplacingReview.peak = 0
    ReplacingReview.mode = 'positive'


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_ack', [False, True])
async def test_replacement_reenters_bounded_queue_and_replays_lost_ack_without_new_send(tmp_path, lost_ack):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = ReplacingReview(harness)
    original_execute = harness.adapter.execute_tool
    lost = False

    async def execute(session, call):
        nonlocal lost
        value = await original_execute(session, call)
        if call['name'] == 'repair_research_fact' and lost_ack and not lost:
            lost = True
            raise RuntimeError('lost repair acknowledgement')
        return value

    harness.adapter.execute_tool = execute
    if lost_ack:
        with pytest.raises(RuntimeError, match='lost repair acknowledgement'):
            await engine.run(job, RUN, 0)
        engine = ReplacingReview(harness)
        assert await engine.run(job, RUN, 0) == 1
    else:
        assert await engine.run(job, RUN, 0) == 2
    assert ReplacingReview.calls == 2
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 1
        rows = [dict(row) for row in db.execute('SELECT f.text,a.eligibility,a.owner_selected FROM fact_assertions a '
            'JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id')]
        assert len(rows) == 2 and {row['eligibility'] for row in rows} == {'withheld', 'eligible'}
        assert all(row['owner_selected'] == 0 for row in rows)
        assert next(row['text'] for row in rows if row['eligibility'] == 'eligible') == 'In 2005 the gate housed an exhibit.'
        outcomes = [json.loads(row[0]) for row in db.execute('SELECT value_json FROM research_checkpoints '
            "WHERE stage LIKE 'headless_fact_review:%'")]
        assert any(row.get('repair_state', {}).get('phase') == 'applied' for row in outcomes)
    assert await engine.run(job, RUN, 0) == 0 and ReplacingReview.calls == 2


@pytest.mark.asyncio
async def test_foreign_replacement_cannot_create_evidence_or_repeat_closed_review(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = ReplacingReview(harness)
    engine.foreign = True
    assert await engine.run(job, RUN, 0) == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
    assert await engine.run(job, RUN, 0) == 0 and ReplacingReview.calls == 1


@pytest.mark.asyncio
async def test_unchanged_proposal_is_visible_remainder_without_new_inference(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = ReplacingReview(harness)
    engine.unchanged = True
    assert await engine.run(job, RUN, 0) == 1
    assert await engine.run(job, RUN, 0) == 0 and ReplacingReview.calls == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0
        states = [json.loads(r[0]) for r in db.execute('SELECT value_json FROM research_checkpoints '
            "WHERE stage LIKE 'headless_fact_review:%'")]
    assert states[0]['repair_state']['error_code'] == 'live_research_repair_no_change'


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['stop', 'source'])
async def test_stop_or_replaced_source_after_closed_review_fences_repair(tmp_path, boundary):
    from street_story.research_control import stop_research
    svc, job, harness = await candidates(tmp_path, count=1)
    execute = harness.adapter.execute_tool

    async def close_then_fence(session, call):
        value = await execute(session, call)
        if call['name'] == 'finalize_fact_review':
            if boundary == 'stop':
                stop_research(svc, job['story_id'], purpose='facts')
            else:
                with svc.store.tx() as db:
                    db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('new-source', job['story_id']))
        return value

    harness.adapter.execute_tool = close_then_fence
    engine = ReplacingReview(harness)
    assert await engine.run(job, RUN, 0) == 1
    assert await engine.run(job, RUN, 0) == 0 and ReplacingReview.calls == 1
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0


def test_replacements_are_optional_private_proposals_not_public_positive_proof():
    schema = next(t['parameters'] for t in FUNCTIONS if t['name'] == 'finalize_fact_review')
    private = response_schema({'verifier_presentation': 'one_assertion_all_own_slices_v1',
                               'quote_catalog': {}, 'items': []}, schema)
    assert 'repair_facts' in private['properties']['decisions']['items']['properties']
    assert 'repair_facts' not in schema['properties']['decisions']['items']['properties']
    assert HeadlessFactReview.MAX_PACKET_FACTS == 12
