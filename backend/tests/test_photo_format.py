import pytest
from street_story.photo_format import set_photo_format
from street_story.service import ConflictError
from test_mvp_visual import service, create_story, PHOTO_SHA


def test_owner_format_persists_replays_without_job_and_old_visual_cannot_be_current(tmp_path):
    svc = service(tmp_path)
    sid = create_story(svc)['id']
    with svc.store.connection() as db:
        before_jobs = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    payload = {'mode': 'crop_4_5', 'expected_photo_sha256': PHOTO_SHA,
        'expected_identity_generation': svc.story(sid)['identity_generation']}
    cropped = set_photo_format(svc, sid, 'crop-intent', payload)
    assert cropped['visual_photo_format']['mode'] == 'crop_4_5'
    assert cropped['visual_photo_format']['crop_rect'] == [8, 0, 24, 30]
    assert not cropped['visual_photo_format']['current_visual']
    replay = set_photo_format(svc, sid, 'crop-intent', payload)
    assert replay['visual_photo_format']['revision'] == 1
    original = set_photo_format(svc, sid, 'original-intent', {**payload, 'mode': 'original'})
    assert original['visual_photo_format']['mode'] == 'original'
    assert original['visual_photo_format']['revision'] == 2
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == before_jobs
        row = svc._story_row(db, sid)
        assert not svc._photo_format_current(row, {'photo_format_revision': 1,
            'source_image_transform': {'mode': 'crop_4_5', 'crop_rect': [8, 0, 24, 30]}})
    with pytest.raises(ConflictError):
        set_photo_format(svc, sid, 'stale-photo', {**payload, 'expected_photo_sha256': 'old'})
    assert svc.story(sid)['visual_photo_format'] == original['visual_photo_format']


@pytest.mark.asyncio
async def test_live_format_choice_uses_same_story_and_survives_reopen_without_provider_send(tmp_path):
    from street_story.live import StreetStoryLiveAdapter
    from types import SimpleNamespace
    svc = service(tmp_path)
    sid = create_story(svc)['id']
    async def write(_session, _message):
        return None
    adapter = StreetStoryLiveAdapter(svc, lambda _session, _message: None, write)
    initial = adapter.initialize(resource_id=sid, actor=None, model='controlled')
    session = SimpleNamespace(id='live-format', resource_id=sid, state=initial['state'], model='controlled', actor=None)
    call = {'name': 'set_photo_format', 'id': 'live-original-choice', 'args': {'mode': 'original'}}
    result = await adapter.execute_tool(session, call)
    assert result['story']['visual_photo_format']['mode'] == 'original'
    assert (await adapter.execute_tool(session, call)) == result
    assert svc.story(session.resource_id)['visual_photo_format']['revision'] == 1
    reopened = adapter.initialize(resource_id=session.resource_id, actor=None, model='controlled')
    assert reopened['context']['visual_photo_format']['mode'] == 'original'


def test_pending_new_format_cannot_label_previous_asset_as_current():
    from street_story.photo_format import projection
    research = {'visual_photo_format': {'mode': 'original', 'revision': 2, 'crop_rect': None}}
    visual = {'photo_format_revision': 2, 'output_frame_evidence': {'state': 'verified', 'asset_ref': 'new-asset'}}
    assert not projection(research, visual, 'old-asset')['current_visual']
    assert projection(research, visual, 'new-asset')['current_visual']


def test_photo_format_stage_does_not_resend_unrelated_discovery_and_conflict_inventory():
    from street_story.live import StreetStoryLiveAdapter
    context = {'photo_sha256': 'source', 'visual_photo_format': {'mode': 'original'},
        'known_fact_inventory': ['repeated source history'] * 1000,
        'fact_conflicts': ['unrelated dispute'] * 1000,
        'facts': [{'fact_id': 'selected', 'selected': True, 'text': 'Chosen qualified claim'},
                  {'fact_id': 'other', 'selected': False, 'text': 'Other material'}]}
    projected = StreetStoryLiveAdapter._capability_context(context, 'photo_format')
    assert 'known_fact_inventory' not in projected and 'fact_conflicts' not in projected
    assert projected['facts'] == context['facts'][:1]
    assert projected['visual_photo_format'] == context['visual_photo_format']
    assert len(context['known_fact_inventory']) == 1000
