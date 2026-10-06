"""Ready references and completed negatives preserve current scope and evidence."""
import json
from types import SimpleNamespace

import pytest

from street_story.headless_identity import HeadlessIdentity
from street_story.poi_memory import candidate_reference_images, ensure_poi_identity
from street_story.service import canonical
from test_visual_search_continuation import prepared


@pytest.mark.asyncio
async def test_new_source_uses_accepted_poi_reference_before_new_search(tmp_path, monkeypatch):
    from test_identity_lifecycle import create, make_service
    from street_story import identity_discovery
    svc, gemini = make_service(tmp_path)
    previous = create(svc, client='previous-reference')
    accepted = {'status':'match', 'candidate_id':'wiki:77', 'candidate_name':'Тестовые ворота',
        'visual_reference_verified':True, 'reference_evidence':[{
            'candidate_id':'web:accepted', 'subject_candidate_id':'wiki:77',
            'article_url':'https://archive.example/gate', 'image_url':'https://archive.example/gate.jpg',
            'figcaption':'Illustrated gate'}]}
    with svc.store.tx() as db:
        ensure_poi_identity(db, accepted, now=1)
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
            (canonical({'visual_identity':accepted}), previous['id']))
    current = create(svc, client='new-source')
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET photo_sha256=? WHERE id=?',
            ('new-source-upload-token', current['id']))
    async def deferred(*args, **kwargs):
        return {'status':'uncertain', 'candidate_id':'', 'confidence':0,
                'observations':[], '_comparison_deferred':True}
    async def forbidden_search(*args, **kwargs):
        raise AssertionError('A saved reference must enter comparison before a new search')
    svc._identify_photo = deferred
    monkeypatch.setattr(identity_discovery, 'recover', forbidden_search)
    svc.ensure_identity(current['id'])
    assert await svc.run_once()
    result = svc.story(current['id'])
    assert result['visual_identity']['status'] == 'uncertain'
    assert result['place_name'] is None
    assert not result['visual_identity']['visual_reference_verified']


def test_new_semantic_contract_requires_observable_paired_geometry():
    from street_story.identity_lifecycle import visual_match
    candidate = {'candidate_id':'wiki:77','name':'Gate'}
    raw = {'status':'match','candidate_id':'wiki:77','confidence':.99,
           'observations':['The article documents this building.'],
           'alternative_candidate_ids':[], '_references_sent':['wiki:77'],
           '_observable_geometry_required':True,'shared_distinctive_geometry':False,
           'observable_correspondences':[]}
    assert not visual_match(raw,[candidate])
    raw.update(shared_distinctive_geometry=True)
    assert not visual_match(raw,[candidate])
    raw['observable_correspondences'] = [{'source_detail':'Two octagonal towers and central pointed arch',
                                          'reference_detail':'Same towers, battlements and central pointed arch'}]
    assert visual_match(raw,[candidate])


def test_accepted_reference_lookup_is_indexed_and_never_name_joined(tmp_path):
    svc, _, story, _ = prepared(tmp_path)
    identity = {'status': 'match', 'candidate_id': 'wiki:77', 'candidate_name': 'Gate',
        'visual_reference_verified': True, 'reference_evidence': [{
            'candidate_id': 'web:article', 'subject_candidate_id': 'wiki:77',
            'article_url': 'https://archive.example/gate', 'image_url': 'https://archive.example/gate.jpg',
            'figcaption': 'Illustrated gate'}]}
    with svc.store.tx() as db:
        ensure_poi_identity(db, identity, now=1)
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
            (canonical({'visual_identity': identity}), story['id']))
        refs = candidate_reference_images(db, [{'candidate_id': 'wiki:77'}])
        assert refs[0]['reference_reuse']['story_id'] == story['id']
        assert refs[0]['reference_image_urls'] == ['https://archive.example/gate.jpg']
        assert refs[0]['article_media'][0]['figcaption'] == 'Illustrated gate'
        assert not any('sha' in key for key in refs[0]['article_media'][0])
        assert not candidate_reference_images(db, [{'candidate_id': 'wiki:999', 'name': 'Gate'}])
        assert not candidate_reference_images(db, [{'candidate_id': 'wiki:77', 'identity_eligible': False}])
        plan = list(db.execute("EXPLAIN QUERY PLAN SELECT id FROM stories WHERE "
            "json_extract(research_json,'$.visual_identity.status')='match' AND "
            "json_extract(research_json,'$.visual_identity.visual_reference_verified')=1 AND "
            "json_extract(research_json,'$.visual_identity.candidate_id')='wiki:77'"))
        assert any('idx_stories_accepted_reference_candidate' in row[3] for row in plan)


