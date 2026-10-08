import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.service import canonical
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


def prepare(tmp_path, count=2):
    svc, _ = make_service(tmp_path)
    photo = jpeg()
    story = svc.create_story(key='parallel', client_story_id='parallel',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    candidates = [{'candidate_id': f'gate:{label}', 'name': f'Gate {label}', 'distance_m': distance,
        'url': f'https://example.com/{label}', 'reference_image_urls': [f'https://example.com/{label}.jpg']}
        for label, distance in list(zip('abcd', (5, 10, 15, 20)))[:count]]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET state=?,research_json=? WHERE id=?', ('identifying',canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), story['id']))
    async def images(candidates, limit, *, story_id, evidence):
        c = candidates[0]
        evidence.append({'candidate_id': c['candidate_id'], 'source_url': c['reference_image_urls'][0]})
        return [(c['candidate_id'], 'image/jpeg', c['reference_image_urls'][0])]
    svc._candidate_reference_images = images
    return svc, story, photo


def response(story, model, status='mismatch'):
    candidate_id = story['_visual_reference_mapping'][0]['candidate_id']
    return {'result': {'status': status, 'candidate_id': candidate_id if status == 'match' else '',
        'confidence': .99, 'observations': ['Distinct arch and facade correspondence' if status=='match' else 'Different arch and facade'],
        'alternative_candidate_ids': []}, 'receipt': {'model': model, 'phase': 'completed', 'operation_id': model+'-op'}}


async def wait_match(svc, sid):
    async def poll():
        while svc.story(sid)['visual_identity']['status'] != 'match':
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
async def test_independent_pairs_overlap_and_early_match_atomically_retains_submitted_peer(tmp_path):
    svc, story, photo = prepare(tmp_path)
    release = asyncio.Event()
    calls = []
    active = peak = 0
    async def pair(route, snapshot, item, schema, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append((route, json.loads(context)['comparison_id']))
        assert len(item['_visual_image_parts']) == 2
        assert len(item['_visual_reference_mapping']) == 1
        assert item['_visual_image_parts'][1]['label'] == 'REF 1'
        assert not item['_visual_pair_resume_only']
        try:
            if route == 'google':
                await asyncio.sleep(0)
                return response(item, 'google-model', 'match')
            await release.wait()
            return response(item, 'opencode-model')
        finally:
            active -= 1
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'), visual_pair_route=pair)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await wait_match(svc, story['id'])
        assert not task.done()
        _, r = svc._identity_snapshot(story['id'])
        queue = r['visual_search_operation']
        assert [p['phase'] for p in queue['parallel_pairs']] == ['completed','submitted']
        assert queue['parallel_pairs'][1]['route'] == 'opencode'
        assert queue['parallel_pairs'][1]['id'] == calls[1][1]
        assert queue['lease_owner']
        assert 'image_parts' not in canonical(queue) and 'data' not in queue
        assert peak == 2
        release.set()
        assert await task
        result = svc.story(story['id'])
        assert result['visual_identity']['candidate_id'] == 'gate:a'
        assert result['visual_identity']['provider_receipt']['model'] == 'google-model'
        assert result['identity_progress']['images_reviewed_count'] == 2
        _, r = svc._identity_snapshot(story['id'])
        assert [p['phase'] for p in r['visual_search_operation']['parallel_pairs']] == ['completed','completed']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_accepted_restart_observes_original_unknown_peer_without_source_or_new_send(tmp_path):
    svc, story, _ = prepare(tmp_path)
    first_calls, resumed_calls = [], []
    async def first(route, snapshot, item, schema, context):
        first_calls.append(json.loads(context)['comparison_id'])
        if route == 'google':
            return response(item, 'google-model', 'match')
        await asyncio.sleep(0)
        raise RetryableProviderError('original_peer_unknown', retry_at=svc.store.now()+1)
    provider=SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'), visual_pair_route=first)
    svc.providers.research=provider
    assert await svc.run_once(claim_kind='identity_visual')
    assert svc.story(story['id'])['visual_identity']['status'] == 'match'
    _, research = svc._identity_snapshot(story['id'])
    pairs=research['visual_search_operation']['parallel_pairs']
    assert pairs[1]['phase'] == 'submitted'
    original_id=pairs[1]['id']
    async def resumed(route, snapshot, item, schema, context):
        resumed_calls.append(json.loads(context)['comparison_id'])
        assert route == 'opencode'
        assert item['_visual_pair_resume_only'] and item['_visual_pair_observe_only']
        assert 'data' not in item['_visual_image_parts'][0]
        assert item['_visual_source_unavailable']
        return response(item, 'original-opencode-model')
    provider.visual_pair_route=resumed
    provider.vision_available=False
    def absent(_id):
        from street_story.service import ConflictError
        raise ConflictError('source_unavailable','SOURCE lost on restart')
    svc._source_photo_bytes=absent
    now = svc.store.now()
    svc.store.now = lambda: now+2  # Original observer cooldown has elapsed.
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE story_id=? AND kind='identity_visual'",(story['id'],))
    assert await svc.run_once(claim_kind='identity_visual')
    assert resumed_calls == [original_id]
    assert len(first_calls) == 2
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:a'
    with svc.store.connection() as db:
        job=dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity_visual'",(story['id'],)).fetchone())
    assert job['state'] == 'done'


@pytest.mark.asyncio
async def test_actual_different_poi_peer_matches_surface_conflict_and_preserve_editorial_state(tmp_path):
    svc, story, _ = prepare(tmp_path)
    released = asyncio.Event()
    async def pair(route, snapshot, item, schema, context):
        if route == 'opencode':
            await released.wait()
        return response(item, route+'-model', 'match')
    svc.providers.research=SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'), visual_pair_route=pair)
    task=asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await wait_match(svc, story['id'])
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET draft_text=? WHERE id=?', ('Owner draft is preserved', story['id']))
            r=json.loads(db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()[0])
            r.update(owner_fact_selection=['fact-owner'], owner_concept={'text':'Owner angle'})
            db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(r),story['id']))
        released.set()
        assert await task
        result=svc.story(story['id'])
        assert result['state']=='needs_review'
        assert result['error']['code']=='visual_identity_conflict'
        assert result['visual_identity']['status']=='uncertain'
        assert not result['visual_identity']['visual_reference_verified']
        assert result['draft_text']=='Owner draft is preserved'
        _, r=svc._identity_snapshot(story['id'])
        assert r['owner_fact_selection']==['fact-owner'] and r['owner_concept']['text']=='Owner angle'
        assert len(r['visual_search_operation']['verdict_history'])==2
        assert r['visual_search_operation']['verdict_history'][-1]['conflicts_with_comparison_id']
        assert all(p['phase']=='completed' for p in r['visual_search_operation']['parallel_pairs'])
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_source_keeps_accepted_peer_pending_for_android_reupload(tmp_path):
    svc, story, photo = prepare(tmp_path)
    calls=[]
    async def pair(route, snapshot, item, schema, context):
        calls.append((json.loads(context)['comparison_id'], item['_visual_pair_resume_only'], item['_visual_source_unavailable']))
        if route=='google':
            return response(item, 'google-model','match')
        if item['_visual_source_unavailable']:
            raise RetryableProviderError('research_visual_pair_source_required',retry_at=svc.store.now()+1)
        if not item['_visual_pair_resume_only']:
            raise RetryableProviderError('original_peer_unknown',retry_at=svc.store.now()+1)
        return response(item,'original-readback')
    svc.providers.research=SimpleNamespace(vision_available=True,vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'),visual_pair_route=pair)
    assert await svc.run_once(claim_kind='identity_visual')
    _,r=svc._identity_snapshot(story['id'])
    peer_id=r['visual_search_operation']['parallel_pairs'][1]['id']
    original_source=svc._source_photo_bytes
    def absent(_id):
        from street_story.service import ConflictError
        raise ConflictError('source_unavailable','RAM unavailable')
    svc._source_photo_bytes=absent
    now = svc.store.now()
    svc.store.now = lambda: now+2
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='identity_visual'")
    assert await svc.run_once(claim_kind='identity_visual')
    result=svc.story(story['id'])
    assert result['visual_identity']['status']=='match'
    assert result['research_pending']['identity']
    _,r=svc._identity_snapshot(story['id'])
    assert r['visual_search_operation']['parallel_pairs'][1]['phase']=='submitted'
    assert calls[-1]==(peer_id,True,True)
    svc._source_photo_bytes=original_source
    svc.store.now = lambda: now+4
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='identity_visual'")
    assert await svc.run_once(claim_kind='identity_visual')
    assert calls[-1]==(peer_id,True,False)
    assert not svc.story(story['id'])['research_pending']['identity']


@pytest.mark.asyncio
async def test_wired_real_provider_dispatch_uses_two_exact_attempts_and_individual_receipts(tmp_path):
    from street_story.research_adapter import ProductResearchAdapter
    svc,story,_=prepare(tmp_path)
    adapter=ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service=svc
    async def google(snapshot,item,schema,context):
        await asyncio.sleep(0)
        return response(item,'actual-google-route','match')
    adapter.primary_vision=SimpleNamespace(available=True,compare_visual=google,
        _verified_routes=lambda: [('actual-google-route', None)])
    class Client:
        endpoint,model_id,provider_id='http://127.0.0.1:4097','mimo-v2.6-flash-free','opencode'
        async def compare_image(self,parts,binding,schema,context):
            assert [p['label'] for p in parts]==['SOURCE','REF 1']
            item={'_visual_reference_mapping':json.loads(context)['references']}
            result=response(item,'actual-opencode-route')
            receipt={**result['receipt'],'binding':binding,'result':result['result']}
            await adapter.checkpoint(binding,receipt)
            return {**result,'receipt':receipt}
    adapter.client=Client()
    adapter.native_vision=None
    svc.store.cache_put('research-vision-verification-v1',{'model_id':adapter.client.model_id,
        'endpoint':adapter.client.endpoint,'positive':'match','negative':'mismatch','pixel_transport_verified':True},3600)
    svc.providers.research=adapter
    assert await svc.run_once(claim_kind='identity_visual')
    assert svc.story(story['id'])['visual_identity']['status']=='match'
    with svc.store.connection() as db:
        rows=list(db.execute('SELECT role,receipt_json FROM research_provider_attempts WHERE story_id=?',(story['id'],)))
    assert {r['role'] for r in rows}=={'vision_google_pair','vision'}
    assert len(rows)==2 and all(json.loads(r['receipt_json'])['phase']=='completed' for r in rows)
    _,r=svc._identity_snapshot(story['id'])
    assert [p['receipt']['model'] for p in r['visual_search_operation']['parallel_pairs']]==['actual-google-route','actual-opencode-route']


@pytest.mark.asyncio
async def test_late_parallel_results_do_not_damage_replacement_generation(tmp_path):
    svc,story,_=prepare(tmp_path)
    entered,released=asyncio.Event(),asyncio.Event()
    count=0
    async def pair(route,snapshot,item,schema,context):
        nonlocal count
        count+=1
        if count==2:
            entered.set()
        await released.wait()
        return response(item,route+'-model','match')
    svc.providers.research=SimpleNamespace(vision_available=True,vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'),visual_pair_route=pair)
    task=asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    await asyncio.wait_for(entered.wait(),2)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET state=?,draft_text=?,research_json=? WHERE id=?',
            ('photo_ready','Replacement editorial content',canonical({'identity_generation':1,
                'visual_identity':{'status':'uncertain','candidates':[]}}),story['id']))
    released.set()
    assert await task
    result=svc.story(story['id'])
    assert result['state']=='photo_ready' and result['draft_text']=='Replacement editorial content'
    assert result['visual_identity']['status']=='uncertain'
    _,r=svc._identity_snapshot(story['id'])
    assert r['identity_generation']==1 and not r.get('visual_search_operation')


@pytest.mark.asyncio
async def test_same_poi_alias_peer_match_does_not_create_conflict(tmp_path):
    svc,story,_=prepare(tmp_path)
    with svc.store.tx() as db:
        r=json.loads(db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()[0])
        for candidate in r['visual_identity']['candidates']:
            candidate['wikidata']='Q1234'
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(r),story['id']))
    async def pair(route,snapshot,item,schema,context):
        await asyncio.sleep(0)
        return response(item,route+'-model','match')
    svc.providers.research=SimpleNamespace(vision_available=True,vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'),visual_pair_route=pair)
    assert await svc.run_once(claim_kind='identity_visual')
    result=svc.story(story['id'])
    assert result['visual_identity']['status']=='match'
    assert result.get('error') is None


@pytest.mark.asyncio
async def test_unknown_existing_parent_is_not_split_into_fresh_child_sends(tmp_path):
    from street_story.headless_identity import HeadlessIdentity
    from street_story.service import digest
    from street_story.visual_attachments import visual_operation_unit
    svc,story,_=prepare(tmp_path)
    calls=[]
    async def forbidden(*args):
        pytest.fail('unknown parent cannot become fresh independent child sends')
    async def original(snapshot,item,schema,context):
        calls.append(json.loads(context)['comparison_id'])
        raise RetryableProviderError('original_parent_unknown',retry_at=svc.store.now()+30)
    svc.providers.research=SimpleNamespace(vision_available=True,vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode'),visual_pair_route=forbidden,visual_verdict=original)
    svc._schedule_identity_visual()
    job=svc._claim(claim_kind='identity_visual')
    worker=HeadlessIdentity(svc)
    session=SimpleNamespace(id='headless:prepared-original',resource_id=story['id'],state={},model='fixture',visual_reference_limit=2)
    pending_reply=await worker._compare_place_images(session,{})
    pending=session.state['visual_comparison']['pending']
    addressed={**story,'_identity_generation':0,'_visual_reference_mapping':pending_reply['references']}
    unit=canonical(visual_operation_unit(addressed,pending_reply))
    logical=digest([story['id'],0,'vision_google_group',unit])
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('old-parent',logical,story['id'],'vision_google_group',canonical({'phase':'unknown',
                'binding':{'story_id':story['id'],'visual_scope':True,'generation':0},
                'provider_send_state':'possibly_sent'}),svc.store.now(),svc.store.now()))
    scope={'photo_sha256':story['photo_sha256'],'generation':0,'control_revision':0}
    worker._release_visual_lease(session,scope)
    with pytest.raises(RetryableProviderError,match='original_parent_unknown'):
        await worker.run(job)
    assert calls==[pending['id']]
    _,r=svc._identity_snapshot(story['id'])
    assert not r['visual_search_operation'].get('parallel_pairs')
    assert r['visual_search_operation']['pending_descriptor']['id']==pending['id']


@pytest.mark.asyncio
async def test_single_initial_unknown_keeps_original_lane_and_accepts_late_independent_pair(tmp_path):
    from street_story.headless_identity import HeadlessIdentity
    svc, story, _ = prepare(tmp_path, count=1)
    now = svc.store.now()
    clock = [now]
    svc.store.now = lambda: clock[0]
    calls = []
    async def pair(route, snapshot, item, schema, context):
        calls.append((route, json.loads(context)['comparison_id']))
        if route == 'google':
            assert not item['_visual_pair_resume_only']
            raise RetryableProviderError('research_visual_pair_outcome_unknown', retry_at=clock[0]+300)
        return response(item, 'independent-model', 'match')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google', 'opencode'), visual_pair_route=pair)
    with svc.store.tx() as db:
        discovery = svc._enqueue_job(db, story['id'], 'identity', 'late-discovery', {'identity_generation': 0})
        db.execute("UPDATE jobs SET state='running',lease_until=? WHERE id=?", (now+180, discovery))
    assert await svc.run_once(claim_kind='identity_visual')
    _, research = svc._identity_snapshot(story['id'])
    frozen = dict(research['visual_search_operation']['parallel_pairs'][0])
    assert len(calls) == 1 and frozen['phase'] == 'submitted'
    assert frozen['retry_at'] == now+300
    assert 'automatic_research_outcome' not in research
    # A later independent discovery supplies another exact facade. It never
    # rewrites the original UNKNOWN comparison or its provider cooldown.
    later = {'candidate_id': 'gate:b', 'name': 'Gate b', 'url': 'https://example.com/b',
             'reference_image_urls': ['https://example.com/b.jpg'], 'distance_m': 10}
    research['visual_identity']['candidates'].append(later)
    research['visual_search_operation']['queue'].extend(HeadlessIdentity._image_entries(later))
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='identity_visual' AND story_id=?", (story['id'],))
    clock[0] += 6
    assert await svc.run_once(claim_kind='identity_visual')
    assert [route for route, _ in calls] == ['google', 'opencode']
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
    _, research = svc._identity_snapshot(story['id'])
    original = research['visual_search_operation']['parallel_pairs'][0]
    assert original['id'] == frozen['id'] and original['reply'] == frozen['reply']
    assert original['phase'] == 'submitted' and original['retry_at'] == frozen['retry_at']


@pytest.mark.asyncio
async def test_proven_unsent_admission_wait_retries_original_pair_as_first_send_after_due(tmp_path):
    svc, story, _ = prepare(tmp_path, count=1)
    clock = [svc.store.now()]
    svc.store.now = lambda: clock[0]
    receipts, calls = {}, []
    async def pair(route, snapshot, item, schema, context):
        identifier = json.loads(context)['comparison_id']
        calls.append((identifier, item['_visual_pair_resume_only']))
        if len(calls) == 1:
            receipts['vision_native'] = {'phase': 'created', 'provider_send_state': 'not_sent',
                'retry_safe': True, 'binding': {'attempt_id': 'same-native-attempt'}}
            raise RetryableProviderError('research_vision_waiting', retry_at=clock[0]+3)
        return response(item, 'native-model', 'match')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('native', 'google'), visual_pair_route=pair,
        visual_pair_receipts=lambda *_args: receipts)
    assert await svc.run_once(claim_kind='identity_visual')
    _, research = svc._identity_snapshot(story['id'])
    original = research['visual_search_operation']['parallel_pairs'][0]
    assert original['phase'] == 'ready' and original['retry_at'] == clock[0]+3
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE story_id=? AND kind='identity_visual'", (story['id'],))
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(calls) == 1  # Local queue polling cannot bypass authority due.
    clock[0] += 4
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE story_id=? AND kind='identity_visual'", (story['id'],))
    assert await svc.run_once(claim_kind='identity_visual')
    assert calls == [(original['id'], False), (original['id'], False)]
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:a'


