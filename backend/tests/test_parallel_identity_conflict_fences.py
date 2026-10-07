import asyncio
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from test_parallel_identity_pairs import prepare, response


async def actual_peer_conflict(svc, story):
    async def pair(route, snapshot, item, schema, context):
        if route == 'opencode':
            await asyncio.sleep(.01)
        return response(item, route, 'match')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'), visual_pair_route=pair)
    assert await svc.run_once(claim_kind='identity_visual')
    result = svc.story(story['id'])
    assert result['error']['code'] == 'visual_identity_conflict'
    assert result['visual_identity']['status'] == 'uncertain'
    _, research = svc._identity_snapshot(story['id'])
    return research['visual_search_operation']


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['deferred_shortlist', 'discovery_final'])
async def test_concurrent_discovery_cannot_overwrite_actual_same_generation_peer_conflict(tmp_path, monkeypatch, boundary):
    svc, story, _ = prepare(tmp_path)
    _, initial = svc._identity_snapshot(story['id'])
    catalog = initial['visual_identity']['candidates']
    monkeypatch.setattr(svc, '_candidate_catalog', lambda *_args, **_kwargs: catalog)
    entered, released = asyncio.Event(), asyncio.Event()
    recovery_calls = []
    async def deferred(*_args):
        if boundary == 'deferred_shortlist':
            entered.set()
            await released.wait()
        return {'status':'uncertain', 'candidate_id':'', 'confidence':0, 'observations':[], '_comparison_deferred':True}
    async def recover(_service, snapshot, transcript, candidates, excluded):
        recovery_calls.append(True)
        entered.set()
        await released.wait()
        return {'status':'match', 'candidate_id':'gate:a', 'confidence':.99,
            'observations':['Distinct arch correspondence'], '_references_sent':['gate:a']}, catalog
    monkeypatch.setattr(svc, '_identify_photo', deferred)
    monkeypatch.setattr(identity_discovery, 'recover', recover)
    task = asyncio.create_task(svc.resolve_identity(story['id']))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        queue = await actual_peer_conflict(svc, story)
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET draft_text=? WHERE id=?', ('Existing draft', story['id']))
        released.set()
        await asyncio.wait_for(task, 2)
        final = svc.story(story['id'])
        assert final['state'] == 'needs_review'
        assert final['error']['code'] == 'visual_identity_conflict'
        assert final['visual_identity']['status'] == 'uncertain'
        assert final['draft_text'] == 'Existing draft'
        _, research = svc._identity_snapshot(story['id'])
        assert research['visual_search_operation'] == queue
        if boundary == 'deferred_shortlist':
            assert recovery_calls == []
        # A subsequent automatic discovery invocation is fenced too.
        await svc.resolve_identity(story['id'], owner_hint='fresh hint')
        _, unchanged = svc._identity_snapshot(story['id'])
        assert unchanged['visual_search_operation'] == queue
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_existing_explicit_identity_correction_allows_new_generation(tmp_path, monkeypatch):
    svc, story, _ = prepare(tmp_path)
    await actual_peer_conflict(svc, story)
    corrected = svc.reject_identity(story['id'], 'gate:a', 'Owner correction')
    assert corrected['visual_identity']['generation'] == 1
    assert not corrected.get('error')
    candidate = {'candidate_id':'gate:b','name':'Gate b','distance_m':10,
        'url':'https://example.com/b','reference_image_urls':['https://example.com/b.jpg']}
    monkeypatch.setattr(svc, '_candidate_catalog', lambda *_args, **_kwargs: [candidate])
    async def valid(*_args):
        return {'status':'match','candidate_id':'gate:b','confidence':.99,
            'observations':['Distinct arch correspondence'],'_references_sent':['gate:b']}
    monkeypatch.setattr(svc, '_identify_photo', valid)
    await svc.resolve_identity(story['id'], expected_generation=1)
    final = svc.story(story['id'])
    assert final['visual_identity']['status'] == 'match'
    assert final['visual_identity']['candidate_id'] == 'gate:b'
    assert final['visual_identity']['generation'] == 1
    assert not final.get('error')
