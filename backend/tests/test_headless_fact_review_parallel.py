import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story import review_packets
from street_story.headless_fact_review import HeadlessFactReview
from street_story.headless_facts import HeadlessFacts
from street_story.service import ConflictError
from test_headless_fact_pool import fixture, result, RUN


async def candidates(tmp_path, count=6):
    svc, job = fixture(tmp_path, count=count)
    async def extract(page, story, context):
        return result(page)
    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    from street_story.errors import RetryableProviderError
    while True:
        try:
            await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
            break
        except RetryableProviderError:
            pass
    return svc, job, HeadlessFacts(svc)


class ControlledReview(HeadlessFactReview):
    active = 0
    peak = 0
    calls = 0
    mode = 'positive'

    async def _infer(self, packet, job, unit, saved, ordinal=0):
        if saved.get('phase') in {'unknown', 'started'}:
            return None
        type(self).calls += 1
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        await asyncio.sleep(.02)
        type(self).active -= 1
        if type(self).mode == 'unknown':
            self._put(job, unit, {'phase': 'unknown', 'packet_ref': packet['packet_ref']})
            return None
        decisions=[]
        for fact in sorted({r['fact'] for r in packet['items']}):
            rows=[r for r in packet['items'] if r['fact']==fact]
            item=rows[0]
            decisions.append({'fact':fact,'evidence':sorted({r['evidence'] for r in rows}),
                'verdict':'supported','atomic':True,'support_complete':True,'qualifiers_preserved':True,
                'claims':[item['text']],'basis_quotes':[item['text']], 'reason':'Controlled own exact passage.'})
        args={'packet_ref':packet['packet_ref'],'decisions':decisions,'relations_complete':True,
              'conflicts':[],'coverage_complete':False,'missing_aspects':[]}
        self._put(job, unit, {'phase':'result','args':args})
        return args


@pytest.fixture(autouse=True)
def reset_host():
    ControlledReview.active=ControlledReview.peak=ControlledReview.calls=0
    ControlledReview.mode='positive'


@pytest.mark.asyncio
async def test_independent_packets_stay_pending_but_new_eligible_claim_stales_sibling(tmp_path):
    svc, job, harness=await candidates(tmp_path)
    session=SimpleNamespace(id='review',resource_id=job['story_id'],model='gemini-3.8-live',actor=None,closed=False,state={})
    with svc.store.connection() as db:
        ids=review_packets.pending_candidates(db,job['story_id'],RUN)
    packets=[review_packets.read(harness.adapter,session,{'run_id':RUN,'_candidate_ids':ids[i:i+3],
        '_parallel_candidate_review':True}) for i in (0,3)]
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM live_review_attempts WHERE state='pending'").fetchone()[0]==2
        for packet in packets:
            review_packets.load(harness.adapter,session,db,packet['packet_ref'])
    # Unrelated background candidate revision does not spoil own frozen review.
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?',(job['story_id'],))
    with svc.store.connection() as db:
        review_packets.load(harness.adapter,session,db,packets[0]['packet_ref'])
    with svc.store.tx() as db:
        db.execute("UPDATE fact_assertions SET eligibility='eligible' WHERE story_id=? AND assertion_id=?",(job['story_id'],ids[0]))
    with svc.store.connection() as db:
        with pytest.raises(ConflictError,match='Revisions changed'):
            review_packets.load(harness.adapter,session,db,packets[1]['packet_ref'])


@pytest.mark.asyncio
async def test_two_live_reviews_overlap_serial_commit_refreshes_nearby_claims_and_preserves_owner(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    sid=job['story_id']
    with svc.store.tx() as db:
        row=svc._story_row(db,sid)
        research=json.loads(row['research_json'])
        research['publication_concept']='Owner concept'
        db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?',('Owner draft',json.dumps(research),sid))
    engine=ControlledReview(harness)
    assert await engine.run(job,RUN,0)==1
    assert ControlledReview.peak==2 and ControlledReview.calls==2
    with svc.store.connection() as db:
        eligible=db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]
        assert eligible==3
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]==3
        assert svc._story_row(db,sid)['draft_text']=='Owner draft'
    # The deferred sibling now sees the first accepted claims, rather than
    # committing a semantic decision based on a stale POI view.
    assert await engine.run(job,RUN,0)==1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]==6
        assert json.loads(svc._story_row(db,sid)['research_json'])['publication_concept']=='Owner concept'


