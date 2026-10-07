import json

import pytest

from street_story import article_media
from street_story.service import canonical
from test_visual_search_continuation import prepared
from visual_queue_fixture import reference_receipt


def gallery(tmp_path, units):
    svc, adapter, story, sessions = prepared(tmp_path)
    candidates = [{'candidate_id': f'wiki:{i}', 'name': f'Physical {i}', 'distance_m': 40+i,
        'url': f'https://ru.wikipedia.org/wiki/Physical_{i}',
        'reference_image_urls': [f'https://upload.wikimedia.org/ref-{i}-{j}.jpg' for j in range(10)]}
        for i in range(4)]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status':'uncertain','candidates':candidates}}),story['id']))
    session = sessions()
    state = {'generation':0,'photo_sha256':svc._identity_snapshot(story['id'])[0]['photo_sha256'],
        'queue':[entry for candidate in candidates for entry in adapter._image_entries(candidate)],
        'query':'Physical','reviewed_reference_ids':[],'fetch_failures':[], 'searches':{},
        'sources':{'https://news.example/building':{'source':{'url':'https://news.example/building',
            'discovery_provider':'opencode'},'status':'pending','attempts':0}},
        'preferred_units':units,'units_since_acquisition':units}
    session.state['visual_comparison']=state
    return svc, adapter, story, session


@pytest.mark.asyncio
@pytest.mark.parametrize('units', [0,2])
async def test_independent_news_materializes_before_remaining_wiki_gallery_but_ready_initial_ref_is_immediate(tmp_path, monkeypatch, units):
    svc, adapter, story, session = gallery(tmp_path, units)
    pages=[]
    news={'candidate_id':'web:news','name':'Article facade','url':'https://news.example/building',
        'reference_image_urls':['https://news.example/facade.jpg'],'discovery':'web_article_media',
        'identity_eligible':False}
    async def articles(service, snapshot, sources, excluded, *, receipts):
        pages.append(sources[0]['url'])
        receipts.append({'status':'completed'})
        return [news]
    async def images(batch, limit, *, story_id, evidence):
        candidate=batch[0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',candidate['reference_image_urls'][0])]
    monkeypatch.setattr(article_media,'article_candidates',articles)
    svc._candidate_reference_images=images
    reply=await adapter._compare_place_images(session,{})
    if units == 0:
        assert pages == []
        assert reply['references'][0]['candidate_id'] == 'wiki:0'
    else:
        assert pages == ['https://news.example/building']
        assert reply['references'][0]['candidate_id'] == 'web:news'
        assert len(session.state['visual_comparison']['queue']) == 40
        assert not svc.story(story['id'])['visual_identity'].get('visual_reference_verified')


@pytest.mark.asyncio
async def test_pending_original_comparison_blocks_new_acquisition_and_new_ref_dispatch(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 0)
    async def images(batch, limit, *, story_id, evidence):
        candidate=batch[0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',candidate['reference_image_urls'][0])]
    svc._candidate_reference_images=images
    first=await adapter._compare_place_images(session,{})
    state=session.state['visual_comparison']
    state.update(preferred_units=2,units_since_acquisition=2)
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            ('original-attempt','original-logical',story['id'],'vision',canonical({'phase':'unknown',
                'comparison_id':first['comparison_id'],'binding':{'visual_scope':True,'generation':0}}),svc.store.now(),svc.store.now()))
    async def forbidden(*args, **kwargs):
        pytest.fail('Pending original comparison cannot send a new frame or acquire another article')
    svc._candidate_reference_images=forbidden
    monkeypatch.setattr(article_media,'article_candidates',forbidden)
    with svc.store.tx() as db:
        row=db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()
        research=json.loads(row[0])
        for candidate in research['visual_identity']['candidates']:
            candidate['identity_eligible']=False
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),story['id']))
    repeated=await adapter._compare_place_images(session,{})
    assert repeated['comparison_id'] == first['comparison_id']
    assert len(state['queue']) == 39
    with svc.store.connection() as db:
        receipt=json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',('original-attempt',)).fetchone()[0])
    assert receipt['phase'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('saved_queue', [False, True])
async def test_false_eligibility_is_excluded_from_new_seed_existing_queue_and_physical_prompt(tmp_path, monkeypatch, saved_queue):
    svc, adapter, story, session = gallery(tmp_path, 0)
    with svc.store.tx() as db:
        row=db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()
        research=json.loads(row[0])
        research['visual_identity']['candidates'][0]['identity_eligible']=False
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),story['id']))
    if not saved_queue:
        session.state.pop('visual_comparison')
    fetched=[]
    async def images(batch, limit, *, story_id, evidence):
        candidate=batch[0]
        fetched.append(candidate['candidate_id'])
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',candidate['reference_image_urls'][0])]
    svc._candidate_reference_images=images
    reply=await adapter._compare_place_images(session,{})
    assert fetched == ['wiki:1']
    assert reply['references'][0]['candidate_id'] == 'wiki:1'
    assert 'wiki:0' not in {c['candidate_id'] for c in reply['physical_candidates']}


@pytest.mark.asyncio
async def test_explicit_documented_local_subject_retains_reference_without_using_container_as_physical_identity(tmp_path):
    svc, adapter, story, session = gallery(tmp_path, 0)
    with svc.store.tx() as db:
        row=db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()
        research=json.loads(row[0])
        candidates=research['visual_identity']['candidates']
        hosted=candidates[0]
        hosted.update(identity_eligible=False,identity_role='institution_at_physical_subject',
            physical_subject_candidate_id='wiki:1',physical_subject_evidence={
                'proof':'host_reviewed_official_location','relation':'institution_housed_in_physical_object',
                'institution_candidate_id':'wiki:0','physical_subject_candidate_id':'wiki:1',
                'source_url':'https://official.example/location','source_quote':'This venue is housed in physical building 1.',
                'source_sha256':'a'*64})
        db.execute('UPDATE stories SET research_json=? WHERE id=?',(canonical(research),story['id']))
    session.state.pop('visual_comparison')
    async def images(batch, limit, *, story_id, evidence):
        candidate=batch[0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',candidate['reference_image_urls'][0])]
    svc._candidate_reference_images=images
    reply=await adapter._compare_place_images(session,{})
    assert any(c['candidate_id'] == 'wiki:0' for c in session.state['visual_comparison']['queue'])
    physical={c['candidate_id'] for c in reply['physical_candidates']}
    assert 'wiki:0' not in physical and 'wiki:1' in physical