@pytest.mark.asyncio
async def test_four_qualified_lanes_commit_first_then_keep_real_conflict_after_later_matches(tmp_path):
    svc, story, _ = prepare(tmp_path, count=4)
    releases = {label: asyncio.Event() for label in 'bcd'}
    entered = asyncio.Event()
    calls = []
    active = peak = 0
    async def pair(route, snapshot, item, schema, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        label = item['_visual_reference_mapping'][0]['candidate_id'].split(':')[-1]
        calls.append((route, json.loads(context)['comparison_id']))
        if len(calls) == 4:
            entered.set()
        try:
            await entered.wait()
            if label != 'a':
                await releases[label].wait()
            return response(item, route+'-'+label, 'match')
        finally:
            active -= 1
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google','opencode','google','native'), visual_pair_route=pair)
    task = asyncio.create_task(svc.run_once(claim_kind='identity_visual'))
    try:
        await wait_match(svc, story['id'])
        _, research = svc._identity_snapshot(story['id'])
        pairs = research['visual_search_operation']['parallel_pairs']
        assert peak == 4 and len({item[1] for item in calls}) == 4
        assert [p['phase'] for p in pairs] == ['completed','submitted','submitted','submitted']
        releases['b'].set()
        async def wait_conflict():
            while (svc.story(story['id']).get('error') or {}).get('code') != 'visual_identity_conflict':
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_conflict(), 2)
        for event in releases.values():
            event.set()
        assert await task
        final = svc.story(story['id'])
        assert final['visual_identity']['status'] == 'uncertain'
        assert final['error']['code'] == 'visual_identity_conflict'
        assert not final['identity_progress']['visual_comparison_verified']
        assert final['identity_progress']['finished']
        _, research = svc._identity_snapshot(story['id'])
        assert len(research['visual_search_operation']['verdict_history']) == 4
        assert all(p['phase']=='completed' for p in research['visual_search_operation']['parallel_pairs'])
        before = len(calls)
        with svc.store.tx() as db:
            db.execute("UPDATE jobs SET state='retry',available_at=0 WHERE story_id=? AND kind='identity_visual'", (story['id'],))
        assert await svc.run_once(claim_kind='identity_visual')
        assert len(calls) == before
    finally:
        for event in releases.values():
            event.set()
        await asyncio.gather(task, return_exceptions=True)