@pytest.mark.asyncio
async def test_bounded_negative_continuation_yields_and_stops_immediately_on_match(tmp_path):
    svc, _, story, _ = prepared(tmp_path)
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture')
    calls = []
    class Worker(HeadlessIdentity):
        async def _run_owned_unit(self, job, provider, row, session, scope):
            calls.append(scope.copy())
            session.state['visual_comparison'] = {'queue': [1]}
            if len(calls) == 2:
                with svc.store.tx() as db:
                    db.execute('UPDATE stories SET research_json=? WHERE id=?',
                        (canonical({'visual_identity': {'status': 'match'}}), story['id']))
                return True
            return False
    await Worker(svc).run({'id':'fixture-job','story_id':story['id'],'attempts':1,
                          'payload_json':json.dumps({'identity_generation':0})})
    assert len(calls) == 2
    await Worker(svc).run({'id':'fixture-job','story_id':story['id'],'attempts':2,
                          'payload_json':json.dumps({'identity_generation':0})})
    assert len(calls) == 2  # No send after accepted match.


@pytest.mark.asyncio
async def test_stop_between_completed_units_prevents_next_send(tmp_path):
    svc, _, story, _ = prepared(tmp_path)
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture')
    calls = []
    class Worker(HeadlessIdentity):
        async def _run_owned_unit(self, job, provider, row, session, scope):
            calls.append(1)
            session.state['visual_comparison'] = {'queue':[1]}
            with svc.store.tx() as db:
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
                    'research_controls': {'identity': {'photo_sha256':row['photo_sha256'],
                        'identity_generation':0,'revision':1,'stopped':True}}}), row['id']))
            return False
    await Worker(svc).run({'id':'fixture-job','story_id':story['id'],'attempts':1,
                          'payload_json':json.dumps({'identity_generation':0})})
    assert calls == [1]


@pytest.mark.asyncio
async def test_far_saved_positive_cannot_preempt_ready_nearby_candidate(tmp_path, monkeypatch):
    from street_story import article_media
    svc, adapter, current, session = prepared(tmp_path)
    previous = svc.create_story(key='far-old-source', client_story_id='far-old-source',
        photo_sha256='old-upload', photo_mime_type='image/jpeg', photo_bytes=b'old source',
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    near = {'candidate_id': 'wiki:near', 'name': 'Near object', 'distance_m': 5.3,
            'url': 'https://en.wikipedia.org/wiki/Near',
            'reference_image_urls': ['https://images.example/near.jpg']}
    far = {'candidate_id': 'wiki:far', 'name': 'Far object', 'distance_m': 481.3,
           'url': 'https://en.wikipedia.org/wiki/Far',
           'reference_image_urls': ['https://images.example/far.jpg']}
    accepted = {'status': 'match', 'candidate_id': 'wiki:far', 'candidate_name': 'Far object',
        'visual_reference_verified': True, 'reference_evidence': [{
            'candidate_id': 'web:old-far', 'subject_candidate_id': 'wiki:far',
            'article_url': 'https://archive.example/far', 'image_url': 'https://archive.example/far.jpg'}]}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
            (canonical({'visual_identity': accepted}), previous['id']))
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
            (canonical({'visual_identity': {'status': 'uncertain', 'candidates': [near, far]}}), current['id']))
    async def no_delay(*args, **kwargs):
        raise AssertionError('Do not delay a ready nearby reference behind a distant article')
    monkeypatch.setattr(article_media, 'article_candidates', no_delay)
    owner = session()
    comparison = await adapter._compare_place_images(owner, {})
    assert comparison['references'][0]['candidate_id'] == 'wiki:near'
    assert comparison['physical_candidates'][0]['distance_m'] == 5.3
    assert comparison['physical_candidates'][1]['distance_m'] == 481.3
    assert any(item.get('reference_reuse', {}).get('subject_candidate_id') == 'wiki:far'
               for item in owner.state['visual_comparison']['queue'])
    assert svc.story(current['id'])['visual_identity']['status'] == 'uncertain'
