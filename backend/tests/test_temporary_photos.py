import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.service import ConflictError, StreetStoryService
from street_story.temporary_photos import TemporaryPhotos
from test_backend import service as make_service


def test_expiry_and_capacity_release_raw_bytes():
    now = [0.0]
    photos = TemporaryPhotos(capacity_bytes=8, idle_seconds=10, clock=lambda: now[0])
    photos.put('a', b'aaaa')
    photos.put('b', b'bbbb')
    photos.put('c', b'cccc')
    assert photos.get('a') is None
    assert photos.get('b') == b'bbbb'
    now[0] = 11
    assert photos.get('b') is None and photos.get('c') is None


def test_normal_reupload_restores_ram_preserving_story(tmp_path):
    service, *_ = make_service(tmp_path)
    params = dict(key='upload-once', client_story_id='gallery-selection',
                  photo_sha256='opaque-upload-token', photo_mime_type='image/jpeg',
                  photo_bytes=b'original-raw-image', voice_protocol='voice-chunks-v2', lat=None, lon=None)
    story = service.create_story(**params)
    with service.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='retained draft',revision=7 WHERE id=?", (story['id'],))
        assert db.execute('SELECT photo_path FROM stories WHERE id=?', (story['id'],)).fetchone()[0] == ''
    restarted = StreetStoryService(service.settings, service.providers)
    with pytest.raises(ConflictError, match='Временное фото'):
        restarted._source_photo_bytes(story['id'])
    assert restarted.story(story['id'])['source_available'] is False
    restored = restarted.create_story(**params)
    assert restored['id'] == story['id'] and restored['draft_text'] == 'retained draft'
    assert restored['revision'] == 7 and restored['source_available'] is True
    assert restarted._source_photo_bytes(story['id']) == params['photo_bytes']
    assert not (tmp_path / 'stories').exists()


@pytest.mark.asyncio
async def test_http_photo_above_default_spool_limit_stays_in_ram(tmp_path, monkeypatch):
    import starlette.formparsers as forms
    original = forms.SpooledTemporaryFile
    handles = []
    def capture(*args, **kwargs):
        handle = original(*args, **kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr(forms, 'SpooledTemporaryFile', capture)
    service, *_ = make_service(tmp_path)
    app = create_app(service.settings, service)
    data = b'raw-photo' * (256 * 1024)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/stories', headers={
            'Authorization': 'Bearer ' + reveal(service.settings.device_token), 'Idempotency-Key': 'raw-photo'},
            data={'client_story_id': 'raw-photo', 'photo_sha256': 'opaque-token', 'voice_protocol': 'voice-chunks-v2'},
            files={'photo': ('source.jpg', data, 'image/jpeg')})
    assert response.status_code == 200, response.text
    assert handles and all(not handle._rolled for handle in handles)
    assert service._source_photo_bytes(response.json()['id']) == data
