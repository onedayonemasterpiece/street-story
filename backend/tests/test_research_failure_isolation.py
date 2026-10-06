"""Provider failure records progress without discarding confirmed editorial value."""
import hashlib
import json
import pytest
from street_story.errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from street_story.research_runs import begin_research_run
from street_story.service import MAX_JOB_ATTEMPTS, canonical
from test_fact_request_recovery import pending_request


def prepared(tmp_path, *, priority='interactive', kind='research', state='researching', draft='Owner draft'):
    svc,sid,jid=pending_request(tmp_path)
    with svc.store.tx() as db:
        research=json.loads(svc._story_row(db,sid)['research_json'])
        research.pop('pending_fact_request',None)
        db.execute('UPDATE stories SET state=?,draft_text=?,research_json=? WHERE id=?',(state,draft,canonical(research),sid))
        row=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        payload=json.loads(row['payload_json'])
        payload['queue_priority']=priority
        db.execute('UPDATE jobs SET kind=?,payload_json=? WHERE id=?',(kind,canonical(payload),jid))
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,'saved','Retained eligible fact',.9,1,1,'[]')",(sid,))
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES(?,'saved','saved','Retained eligible fact',1,'eligible','eligible',1,1)",(sid,))
        run='research_'+hashlib.sha256(f"{jid}:{payload['input_revision']}".encode()).hexdigest()[:24]
        begin_research_run(db,story_id=sid,poi_key='wiki:1',goal='Frozen scope',expected_story_revision=1,
                           identity_generation=0,run_id=run,now=1)
    return svc,sid,jid,run


def snapshot(svc,sid,jid,run):
    with svc.store.connection() as db:
        story=dict(db.execute('SELECT state,error_code,draft_text,research_json FROM stories WHERE id=?',(sid,)).fetchone())
        story['concept']=json.loads(story.pop('research_json'))['publication_concept']
        return (story,dict(db.execute('SELECT state,last_error FROM jobs WHERE id=?',(jid,)).fetchone()),
            dict(db.execute('SELECT state,status_detail,completed_at FROM research_runs WHERE run_id=?',(run,)).fetchone()),
            db.execute('SELECT selected FROM facts WHERE story_id=?',(sid,)).fetchone()[0],
            db.execute('SELECT owner_selected,eligibility FROM fact_assertions WHERE story_id=?',(sid,)).fetchone()[:])


@pytest.mark.asyncio
@pytest.mark.parametrize('priority,kind',[('background','research'),('interactive','research'),(None,'research'),('interactive','refinement')])
@pytest.mark.parametrize('failure',[PermanentProviderError('gigachat:closed_semantic_unit_requires_live'),MalformedProviderResponse('gigachat:semantic_json_invalid')])
async def test_worker_failure_isolated_for_all_fact_routes(tmp_path,priority,kind,failure):
    svc,sid,jid,run=prepared(tmp_path,priority=priority,kind=kind)
    async def fail(job):
        raise failure
    svc._run_research=fail
    assert await svc.run_once()
    story,job,research,selected,assertion=snapshot(svc,sid,jid,run)
    assert story=={'state':'review','error_code':None,'draft_text':'Owner draft','concept':'Owner concept'}
    assert job['state']==('failed' if isinstance(failure,PermanentProviderError) else 'retry')
    assert research['state']=='partial' and research['completed_at'] is None
    assert selected==1 and assertion==(1,'eligible')


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['facts_ready','review','generating_visual','scheduling','scheduled','published'])
async def test_background_failure_preserves_current_product_stage_and_editorial(tmp_path,state):
    svc,sid,jid,run=prepared(tmp_path,priority='background',state=state)
    async def fail(job):
        raise RuntimeError('Actual extraction worker failure')
    svc._run_research=fail
    assert await svc.run_once()
    story,job,research,selected,assertion=snapshot(svc,sid,jid,run)
    assert story['state']==state and story['error_code'] is None
    assert story['draft_text']=='Owner draft' and story['concept']=='Owner concept'
    assert job['state']=='retry' and job['last_error']=='worker_failure:RuntimeError'
    assert research['state']=='partial' and selected==1 and assertion==(1,'eligible')


@pytest.mark.asyncio
@pytest.mark.parametrize('restart',[False,True])
async def test_actual_failure_exhaustion_leaves_job_failed_but_story_usable(tmp_path,restart):
    svc,sid,jid,run=prepared(tmp_path)
    svc.store.checkpoint_put(jid,'worker_non_wait_failures',{'count':MAX_JOB_ATTEMPTS if restart else MAX_JOB_ATTEMPTS-1})
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state=?,attempts=?,lease_until=0,last_error='worker_failure:RuntimeError' WHERE id=?",
                   ('running' if restart else 'ready',MAX_JOB_ATTEMPTS if restart else MAX_JOB_ATTEMPTS-1,jid))
    if restart:
        assert svc.recover_jobs()==1
    else:
        async def fail(job):
            raise RuntimeError('Actual worker failure')
        svc._run_research=fail
        assert await svc.run_once()
    story,job,research,selected,assertion=snapshot(svc,sid,jid,run)
    assert job['state']=='failed' and research['state']=='partial' and research['completed_at'] is None
    assert story['state']=='review' and story['error_code'] is None
    assert selected==1 and assertion==(1,'eligible')


@pytest.mark.asyncio
async def test_unknown_retry_keeps_same_frozen_attempt_and_never_consumes_failure_budget(tmp_path):
    svc,sid,jid,run=prepared(tmp_path,draft=None)
    frozen={'phase':'submitted','provider_send_state':'possibly_sent','binding':{'job_id':jid}}
    svc.store.checkpoint_put(jid,'unknown_model_attempt',frozen)
    calls=[]
    async def recover_only(job):
        assert svc.store.checkpoint_get(jid,'unknown_model_attempt')==frozen
        raise RetryableProviderError('gigachat_attempt_outcome_unknown',retry_at=svc.store.now()+300)
    svc._run_research=recover_only
    for _ in range(2):
        assert await svc.run_once()
        with svc.store.tx() as db:
            db.execute('UPDATE jobs SET available_at=0 WHERE id=?',(jid,))
    story,job,research,selected,assertion=snapshot(svc,sid,jid,run)
    assert story['state']=='facts_ready' and job['state']=='retry' and research['state']=='partial'
    assert svc.store.checkpoint_get(jid,'unknown_model_attempt')==frozen
    assert svc.store.checkpoint_get(jid,'worker_non_wait_failures') is None and not calls


@pytest.mark.asyncio
async def test_terminal_failed_run_without_eligible_partial_value_is_failed(tmp_path):
    svc,sid,jid,run=prepared(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE fact_assertions SET eligibility='withheld' WHERE story_id=?",(sid,))
    async def fail(job):
        raise PermanentProviderError('gigachat:closed_semantic_unit_requires_live')
    svc._run_research=fail
    assert await svc.run_once()
    story,job,research,selected,assertion=snapshot(svc,sid,jid,run)
    assert story['state']=='needs_review' and job['state']=='failed' and research['state']=='failed'
    assert story['draft_text']=='Owner draft' and story['concept']=='Owner concept' and selected==1