@pytest.mark.asyncio
async def test_unknown_review_is_not_repeated_and_other_scope_can_finish(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    ControlledReview.mode='unknown'
    engine=ControlledReview(harness)
    assert await engine.run(job,RUN,0)==0
    assert ControlledReview.calls==2
    ControlledReview.mode='positive'
    assert await engine.run(job,RUN,0)==0
    assert ControlledReview.calls==2


@pytest.mark.asyncio
async def test_owner_selection_change_stales_private_background_packet(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    session=SimpleNamespace(id='review',resource_id=job['story_id'],actor=None,closed=False,state={})
    packet=review_packets.read(harness.adapter,session,{'run_id':RUN,'_parallel_candidate_review':True})
    with svc.store.tx() as db:
        db.execute('UPDATE fact_assertions SET owner_selected=1 WHERE story_id=?',(job['story_id'],))
    with svc.store.connection() as db:
        with pytest.raises(ConflictError):
            review_packets.load(harness.adapter,session,db,packet['packet_ref'])


@pytest.mark.asyncio
async def test_good_fact_enters_poi_while_slow_extraction_siblings_still_run(tmp_path, monkeypatch):
    svc,job=fixture(tmp_path,count=3)
    release=asyncio.Event()
    active=asyncio.Event()
    async def extract(page,story,context):
        if page['_extractor_ordinal']:
            active.set()
            await release.wait()
        return result(page)
    svc.providers.research=SimpleNamespace(client=None,extract_fact_page=extract)
    harness=HeadlessFacts(svc)
    async def review(job,run_id,control_revision):
        return await ControlledReview(harness).run(job,run_id,control_revision)
    monkeypatch.setattr(harness,'_review_candidates',review)
    task=asyncio.create_task(harness.run(job,RUN,'History','history'))
    try:
        await asyncio.wait_for(active.wait(),1)
        for _ in range(30):
            with svc.store.connection() as db:
                eligible=db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]
            if eligible:
                break
            await asyncio.sleep(.05)
        assert eligible==1 and not task.done()
        release.set()
        await asyncio.wait_for(task,3)
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]==3
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_repeated_local_capacity_polling_backs_off_without_model_send_or_owner_loss(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    svc,job=fixture(tmp_path,count=3)
    current=svc.store.now()
    monkeypatch.setattr(svc.store,'now',lambda: current)
    calls=[]
    async def no_capacity(page,story,context):
        calls.append(page['_unit_id'])
        error=RetryableProviderError('research_fact_pool_waiting',retry_at=current+3)
        error.route_failures=['RESOURCE_NO_CAPACITY','RESOURCE_NO_CAPACITY']
        raise error
    svc.providers.research=SimpleNamespace(client=None,extract_fact_page=no_capacity)
    harness=HeadlessFacts(svc)
    monkeypatch.setattr(harness,'_boundary_closed',lambda *args:True)
    for expected in (5,10,20,40,60,60):
        with pytest.raises(RetryableProviderError) as failure:
            await harness.run(job,RUN,'History','history')
        assert failure.value.retry_at >= current+expected
        current=failure.value.retry_at+1
    assert len(calls)==18
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0]==0
        assert svc._story_row(db,job['story_id'])['state']!='needs_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['malformed','unknown','aborted_unknown'])
async def test_semantic_text_pool_closed_fallback_and_unknown_fence_are_distinct(tmp_path,mode):
    from contextvars import ContextVar
    from street_story.research_adapter import ProductResearchAdapter
    from street_story.opencode_research import ResearchUnavailable
    svc,job,harness=await candidates(tmp_path,count=3)
    provider=object.__new__(ProductResearchAdapter)
    provider.service=svc
    provider._active_binding=ContextVar('semantic-test',default=None)
    provider.giga=None
    calls=[]
    class Client:
        endpoint='http://127.0.0.1:4097'
        provider_id='opencode'
        directory='/existing/research'
        def __init__(self,model_id):
            self.model_id=model_id
        async def _run(self,role,prompt,binding,schema):
            calls.append(self.model_id)
            packet=json.loads(prompt.split('Frozen packet: ',1)[1])
            if self.model_id=='mimo-v2.6-flash-free' and mode in {'unknown','aborted_unknown'}:
                receipt={'binding':binding,'phase':'aborted' if mode=='aborted_unknown' else 'submitted','session_id':'ses_existing',
                         'message_id':'msg_existing','model_id':self.model_id}
                await provider.checkpoint(binding,receipt)
                raise ResearchUnavailable('research_attempt_unknown',receipt)
            if self.model_id=='mimo-v2.6-flash-free':
                args={'decisions':'broken'}
            else:
                args={'packet_ref':packet['packet_ref'],'decisions':[],
                    'relations_complete':True,'conflicts':[],'coverage_complete':False,'missing_aspects':[]}
            receipt={'binding':binding,'phase':'completed','result':args,'model_id':self.model_id}
            await provider.checkpoint(binding,receipt)
            return {'result':args,'receipt':receipt}
    provider.client=Client('mimo-v2.6-flash-free')
    extra=Client('nemotron-3-ultra-free')
    provider._fact_extractor_clients={extra.model_id:extra}
    fields={'semantic_contract_verified':True,'source_subject_negative_verified':True,
            'planned_modality_verified':True,'known_claim_reuse_verified':True,
            'schema_verified':True,'own_passages_verified':True,'qualifier_negative_verified':True,
            'nearby_duplicate_verified':True,'nearby_conflict_verified':True}
    entries=[{'provider_id':'opencode','model_id':client.model_id,'endpoint':client.endpoint,**fields}
             for client in (provider.client,extra)]
    svc.store.cache_put('research-text-verification-v1',{'extractors':entries},ttl_seconds=3600)
    svc.store.cache_put('fact-semantic-verification-v1',{'routes':entries},ttl_seconds=3600)
    svc.providers.research=provider
    session=SimpleNamespace(id='review',resource_id=job['story_id'],actor=None,closed=False,state={})
    packet=review_packets.read(harness.adapter,session,{'run_id':RUN,'_parallel_candidate_review':True})
    engine=HeadlessFactReview(harness)
    response=await engine._infer(packet,job,'semantic-controlled-unit',{})
    assert calls==['mimo-v2.6-flash-free']+([] if mode in {'unknown','aborted_unknown'} else ['nemotron-3-ultra-free'])
    assert (response is None)==(mode in {'unknown','aborted_unknown'})
    if mode in {'unknown','aborted_unknown'}:
        saved=svc.store.checkpoint_get(job['id'],'headless_fact_review:semantic-controlled-unit')
        assert await engine._infer(packet,job,'semantic-controlled-unit',saved) is None
        assert len(calls)==1
