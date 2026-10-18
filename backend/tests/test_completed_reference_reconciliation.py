"""Addressed pending observations survive restart; old SOURCE verdicts stay historical."""
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.headless_identity import HeadlessIdentity
from street_story.live import ensure_live_schema
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical, ConflictError
from street_story.visual_attachments import direct_visual_parts
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


@pytest.fixture
def prepared(tmp_path):
    service, _ = make_service(tmp_path)
    raw = jpeg((800, 600))
    story = service.create_story(key='direct-stage', client_story_id='direct-stage',
        photo_sha256='opaque-upload-token', photo_mime_type='image/jpeg', photo_bytes=raw,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    ensure_live_schema(service)
    candidate = {'candidate_id':'wiki:77', 'name':'Gate', 'url':'https://archive.example/gate',
        'reference_image_urls':['https://archive.example/gate.jpg']}
    research = {'identity_generation':0,'visual_identity':{'status':'uncertain','candidates':[candidate]}}
    with service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),story['id']))
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client, adapter.native_vision = service, None, None
    calls = []
    async def compare(snapshot, supplied_story, schema, context):
        supplied = json.loads(context)
        parts = direct_visual_parts(supplied_story, supplied)
        assert snapshot is None and parts[0]['bytes'] == raw
        assert parts[1]['url'] == candidate['reference_image_urls'][0]
        assert supplied_story['_visual_reference_mapping'] == supplied['references']
        calls.append(supplied)
        negative = {'status':'mismatch','candidate_id':'','confidence':1,
                    'observations':['Different openings.'],'alternative_candidate_ids':[]}
        if len(parts) > 2:
            negative['reference_verdicts'] = [{**negative, 'reference_id':ref['reference_id']}
                                            for ref in supplied['references']]
        return {'result':negative,'receipt':{'model':'fake-direct-boundary'}}
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=compare)
    service.providers.research = adapter
    worker = HeadlessIdentity(service)
    session = SimpleNamespace(id='headless:stage:1', resource_id=story['id'], state={}, model='fixture')
    with service.store.tx() as db:
        jid = service._enqueue_job(db,story['id'],'identity_visual','direct-stage-job',{'identity_generation':0})
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?",(jid,))
    job = {'id':jid,'story_id':story['id'],'attempts':1}
    scope = {'photo_sha256':story['photo_sha256'],'generation':0,'control_revision':0}
    return SimpleNamespace(service=service, story=story, worker=worker, session=session,
                          job=job, scope=scope, calls=calls, candidate=candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize('reference_count',[1,2])
async def test_owned_unit_dispatches_exact_mapping_through_real_adapter(prepared,reference_count):
    fixture = prepared
    if reference_count == 2:
        row,research = fixture.service._identity_snapshot(fixture.story['id'])
        research['visual_identity']['candidates'][0]['reference_image_urls'].append('https://archive.example/gate-back.jpg')
        with fixture.service.store.tx() as db:
            db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),row['id']))
        fixture.session.visual_reference_limit = 2
    assert not await fixture.worker._run_owned_unit(fixture.job,fixture.service.providers.research,
        fixture.story,fixture.session,fixture.scope)
    assert len(fixture.calls) == 1
    latest = fixture.service.story(fixture.story['id'])
    assert latest['visual_identity']['status'] == 'uncertain'
    assert latest['identity_progress']['images_reviewed_count'] == reference_count
    _, research = fixture.service._identity_snapshot(fixture.story['id'])
    state = research['visual_search_operation']
    assert len(state['reviewed_reference_ids']) == reference_count
    assert 'pending_descriptor' not in state
    assert not any(fixture.service.settings.data_dir.rglob('*.jpg'))


@pytest.mark.asyncio
async def test_prior_completed_match_does_not_authorize_new_source_verdict(prepared):
    fixture = prepared
    receipt = {'phase':'completed','result':{'status':'match','candidate_id':'wiki:77'},
        'binding':{'story_id':fixture.story['id'],'visual_scope':True,'generation':0,'control_revision':0}}
    with fixture.service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('old-attempt','prior-comparison',fixture.story['id'],'vision_native',canonical(receipt),1,1))
    await fixture.worker._run_owned_unit(fixture.job,fixture.service.providers.research,
        fixture.story,fixture.session,fixture.scope)
    assert len(fixture.calls) == 1
    assert fixture.service.story(fixture.story['id'])['visual_identity']['status'] == 'uncertain'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['unknown','submitted','prompt_intent'])
async def test_unknown_new_visual_scope_without_photo_digest_blocks_new_operation(prepared,phase):
    fixture = prepared
    receipt = {'phase':phase,'binding':{'story_id':fixture.story['id'],'visual_scope':True,
                                      'generation':0,'control_revision':0}}
    with fixture.service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('unknown-attempt','unknown-comparison',fixture.story['id'],'vision_google_pair',canonical(receipt),1,1))
    with pytest.raises(RetryableProviderError,match='research_visual_outcome_unknown'):
        await fixture.worker._run_owned_unit(fixture.job,fixture.service.providers.research,
            fixture.story,fixture.session,fixture.scope)
    assert not fixture.calls
    assert fixture.service.story(fixture.story['id'])['visual_identity']['status'] == 'uncertain'


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['stop','stop_resume','generation','lease'])
async def test_late_verdict_cannot_commit_superseded_control_or_lease(prepared,change):
    fixture = prepared
    original = fixture.service._candidate_reference_images
    async def changed(*args,**kwargs):
        images = await original(*args,**kwargs)
        row,research = fixture.service._identity_snapshot(fixture.story['id'])
        if change in {'stop','stop_resume'}:
            research['research_controls'] = {'identity':{'revision':1 if change=='stop' else 2,
                'stopped':change=='stop','photo_sha256':row['photo_sha256'],'identity_generation':0}}
        elif change == 'generation':
            research['identity_generation'] = 1
        else:
            research['visual_search_operation']['lease_owner'] = 'other-worker'
        with fixture.service.store.tx() as db:
            db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),row['id']))
        return images
    fixture.service._candidate_reference_images = changed
    try:
        await fixture.worker._run_owned_unit(fixture.job,fixture.service.providers.research,
            fixture.story,fixture.session,fixture.scope)
    except ConflictError:
        assert change == 'lease'
    assert not fixture.calls
    assert fixture.service.story(fixture.story['id'])['visual_identity']['status'] == 'uncertain'
