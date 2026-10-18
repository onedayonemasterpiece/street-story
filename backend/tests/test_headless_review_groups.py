"""Actual dispatch and commit interleavings with controlled semantic answers."""
import asyncio
import json

import pytest

from street_story import review_packets
from street_story.headless_fact_review import HeadlessFactReview
from test_headless_fact_pool import RUN
from test_headless_fact_review_parallel import ControlledReview, candidates


class ParallelReview(ControlledReview):
    MAX_PARALLEL_GROUPS = HeadlessFactReview.MAX_PARALLEL_GROUPS


@pytest.fixture(autouse=True)
def reset_parallel_host():
    ParallelReview.calls = ParallelReview.active = ParallelReview.peak = 0
    ParallelReview.mode = 'positive'


@pytest.mark.asyncio
async def test_two_new_independent_groups_dispatch_and_commit_without_stale_sibling(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=6)
    engine = ParallelReview(harness)
    assert await engine.run(job, RUN, 0) == 2
    assert ParallelReview.peak == 2 and ParallelReview.calls == 2
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 6
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
    assert await engine.run(job, RUN, 0) == 0 and ParallelReview.calls == 2


@pytest.mark.asyncio
async def test_two_unknown_groups_keep_their_original_scopes_and_never_repeat(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=6)
    engine = ParallelReview(harness)
    ParallelReview.mode = 'unknown'
    assert await engine.run(job, RUN, 0) == 0
    with svc.store.connection() as db:
        current = review_packets.bundle(db, job['story_id'])
    assert len(engine._unknown_candidates(job, current)) == 6
    assert ParallelReview.calls == 2 and ParallelReview.peak == 2
    ParallelReview.mode = 'positive'
    assert await engine.run(job, RUN, 0) == 0 and ParallelReview.calls == 2


@pytest.mark.asyncio
async def test_declared_pending_relation_is_jointly_reviewed_without_accepting_peer_text_as_evidence(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=6)

    class RelatedReview(ParallelReview):
        declared = False
        pair = None
        seen = []

        async def _infer(self, packet, job, unit, saved, ordinal=0):
            args = await super()._infer(packet, job, unit, saved, ordinal)
            with svc.store.connection() as db:
                original = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                    (packet['packet_ref'],)).fetchone()[0])
            ids = {item['id'] for item in original['items']}
            self.seen.append(ids)
            if not self.declared:
                peer = packet['pending_relation_context'][0]['fact_id']
                own = original['items'][0]['id']
                self.pair = {own, peer}
                args['decisions'][0].update(verdict='insufficient', related_pending_ids=[peer])
                self.declared = True
                old = self.service.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
                self._put(job, unit, {**old, 'args': args})
            return args

    engine = RelatedReview(harness)
    assert await engine.run(job, RUN, 0) == 3
    assert engine.pair <= engine.seen[-1] and len(engine.seen[-1]) == 2
    assert RelatedReview.calls == 3
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_pending_review_links').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM live_pending_review_resolutions').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 6
    assert await engine.run(job, RUN, 0) == 0 and RelatedReview.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['stop', 'source'])
async def test_fast_group_commits_before_slow_sibling_and_late_result_respects_fence(tmp_path, boundary):
    from street_story.research_control import stop_research
    svc, job, harness = await candidates(tmp_path, count=6)
    waiting, release = asyncio.Event(), asyncio.Event()

    class WaitingReview(ParallelReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            if ordinal == 1:
                waiting.set()
                await release.wait()
            return await super()._infer(packet, job, unit, saved, ordinal)

    engine = WaitingReview(harness)
    task = asyncio.create_task(engine.run(job, RUN, 0))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        for _ in range(100):
            with svc.store.connection() as db:
                ready = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]
            if ready:
                break
            await asyncio.sleep(.01)
        assert ready == 3 and not task.done()
        if boundary == 'stop':
            stop_research(svc, job['story_id'], purpose='facts')
        else:
            with svc.store.tx() as db:
                db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('new-source', job['story_id']))
        release.set()
        assert await asyncio.wait_for(task, 3) == 1
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 3
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
