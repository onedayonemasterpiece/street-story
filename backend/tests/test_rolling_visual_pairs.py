"""Real queue/store fixtures with controlled provider and reader boundaries."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.research_control import stop_research
from street_story.service import canonical, digest
from street_story.visual_attachments import visual_operation_unit
from test_parallel_identity_pairs import prepare, response, wait_match


def install(svc, call):
    provider = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google', 'opencode'), visual_pair_route=call)
    svc.providers.research = provider
    return provider


def retained_unknown(svc, story, item, context, *, foreign=False):
    supplied = json.loads(context)
    if foreign:
        supplied = {**supplied, 'comparison_id': 'unrelated-original-operation'}
    unit = canonical(visual_operation_unit(item, supplied))
    logical = digest([story['id'], 0, 'vision', unit])
    receipt = {'phase': 'unknown', 'provider_send_state': 'possibly_sent',
        'message_id': 'original-message', 'binding': {'visual_scope': True, 'generation': 0}}
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('retained-original', logical, story['id'], 'vision', canonical(receipt),
             svc.store.now(), svc.store.now()))


@pytest.mark.asyncio
async def test_late_discovery_wakes_free_lane_and_uses_new_qualified_route(tmp_path, monkeypatch):
    svc, story, _ = prepare(tmp_path, count=2)
    svc.settings = replace(svc.settings, worker_poll_seconds=.01)
    fast_finished, next_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    qualified = ['google', 'opencode']
    async def read(service, item, sources, rejected, *, receipts):
        assert sources[0]['url'] == 'https://example.com/late-article'
        receipts.append({'url': sources[0]['url'], 'status': 'completed'})
        return [{'candidate_id': 'web:late', 'name': 'Late exterior', 'url': sources[0]['url'],
            'discovery': 'web_article_media', 'reference_image_urls': ['https://example.com/late.jpg']}]
    monkeypatch.setattr('street_story.article_media.article_candidates', read)
    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        if cid == 'gate:b':
            await release.wait()
        elif cid == 'gate:a':
            qualified[:] = ['native', 'opencode']
            fast_finished.set()
        else:
            assert cid == 'web:late' and route == 'native' and not release.is_set()
            next_started.set()
        return response(item, route)
    provider = install(svc, pair)
    provider.parallel_visual_routes = lambda: tuple(qualified)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await asyncio.wait_for(fast_finished.wait(), 2)
        await asyncio.sleep(.05)  # No reader work existed when the lane first freed.
        with svc.store.tx() as db:
            research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
            research['identity_article_discovery'] = {'photo_sha256': story['photo_sha256'], 'generation': 0,
                'sources': [{'url': 'https://example.com/late-article', 'discovery_provider': 'fixture'}]}
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        await asyncio.wait_for(next_started.wait(), 2)
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_freed_route_dispatches_next_ready_ref_before_slow_sibling_finishes(tmp_path):
    svc, story, _ = prepare(tmp_path, count=4)
    slow_entered, next_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    active = peak = 0

    async def pair(route, snapshot, item, schema, context):
        nonlocal active, peak
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        calls.append((route, cid, json.loads(context)['comparison_id']))
        active += 1
        peak = max(peak, active)
        try:
            if cid == 'gate:b':
                slow_entered.set()
                await release.wait()
                return response(item, 'original-slow')
            await slow_entered.wait()
            if cid == 'gate:c':
                assert not release.is_set()
                next_started.set()
                return response(item, 'fast-next', 'match')
            assert cid == 'gate:a'
            return response(item, 'fast-first')
        finally:
            active -= 1

    install(svc, pair)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await asyncio.wait_for(next_started.wait(), 2)
        await wait_match(svc, story['id'])
        assert not task.done() and peak == 2
        assert [(route, cid) for route, cid, _ in calls] == [
            ('google', 'gate:a'), ('opencode', 'gate:b'), ('google', 'gate:c')]
        _, research = svc._identity_snapshot(story['id'])
        pairs = research['visual_search_operation']['parallel_pairs']
        assert pairs[1]['id'] == calls[1][2] and pairs[1]['phase'] == 'submitted'
        assert pairs[2]['id'] == calls[2][2] and pairs[2]['phase'] == 'completed'
        assert len({p['id'] for p in pairs}) == 3
        assert 'gate:d' in [c['candidate_id'] for c in research['visual_search_operation']['queue']]
        release.set()
        assert await task
        assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:c'
        assert len(calls) == 3  # Match stopped the remaining ready reference.
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_child_holds_its_lane_but_fast_lane_refills_then_restart_observes_original(tmp_path):
    svc, story, _ = prepare(tmp_path, count=4)
    unknown_written = asyncio.Event()
    calls = []

    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        calls.append((route, cid, json.loads(context)['comparison_id'], item['_visual_pair_resume_only']))
        if cid == 'gate:b':
            retained_unknown(svc, story, item, context)
            unknown_written.set()
            raise RetryableProviderError('research_visual_pair_outcome_unknown', retry_at=svc.store.now()+1)
        await unknown_written.wait()
        assert route == 'google'
        return response(item, 'fast-route', 'match' if cid == 'gate:c' else 'mismatch')

    provider = install(svc, pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:c'
    original_id = next(c[2] for c in calls if c[1] == 'gate:b')
    assert len(calls) == 3
    _, research = svc._identity_snapshot(story['id'])
    peer = next(p for p in research['visual_search_operation']['parallel_pairs'] if p['id'] == original_id)
    assert peer['phase'] == 'submitted'

    async def observe(route, snapshot, item, schema, context):
        assert route == 'opencode'
        assert item['_visual_pair_resume_only'] and item['_visual_pair_observe_only']
        assert json.loads(context)['comparison_id'] == original_id
        calls.append((route, 'gate:b', original_id, True))
        return response(item, 'original-readback')

    provider.visual_pair_route = observe
    provider.vision_available = False
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='identity_visual'")
    assert await svc.run_once(claim_kind='identity_visual')
    assert calls[-1] == ('opencode', 'gate:b', original_id, True)
    assert len(calls) == 4
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:c'


@pytest.mark.asyncio
async def test_unrelated_unknown_retains_guard_and_never_dispatches_new_ref(tmp_path):
    svc, story, _ = prepare(tmp_path, count=4)
    unknown_written = asyncio.Event()
    calls = []

    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        calls.append(cid)
        if cid == 'gate:b':
            retained_unknown(svc, story, item, context, foreign=True)
            unknown_written.set()
            raise RetryableProviderError('research_visual_pair_outcome_unknown', retry_at=svc.store.now()+1)
        await unknown_written.wait()
        return response(item, 'fast-first')

    install(svc, pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert calls == ['gate:a', 'gate:b']
    _, research = svc._identity_snapshot(story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert len(research['visual_search_operation']['parallel_pairs']) == 2
    assert [c['candidate_id'] for c in research['visual_search_operation']['queue']] == ['gate:c', 'gate:d']


@pytest.mark.asyncio
async def test_stop_preserves_submitted_ids_and_prohibits_refill(tmp_path):
    svc, story, _ = prepare(tmp_path, count=4)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def pair(route, snapshot, item, schema, context):
        calls.append(json.loads(context)['comparison_id'])
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return response(item, route+'-original')

    install(svc, pair)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop_research(svc, story['id'], purpose='identity')
        _, before = svc._identity_snapshot(story['id'])
        frozen = before['visual_search_operation']['parallel_pairs']
        release.set()
        assert await task
        _, after = svc._identity_snapshot(story['id'])
        assert after['visual_search_operation']['parallel_pairs'] == frozen
        assert len(calls) == 2 and [p['id'] for p in frozen] == calls
        assert not after['visual_search_operation']['reviewed_reference_ids']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_during_reader', [False, True])
async def test_reader_refill_does_not_block_ready_match_commit_or_send_after_match(tmp_path, monkeypatch, stop_during_reader):
    svc, story, _ = prepare(tmp_path, count=4)
    with svc.store.tx() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
        research['identity_article_discovery'] = {'photo_sha256': story['photo_sha256'], 'generation': 0,
            'sources': [{'url': 'https://example.com/new-article', 'discovery_provider': 'fixture'}]}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    reader_entered, reader_release = asyncio.Event(), asyncio.Event()
    verdict_ready = asyncio.Event()
    calls = []

    async def read(service, item, sources, rejected, *, receipts):
        reader_entered.set()
        await reader_release.wait()
        receipts.append({'status': 'completed'})
        return [{'candidate_id': 'web:reader', 'name': 'New article', 'url': sources[0]['url'],
            'discovery': 'web_article_media', 'reference_image_urls': ['https://example.com/new-photo.jpg']}]

    monkeypatch.setattr('street_story.article_media.article_candidates', read)

    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        calls.append(cid)
        if cid == 'gate:b':
            await reader_entered.wait()
            await verdict_ready.wait()
            return response(item, 'ready-match', 'match')
        assert cid == 'gate:a'
        return response(item, 'first-negative')

    install(svc, pair)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await asyncio.wait_for(reader_entered.wait(), 2)
        if stop_during_reader:
            stop_research(svc, story['id'], purpose='identity')
            _, stopped = svc._identity_snapshot(story['id'])
            verdict_ready.set()
            reader_release.set()
            assert await task
            _, after = svc._identity_snapshot(story['id'])
            assert after['visual_search_operation'] == stopped['visual_search_operation']
            assert after['visual_identity']['status'] == 'uncertain'
            assert calls == ['gate:a', 'gate:b']
            return
        verdict_ready.set()
        await wait_match(svc, story['id'])
        assert not reader_release.is_set() and not task.done()
        assert calls == ['gate:a', 'gate:b']
        reader_release.set()
        assert await task
        assert calls == ['gate:a', 'gate:b']
        assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
        _, final = svc._identity_snapshot(story['id'])
        assert final['visual_search_operation']['lease_owner'] is None
        assert final['visual_search_operation']['lease_until'] == 0
    finally:
        verdict_ready.set()
        reader_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_with_only_unknown_child_acquires_late_source_on_already_free_lane(tmp_path, monkeypatch):
    svc, story, _ = prepare(tmp_path, count=2)
    calls = []
    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        calls.append((route, cid, item['_visual_pair_resume_only']))
        if cid == 'gate:b':
            if not item['_visual_pair_resume_only']:
                retained_unknown(svc, story, item, context)
            raise RetryableProviderError('research_visual_pair_outcome_unknown', retry_at=svc.store.now()+1)
        if cid == 'web:late':
            return response(item, 'late-completed')
        return response(item, 'first-negative')
    install(svc, pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert calls == [('google', 'gate:a', False), ('opencode', 'gate:b', False)]
    async def read(service, item, sources, rejected, *, receipts):
        assert sources[0]['url'] == 'https://example.com/late-article'
        receipts.append({'status': 'completed'})
        return [{'candidate_id': 'web:late', 'name': 'Late exterior',
                 'url': sources[0]['url'], 'discovery': 'web_article_media',
                 'reference_image_urls': ['https://example.com/late.jpg']}]
    monkeypatch.setattr('street_story.article_media.article_candidates', read)
    with svc.store.tx() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
        research['identity_article_discovery'] = {'photo_sha256': story['photo_sha256'], 'generation': 0,
            'sources': [{'url': 'https://example.com/late-article', 'discovery_provider': 'fixture'}]}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='identity_visual'")
    assert await svc.run_once(claim_kind='identity_visual')
    assert ('opencode', 'gate:b', True) in calls
    assert ('google', 'web:late', False) in calls
    _, latest = svc._identity_snapshot(story['id'])
    assert latest['visual_identity']['status'] == 'uncertain'
    late = next(p for p in latest['visual_search_operation']['parallel_pairs']
                if p['candidates'][0]['candidate_id'] == 'web:late')
    assert late['phase'] == 'completed'
    assert late['candidates'][0]['reference_id'] in latest['visual_search_operation']['reviewed_reference_ids']
    unknown = next(p for p in latest['visual_search_operation']['parallel_pairs'] if p['route'] == 'opencode')
    assert unknown['phase'] == 'submitted'
