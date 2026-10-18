import hashlib
import io
from copy import deepcopy

import pytest
from PIL import Image

from street_story.live import StreetStoryLiveAdapter
from street_story.photo_metadata import inspect_capture_time
from test_identity_lifecycle import make_service


def photo(*, original='2026:10:07 12:08:36', offset='+02:00', subsec='144', top_original=None):
    exif = Image.Exif()
    # A generic modification timestamp must never become capture time.
    exif[306] = '2026:10:15 23:59:59'
    exif[34665] = {key: value for key, value in
        ((36867, original), (36881, offset), (37521, subsec)) if value is not None}
    if top_original is not None:
        exif[36867] = top_original
    output = io.BytesIO()
    Image.new('RGB', (16, 12), 'gray').save(output, format='JPEG', exif=exif)
    return output.getvalue()


def test_original_capture_time_and_offset_are_bound_to_actual_source_bytes():
    source = photo()
    result = inspect_capture_time(source)
    assert result == {
        'status': 'known', 'provenance': 'source_exif',
        'source_sha256': hashlib.sha256(source).hexdigest(),
        'datetime_original': '2026:10:07 12:08:36', 'subsec_time_original': '144',
        'local_datetime': '2026-10-07T12:08:36.144', 'offset_time_original': '+02:00',
        'captured_at': '2026-10-07T12:08:36.144+02:00',
        'captured_at_utc': '2026-10-07T10:08:36.144000+00:00',
    }


@pytest.mark.parametrize(('kwargs', 'status', 'reason'), [
    ({'original': None}, 'unknown', 'datetime_original_missing'),
    ({'offset': None}, 'timezone_unknown', 'offset_time_original_missing'),
    ({'offset': '+99:00'}, 'timezone_unknown', 'offset_time_original_invalid'),
    ({'offset': 'Europe/Kaliningrad'}, 'timezone_unknown', 'offset_time_original_invalid'),
    ({'original': '2026:02:31 12:08:36'}, 'unknown', 'original_capture_time_unreadable'),
    ({'subsec': 'not-a-time'}, 'unknown', 'original_capture_time_unreadable'),
    ({'top_original': '2026:10:08 12:08:36'}, 'unknown', 'conflicting_original_time_tags'),
])
def test_missing_invalid_or_conflicting_time_is_never_replaced(kwargs, status, reason):
    result = inspect_capture_time(photo(**kwargs))
    assert (result['status'], result['reason']) == (status, reason)
    assert 'captured_at_utc' not in result
    assert 'captured_at' not in result
    if status == 'timezone_unknown':
        assert result['local_datetime'] == '2026-10-07T12:08:36.144'
        assert 'offset_time_original' not in result


def test_opaque_upload_rest_reopen_and_actual_editor_context_use_source_metadata(tmp_path):
    service, _ = make_service(tmp_path)
    source = photo()
    story = service.create_story(key='capture-source', client_story_id='capture-source',
        photo_sha256='opaque-upload-id', photo_mime_type='image/jpeg', photo_bytes=source,
        voice_protocol='voice-chunks-v2', lat=None, lon=None)
    capture = story['photo_capture']
    assert story['photo_sha256'] == 'opaque-upload-id'
    assert capture['source_sha256'] == hashlib.sha256(source).hexdigest()
    with service.store.connection() as db:
        before = tuple(db.execute('SELECT research_json,revision FROM stories WHERE id=?', (story['id'],)).fetchone())
    reopened, _ = make_service(tmp_path)
    saved = reopened.story(story['id'])
    state = {'story': saved, 'editor': {}}
    original = deepcopy(state)
    compact = StreetStoryLiveAdapter._compact_context(state)
    for capability in ('editor', 'dictation', 'publication', 'photo_format'):
        assert StreetStoryLiveAdapter._capability_context(compact, capability)['photo_capture'] == capture
    assert state == original
    with service.store.connection() as db:
        after = tuple(db.execute('SELECT research_json,revision FROM stories WHERE id=?', (story['id'],)).fetchone())
        assert db.execute('SELECT count(*) FROM jobs WHERE story_id=?', (story['id'],)).fetchone()[0] == 0
    assert before == after  # Projection neither migrates research nor starts it.

    # A re-encoded upload with absent EXIF must not borrow the earlier date.
    no_capture = photo(original=None)
    restored = reopened.create_story(key='capture-source', client_story_id='capture-source',
        photo_sha256='opaque-upload-id', photo_mime_type='image/jpeg', photo_bytes=no_capture,
        voice_protocol='voice-chunks-v2', lat=None, lon=None)
    assert restored['photo_capture']['status'] == 'unknown'
    assert restored['photo_capture']['source_sha256'] == hashlib.sha256(no_capture).hexdigest()
    reopened._temporary_photos.discard(story['id'])
    (tmp_path / 'stories' / story['id'] / 'source.original').unlink()
    unavailable = reopened.story(story['id'])
    assert unavailable['photo_capture'] == {
        'status': 'unknown', 'provenance': 'source_exif', 'reason': 'source_unavailable',
    }
    assert unavailable['source_available'] is False


def test_legacy_compact_state_does_not_infer_capture_time_from_current_date():
    compact = StreetStoryLiveAdapter._compact_context({'story': {'id': 'legacy'}, 'editor': {}})
    assert compact['photo_capture'] == {'status': 'unknown', 'reason': 'capture_metadata_unavailable'}
