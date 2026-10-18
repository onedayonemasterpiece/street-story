"""Durable Stop fences visual saves and verdicts, even after Resume."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from visual_queue_fixture import prepare_live_parts, queued_reference_count, reference_receipt

from street_story.headless_identity import HeadlessIdentity
from street_story.live import StreetStoryLiveAdapter
from street_story.research_control import resume_research, stop_research
from street_story.service import ConflictError
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


def setup(tmp_path):
    svc, _ = make_service(tmp_path)
    photo = jpeg()
    story = svc.create_story(key='visual-control', client_story_id='visual-control',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    candidate = {'candidate_id': 'wiki:1', 'name': 'Gate', 'url': 'https://example.com/gate',
                 'reference_image_urls': ['https://upload.wikimedia.org/one.jpg', 'https://upload.wikimedia.org/two.jpg']}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps({
            'identity_generation': 0, 'visual_identity': {'status': 'uncertain', 'candidates': [candidate]}}), story['id']))
    async def images(candidates, limit, *, story_id, evidence):
        item = candidates[0]
        evidence.append(reference_receipt(item))
        return [(item['candidate_id'], 'image/jpeg', item['reference_image_urls'][0])]
    svc._candidate_reference_images = images
    adapter = StreetStoryLiveAdapter(svc, lambda *args: None, lambda *args: None)
    prepare_live_parts(adapter, photo)
    session = SimpleNamespace(id='visual-control-session', resource_id=story['id'], model='test-vision', state={})
    return svc, adapter, session, story


def state(svc, sid):
    return svc._identity_snapshot(sid)[1]


def verdict(comparison_id, status='match'):
    return {'comparison_id': comparison_id, 'status': status, 'candidate_id': 'wiki:1', 'confidence': .98,
            'observations': ['Distinct arch and doorway correspondence.'], 'alternative_candidate_ids': []}


@pytest.mark.asyncio
async def test_low_confidence_match_records_uncertain_and_continues_same_queue(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    reply = await adapter._compare_place_images(session, {})
    result = adapter._record_place_comparison(session, 'low-confidence',
        {**verdict(reply['comparison_id']), 'confidence': .78})
    assert not result['matched'] and result['continue_comparison']
    research = state(svc, story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert not research.get('poi_id')
    queue = research['visual_search_operation']
    assert queue.get('pending') is None and queue['lease_owner'] is None
    assert len(queue['queue']) == 1 and len(queue['reviewed_reference_ids']) == 1
    assert queue['verdict_history'][-1]['status'] == 'uncertain'
    assert queue['verdict_history'][-1]['model_status'] == 'match'
    assert queue['verdict_history'][-1]['confidence'] == .78
    assert research['identity_progress']['images_reviewed_count'] == 1
    # The worker follows its ordinary retry path, rather than a terminal
    # product ConflictError, while the acceptance threshold remains .90.


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('status', ['match', 'mismatch'])
async def test_late_verdict_cannot_record_seen_or_clear_retained_queue(tmp_path, resume, status):
    svc, adapter, session, story = setup(tmp_path)
    reply = await adapter._compare_place_images(session, {})
    stop_research(svc, story['id'], purpose='identity')
    if resume:
        resume_research(svc, story['id'], purpose='identity')
    before = state(svc, story['id'])
    with pytest.raises(ConflictError, match='изменилось'):
        adapter._record_place_comparison(session, 'late-verdict', verdict(reply['comparison_id'], status))
    after = state(svc, story['id'])
    assert after == before
    assert queued_reference_count(after['visual_search_operation']) == 2
    assert not after['visual_search_operation']['reviewed_reference_ids']
    assert after['visual_identity']['status'] == 'uncertain'
    assert not after.get('poi_id')
    with svc.store.connection() as db:
        assert not db.execute("SELECT 1 FROM live_commands WHERE command_id='late-verdict'").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
async def test_late_download_cannot_overwrite_queue_after_stop(tmp_path, resume):
    svc, adapter, session, story = setup(tmp_path)
    load = svc._candidate_reference_images
    saved = []
    async def images(*args, **kwargs):
        result = await load(*args, **kwargs)
        stop_research(svc, story['id'], purpose='identity')
        if resume:
            resume_research(svc, story['id'], purpose='identity')
        saved.append(state(svc, story['id']))
        return result
    svc._candidate_reference_images = images
    with pytest.raises(ConflictError, match='изменилось'):
        await adapter._compare_place_images(session, {})
    assert state(svc, story['id']) == saved[0]
    assert queued_reference_count(saved[0]['visual_search_operation']) == 2
    assert not saved[0]['visual_search_operation']['reviewed_reference_ids']


@pytest.mark.asyncio
async def test_stop_resume_between_snapshot_and_lease_does_not_capture_new_authority(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    acquire = adapter._visual_lease
    saved = []
    def lease(*args, **kwargs):
        stop_research(svc, story['id'], purpose='identity')
        resume_research(svc, story['id'], purpose='identity')
        saved.append(state(svc, story['id']))
        return acquire(*args, **kwargs)
    adapter._visual_lease = lease
    with pytest.raises(ConflictError, match='изменилось'):
        await adapter._compare_place_images(session, {})
    assert state(svc, story['id']) == saved[0]
    assert not session.state.get('visual_comparison')


@pytest.mark.asyncio
async def test_persisted_visual_control_revision_also_fences_late_verdict(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    reply = await adapter._compare_place_images(session, {})
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, story['id'])['research_json'])
        research['visual_search_operation']['control_revision'] += 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    before = state(svc, story['id'])
    with pytest.raises(ConflictError, match='изменилось'):
        adapter._record_place_comparison(session, 'superseded-operation', verdict(reply['comparison_id']))
    assert state(svc, story['id']) == before


@pytest.mark.asyncio
async def test_resume_reoffers_pending_reference_with_same_operation_id(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    old = await adapter._compare_place_images(session, {})
    stop_research(svc, story['id'], purpose='identity')
    resume_research(svc, story['id'], purpose='identity')
    with pytest.raises(ConflictError):
        adapter._record_place_comparison(session, 'old-same-session', verdict(old['comparison_id']))
    fresh = await adapter._compare_place_images(session, {})
    assert fresh['comparison_id'] == old['comparison_id']
    assert fresh['references'] == old['references']
    result = adapter._record_place_comparison(session, 'fresh', verdict(fresh['comparison_id']))
    assert result['matched']
    assert state(svc, story['id'])['identity_progress']['images_reviewed_count'] == 1


@pytest.mark.asyncio
async def test_facts_only_stop_does_not_cancel_visual_verdict(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    reply = await adapter._compare_place_images(session, {})
    stop_research(svc, story['id'], purpose='facts')
    assert adapter._record_place_comparison(session, 'visual', verdict(reply['comparison_id']))['matched']


@pytest.mark.asyncio
async def test_old_photo_generation_stop_does_not_block_new_visual_operation(tmp_path):
    svc, adapter, session, story = setup(tmp_path)
    stop_research(svc, story['id'], purpose='identity')
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, story['id'])['research_json'])
        research['identity_generation'] = 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    reply = await adapter._compare_place_images(session, {})
    assert adapter._record_place_comparison(session, 'new-generation', verdict(reply['comparison_id']))['matched']


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
async def test_headless_late_provider_result_returns_without_commit(tmp_path, resume):
    svc, _, _, story = setup(tmp_path)
    calls = []
    async def visual_verdict(snapshot, story_context, schema, context):
        calls.append(snapshot)
        stop_research(svc, story['id'], purpose='identity')
        if resume:
            resume_research(svc, story['id'], purpose='identity')
        return {'result': {key: value for key, value in verdict('unused').items() if key != 'comparison_id'},
                'receipt': {'model': 'test-vision'}}
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='test-vision', visual_verdict=visual_verdict)
    await HeadlessIdentity(svc).run({'id': 'visual-job', 'story_id': story['id'], 'attempts': 1,
                                   'payload_json': json.dumps({'identity_generation': 0})})
    assert len(calls) == 1
    research = state(svc, story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert not research['visual_search_operation']['reviewed_reference_ids']
    assert queued_reference_count(research['visual_search_operation']) == 2
    assert not research.get('poi_id')
