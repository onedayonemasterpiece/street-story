from __future__ import annotations
import asyncio
import hashlib
import io
import json

from PIL import Image
from PIL.TiffImagePlugin import IFDRational
import pytest

from street_story.identity_lifecycle import distance, visual_match
from street_story.identity_visual import identify_nearest
from street_story.identity_telemetry import client_fields
from street_story.photo_metadata import inspect_gps
from street_story.service import ConflictError
from test_identity_lifecycle import create, make_service


def photo(*, gps=True, redacted=False, shade=80):
    image = Image.new('RGB', (12, 9), (shade, 100, 130))
    exif = Image.Exif()
    exif[274] = 6
    if gps:
        exif[34853] = {1: '\x00' if redacted else 'N', 2: (IFDRational(54), IFDRational(42), IFDRational(6)),
                       3: '\x00' if redacted else 'E', 4: (IFDRational(20), IFDRational(30), IFDRational(9))}
    stream = io.BytesIO()
    image.save(stream, format='JPEG', exif=exif)
    return stream.getvalue()


def create_photo(service, content, client='real-photo'):
    return service.create_story(key='create-' + client, client_story_id=client,
        photo_sha256=hashlib.sha256(content).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=content,
        voice_protocol='voice-chunks-v2', lat=None, lon=None)


def candidate(i):
    return {'candidate_id': f'c{i}', 'name': f'Building {i}', 'distance_m': i * 30,
            'reference_image_urls': [f'https://upload.wikimedia.org/c{i}.jpg']}


def match(cid, refs=True, certainty=.97):
    return {'status': 'match', 'candidate_id': cid, 'confidence': certainty,
            'observations': ['Matching towers and distinctive arch.'], 'alternative_candidate_ids': [],
            '_references_sent': [cid] if refs else []}


def test_gps_parser_distinguishes_redacted_and_valid_metadata():
    good = inspect_gps(photo())
    assert good['status'] == 'gps_present'
    assert good['latitude'] == pytest.approx(54.7016666667)
    assert good['longitude'] == pytest.approx(20.5025)
    assert inspect_gps(photo(gps=False))['status'] == 'gps_missing'
    redacted = inspect_gps(photo(redacted=True))
    assert redacted['status'] == 'gps_redacted_or_invalid'
    assert redacted['latitude'] is None and redacted['longitude'] is None
    assert inspect_gps(b'not-an-image')['status'] == 'gps_unreadable'


