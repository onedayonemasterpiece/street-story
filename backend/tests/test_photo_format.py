import pytest
from street_story.photo_format import set_photo_format
from street_story.service import ConflictError
from test_mvp_visual import service, create_story as create_mvp_story, PHOTO_SHA


def create_story(svc):
    return create_mvp_story(svc, portrait=False)


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


@pytest.mark.asyncio
async def test_opaque_upload_token_never_blocks_format_snapshot_or_visual(tmp_path):
    # Android sends an opaque upload token as photo_sha256, never the image checksum.
    import hashlib
    import json
    from street_story.live import StreetStoryLiveAdapter
    from test_mvp_visual import FrameBoundary, PHOTO
    token = hashlib.sha256(b'opaque-upload-token').hexdigest()
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    story = svc.create_story(key='create-opaque', client_story_id='story-opaque', photo_sha256=token,
        photo_mime_type='image/jpeg', photo_bytes=PHOTO, voice_protocol='voice-chunks-v2', lat=54.7104, lon=20.4522)
    sid = story['id']
    chosen = set_photo_format(svc, sid, 'opaque-crop', {'mode': 'crop_4_5', 'expected_photo_sha256': token,
        'expected_identity_generation': story['identity_generation']})
    assert chosen['visual_photo_format']['crop_rect'] == [8, 0, 24, 30]
    adapter = StreetStoryLiveAdapter(svc, lambda _session, _message: None, lambda _session, _message: None)
    assert adapter._visual_snapshot(sid, selected_format=True)[1:] == (24, 30)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (sid,))
    svc.mutate_visual(sid, 'opaque-visual', {'selected_fact_ids': []})
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
        context = json.loads(svc._story_row(db, sid)['visual_context_json'])
    assert context['source_photo_sha256'] == token
    assert context['source_image_transform']['original_sha256'] == PHOTO_SHA
    await svc._run_visual(job)
    assert vp.tune_calls == 1


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


def test_mira_context_exposes_actual_source_frame_for_semantic_layout_editing():
    from street_story.live import StreetStoryLiveAdapter
    frame = {'copy': {'title': 'Court', 'body': 'Source2022'}, 'layout': {'text_box': [.1, .1, .8, .3]}}
    projected = StreetStoryLiveAdapter._compact_context({'story': {'facts': [], 'visual': {'source_frame': frame, 'output_frame_evidence': {'width': 1080, 'height': 1350}}}, 'editor': {}})
    assert projected['visual']['source_frame'] == frame
    assert projected['visual']['render_canvas_size'] == [1080, 1350]


@pytest.mark.asyncio
async def test_photo_choice_never_infers_source_frame_from_model_copy(tmp_path):
    from street_story.live import StreetStoryLiveAdapter
    from test_live_editor import mark_identity_ready
    svc = service(tmp_path)
    sid = create_story(svc)['id']
    mark_identity_ready(svc, sid)
    set_photo_format(svc, sid, 'owner-original', {'mode': 'original', 'expected_photo_sha256': PHOTO_SHA,
        'expected_identity_generation': svc.story(sid)['identity_generation']})
    captured = []
    async def request(_sid, _key, body):
        captured.append(body)
        return svc.story(_sid)
    svc.request_visual = request
    adapter = StreetStoryLiveAdapter(svc, lambda _session, _message: None, lambda _session, _message: None)
    args = {'fact_ids': [], 'visual_copy': {'title': 'Original photo'},
        'visual_layout': {'text_box': [.1, .01, .8, .1]}}
    await adapter._generate_visual(sid, 'copy-complete', args)
    assert 'preserve_source_frame' not in captured[-1]
    assert captured[-1]['visual_copy'] == args['visual_copy']
    await adapter._generate_visual(sid, 'explicit-other-mode', {**args, 'preserve_source_frame': False})
    assert captured[-1]['preserve_source_frame'] is False
    await adapter._generate_visual(sid, 'missing-semantic-layout', {'fact_ids': []})
    assert 'visual_copy' not in captured[-1] and 'preserve_source_frame' not in captured[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,size', [('original', (40, 30)), ('crop_4_5', (24, 30))])
async def test_artistic_tune_uses_actual_owner_frame_without_source_composition(tmp_path, mode, size):
    import io
    from PIL import Image
    from test_mvp_visual import FrameBoundary, visual_context
    class ArtisticBoundary(FrameBoundary):
        async def ingress_asset(self, data, mime_type, request_key):
            with Image.open(io.BytesIO(data)) as image:
                assert image.size == size
            return await super().ingress_asset(data, mime_type, request_key)
        async def visual(self, payload, request_key):
            command = payload['command']
            if command['kind'] == 'select':
                return await super().visual(payload, request_key)
            assert command['kind'] == 'tune' and command['copy'] == {}
            assert command['formats'] == ['original' if mode == 'original' else 'post_4_5']
            assert 'handwritten' in command['brief'] and 'single tasteful accent' in command['brief']
            assert 'Never add floors, arches' in command['brief']
            return {'operation_id': 'frame-op', 'state': 'accepted', 'visual_job_id': 'frame-job'}
    vp = ArtisticBoundary({'width': size[0] * 2, 'height': size[1] * 2})
    svc = service(tmp_path, vp)
    sid = create_story(svc)['id']
    set_photo_format(svc, sid, 'owner-format', {'mode': mode, 'expected_photo_sha256': PHOTO_SHA,
        'expected_identity_generation': svc.story(sid)['identity_generation']})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review',draft_text='Keep owner draft' WHERE id=?", (sid,))
    svc.mutate_visual(sid, 'owner-artistic-request', {'selected_fact_ids': [], 'visual_instruction': 'Ink sketch, one amber accent.'})
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    await svc._run_visual(job)
    ctx = visual_context(svc, sid)
    assert 'source_frame' not in ctx
    assert ctx['output_frame_evidence']['state'] == 'verified'
    assert svc.story(sid)['draft_text'] == 'Keep owner draft'
    assert ctx['source_image_transform']['output_size'] == list(size)
    assert ctx['source_photo_sha256'] == PHOTO_SHA


def test_artistic_original_rejects_wrong_aspect_instead_of_stretching():
    from street_story.product import ProductStreetStoryService, ORIGINAL_ART_FRAME_CONTRACT
    ctx = {'output_frame_contract': ORIGINAL_ART_FRAME_CONTRACT,
        'source_image_transform': {'output_size': [3000, 4000]}}
    assert ProductStreetStoryService._frame_dimensions_match(ctx, 768, 1024)
    assert not ProductStreetStoryService._frame_dimensions_match(ctx, 1024, 1350)
