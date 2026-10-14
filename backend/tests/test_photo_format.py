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
    def write(_session, _message):
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


def test_visual_mira_snapshot_uses_chosen_crop_but_identity_keeps_original(tmp_path):
    from street_story.live import StreetStoryLiveAdapter
    from PIL import Image
    import io
    svc = service(tmp_path)
    sid = create_story(svc)['id']
    adapter = StreetStoryLiveAdapter(svc, lambda _session, _message: None, lambda _session, _message: None)
    set_photo_format(svc, sid, 'crop-view', {'mode': 'crop_4_5', 'expected_photo_sha256': PHOTO_SHA,
        'expected_identity_generation': svc.story(sid)['identity_generation']})
    _raw, width, height = adapter._visual_snapshot(sid)
    assert (width, height) == (40, 30)
    raw, width, height = adapter._visual_snapshot(sid, selected_format=True)
    assert (width, height) == (24, 30)
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (24, 30)
    import hashlib
    assert hashlib.sha256(svc._source_photo_bytes(sid)).hexdigest() == PHOTO_SHA


@pytest.mark.asyncio
async def test_saved_visual_review_delivers_actual_result_without_generating(tmp_path):
    from street_story.live import StreetStoryLiveAdapter
    from types import SimpleNamespace
    from PIL import Image
    import io
    import base64
    svc = service(tmp_path)
    sid = create_story(svc)['id']
    stream = io.BytesIO()
    Image.new('RGB', (1200, 800), 'red').save(stream, format='PNG')
    calls = []
    async def read_asset(ref):
        calls.append(ref)
        return stream.getvalue(), 'image/png'
    svc.providers.vibepublish.read_asset = read_asset
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET vibepublish_asset_ref=?,processed_image_url=? WHERE id=?',
            ('owned-result', '/processed/owned-result', sid))
    writes, events = [], []
    adapter = StreetStoryLiveAdapter(svc, lambda _s, e: events.append(e), lambda _s, e: writes.append(e))
    session = SimpleNamespace(id='visual-review', resource_id=sid)
    await adapter._send_saved_visual_snapshot(session)
    assert calls == ['owned-result'] and len(writes) == 1
    assert writes[0]['context'] == {'kind': 'saved_visual', 'story_id': sid, 'asset_ref': 'owned-result'}
    with Image.open(io.BytesIO(base64.b64decode(writes[0]['data']))) as result:
        assert result.size == (768, 512)
        red, green, blue = result.getpixel((100, 100))
        assert red > 245 and green < 10 and blue < 10
    assert events[-1]['type'] == 'visual_review_context'