@pytest.mark.asyncio
async def test_server_fallback_reads_real_photo_and_reopen_never_researches(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create_photo(service, photo())
    service.ensure_identity(story['id'])
    await service.run_once()
    result = service.story(story['id'])
    assert result['visual_identity']['status'] == 'match'
    assert result['visual_identity']['photo_sha256'] == hashlib.sha256(photo()).hexdigest()
    assert result['visual_identity']['candidate_url'].startswith('https://ru.wikipedia.org/')
    before = len(gemini.identity_calls)
    service.ensure_identity(story['id'])
    await service.resolve_identity(story['id'])
    assert len(gemini.identity_calls) == before
    assert gemini.research_calls == 0


@pytest.mark.asyncio
async def test_recover_same_original_keeps_story_and_rejects_other_image(tmp_path):
    service, _ = make_service(tmp_path)
    story = create_photo(service, photo(redacted=True))
    service.ensure_identity(story['id'])
    await service.run_once()
    assert service.story(story['id'])['error']['code'] == 'identity_location_missing'
    original_hash = hashlib.sha256(photo(redacted=True)).hexdigest()
    with pytest.raises(ConflictError) as wrong:
        service.recover_photo_location(story['id'], original_hash, photo(shade=20))
    assert wrong.value.code == 'photo_recovery_mismatch'
    recovered = service.recover_photo_location(story['id'], original_hash, photo())
    assert recovered['id'] == story['id'] and recovered['state'] == 'identifying'
    await service.run_once()
    ready = service.story(story['id'])
    assert ready['visual_identity']['status'] == 'match'
    assert ready['visual_identity']['generation'] == 1
    assert ready['visual_identity']['photo_sha256'] == original_hash
    # Replaying a lost successful recovery does not create a new generation.
    service.recover_photo_location(story['id'], original_hash, photo())
    with service.store.connection() as db:
        saved = db.execute('SELECT * FROM stories WHERE id=?', (story['id'],)).fetchone()
        assert saved['photo_sha256'] == original_hash
        assert json.loads(saved['research_json'])['identity_generation'] == 1


@pytest.mark.asyncio
async def test_nearest_batch_stops_before_loading_distant_references(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    calls = []
    async def check(_story, _transcript, batch, reference_limit):
        calls.append(([x['candidate_id'] for x in batch], reference_limit))
        return match('c1')
    service._identify_photo_batch = check
    result = await identify_nearest(service, {'id': story['id']}, '', [candidate(i) for i in range(16, 0, -1)])
    assert result['candidate_id'] == 'c1'
    assert calls == [(['c1', 'c2', 'c3', 'c4'], 2)]


@pytest.mark.asyncio
async def test_ambiguous_nearby_does_not_stop_but_total_work_is_bounded(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    calls = []
    async def check(_story, _transcript, batch, reference_limit):
        calls.append(batch)
        return match(batch[0]['candidate_id'], certainty=.55)
    service._identify_photo_batch = check
    result = await identify_nearest(service, {'id': story['id']}, '', [candidate(i) for i in range(1, 500)])
    assert result['status'] == 'uncertain'
    assert [len(batch) for batch in calls] == [4, 6, 6]
    assert not visual_match(match('c1', refs=False), [candidate(1)])
    assert not visual_match(match('c1', certainty=.89), [candidate(1)])
    assert distance({'distance_m': float('nan')}) == float('inf')


@pytest.mark.asyncio
async def test_camera_building_is_not_promoted_to_match_without_visual_proof(tmp_path):
    service, gemini = make_service(tmp_path)
    async def lookup(*_):
        return {'reverse': {'osm_type': 'way', 'osm_id': 1, 'type': 'building',
                'display_name': 'Camera building', 'address': {'house_number': '1'}}, 'nearby': []}
    async def nearby(*_): return []
    async def uncertain(*_): return {'status': 'mismatch', 'candidate_id': 'osm:way:1', 'confidence': .99, 'observations': ['Does not match']}
    service.providers.osm.lookup = lookup
    service.providers.wikipedia.nearby = nearby
    gemini.identify_photo = uncertain
    story = create(service)
    result = await service.resolve_identity(story['id'])
    assert result['visual_identity']['status'] != 'match'
    assert result['state'] == 'needs_review'


@pytest.mark.asyncio
async def test_correction_excludes_old_object_and_persists_new_binding(tmp_path):
    service, gemini = make_service(tmp_path)
    original_nearby = service.providers.wikipedia.nearby
    async def with_alternative(*args):
        pages = await original_nearby(*args)
        return [*pages, {'pageid': 88, 'title': 'Another building', 'url': 'https://ru.wikipedia.org/wiki/Another', 'distance_m': 100}]
    service.providers.wikipedia.nearby = with_alternative
    story = create(service)
    await service.resolve_identity(story['id'])
    result = service.reject_identity(story['id'], 'wiki:77', 'Not this building')
    assert result['state'] == 'identifying'
    calls = []
    async def compare(_path, _mime, _text, candidates):
        calls.extend(x['candidate_id'] for x in candidates)
        return match(candidates[0]['candidate_id'])
    gemini.identify_photo = compare
    # The old OSM source has the same name but no longer needs to replace the
    # explicitly rejected wiki identity; absent alternatives remains uncertain.
    result = await service.resolve_identity(story['id'])
    assert 'wiki:77' not in calls
    assert result['visual_identity']['candidate_id'] == 'wiki:88'
    assert result['visual_identity']['source_links'] == ['https://ru.wikipedia.org/wiki/Another']
    assert result['visual_identity']['status'] == 'match'
    with service.store.connection() as db:
        prior = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?',(story['id'],)).fetchone()[0])
    assert prior['identity_history'][-1]['candidate_id'] == 'wiki:77'
    assert prior['identity_generation'] == 1


@pytest.mark.asyncio
async def test_late_visual_result_cannot_overwrite_owner_confirmation(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service)
    arrived, release = asyncio.Event(), asyncio.Event()
    async def delayed(*_):
        arrived.set()
        await release.wait()
        return match('wiki:77')
    gemini.identify_photo = delayed
    task = asyncio.create_task(service.resolve_identity(story['id']))
    await arrived.wait()
    with service.store.tx() as db:
        prior = {'identity_generation': 1, 'visual_identity': {'status': 'owner_confirmed', 'candidate_id': 'manual-other'}}
        db.execute("UPDATE stories SET research_json=?,place_name='Owner selected' WHERE id=?",(json.dumps(prior),story['id']))
    release.set()
    result = await task
    assert result['visual_identity']['candidate_id'] == 'manual-other'
    assert result['place_name'] == 'Owner selected'


def test_import_telemetry_does_not_accept_coordinates_uri_or_credentials():
    result = client_fields({'status': 'gps_redacted_or_invalid', 'gps_present': False,
        'token': 'private', 'latitude': 54.7, 'uri': 'content://private/photo', 'photo_bytes': 100})
    assert result == {'status': 'gps_redacted_or_invalid', 'gps_present': False, 'photo_bytes': 100}
