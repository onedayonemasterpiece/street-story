import json
from types import SimpleNamespace

import pytest

from street_story.headless_identity import HeadlessIdentity
from street_story.service import canonical
from test_independent_article_priority import gallery
from visual_queue_fixture import reference_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt,skip', [({},True),
    ({'phase':'created','provider_send_state':'not_sent'},True),
    ({'phase':'failed','provider_send_state':'not_sent'},True),
    ({'phase':'submitted','provider_send_state':'possibly_sent'},False),
    ({'phase':'completed','result':{'status':'mismatch'}},False),
    ({'phase':'failed'},False)])
async def test_ready_ineligible_child_skips_only_proven_unsent_or_observes_exact_original(tmp_path, receipt, skip):
    svc, _adapter, story, session = gallery(tmp_path,0)
    worker=HeadlessIdentity(svc)
    session.id='headless:ready-child'
    async def images(batch, limit, *, story_id, evidence):
        candidate=batch[0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',candidate['reference_image_urls'][0])]
    svc._candidate_reference_images=images
    await worker._compare_place_images(session,{})
    state=session.state['visual_comparison']
    worker._freeze_parallel_pairs(session,state['pending'],('google',))
    child=state['parallel_pairs'][0]
    original_id=child['id']
    with svc.store.tx() as db:
        row=db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()
        research=json.loads(row[0])
        research['visual_identity']['candidates'][0]['identity_eligible']=False
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),story['id']))
    calls=[]
    def receipts(snapshot, context):
        assert snapshot['_visual_reference_mapping'][0]['reference_id']==child['reference_id']
        assert json.loads(context)['comparison_id']==original_id
        return {'vision':receipt} if receipt else {}
    async def observe(route, snapshot, item, schema, context):
        calls.append(json.loads(context)['comparison_id'])
        assert item['_visual_pair_resume_only'] is True
        return {'result':{'status':'mismatch','candidate_id':'','confidence':.99,
            'observations':['Distinct facade differs.'],'alternative_candidate_ids':[]},
            'receipt':{'phase':'completed','model':'original-model'}}
    provider=SimpleNamespace(visual_pair_receipts=receipts,visual_pair_route=observe)
    snapshot,research=svc._identity_snapshot(story['id'])
    scope={'generation':0,'photo_sha256':snapshot['photo_sha256'],'control_revision':0}
    await worker._run_parallel_pairs({'id':'ready-job','attempts':1},provider,snapshot,session,scope)
    assert child['phase']==('skipped' if skip else 'completed')
    assert calls==([] if skip else [original_id])
