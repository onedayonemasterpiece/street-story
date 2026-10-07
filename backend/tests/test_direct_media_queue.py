import base64
from contextlib import contextmanager
import json
from io import BytesIO
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from street_story import live_visual_comparison as queue_module
from street_story.errors import RetryableProviderError
from street_story.headless_identity import HeadlessIdentity
from street_story.identity_progress import advance
from street_story.identity_references import reference_images
from street_story.identity_subject_binding import bind_reference_subject, reference_binding_valid
from street_story.reference_image_codec import reference_mime
from street_story.reference_image_codec import normalize_reference, MAX_LIVE_BYTES
from street_story.service import ConflictError


RAW_SOURCE = b'\xff\xd8\xffOriginal source bytes without image decoding'
REF = 'https://history.example/gate.jpg'


class Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.clock = 1000
        self.db.executescript('''
            CREATE TABLE stories(id TEXT, photo_sha256 TEXT, photo_mime_type TEXT, state TEXT,
                research_json TEXT, updated_at REAL, error_code TEXT);
            CREATE TABLE poi_aliases(namespace TEXT, normalized_value TEXT, poi_id TEXT);
            CREATE TABLE research_provider_attempts(story_id TEXT,role TEXT,receipt_json TEXT);
            CREATE TABLE live_commands(story_id TEXT,command_id TEXT,tool_name TEXT,request_digest TEXT,
                result_json TEXT,created_at REAL);
        ''')
    def now(self):
        return self.clock
    def cache_get(self, _key):
        return None
    @contextmanager
    def connection(self):
        yield self.db
    tx = connection


class Service:
    def __init__(self, candidates):
        self.store = Store()
        research = {'identity_generation': 0, 'visual_identity': {'status': 'uncertain', 'candidates': candidates}}
        self.store.db.execute('INSERT INTO stories VALUES(?,?,?,?,?,?,?)',
            ('story_direct', 'opaque-upload-token', 'image/jpeg', 'identifying', json.dumps(research), 1, None))
        self.raw = RAW_SOURCE
    def _story_row(self, db, sid):
        return db.execute('SELECT * FROM stories WHERE id=?', (sid,)).fetchone()
    def _identity_snapshot(self, sid):
        row = dict(self._story_row(self.store.db, sid))
        return row, json.loads(row['research_json'])
    def story(self, sid):
        row, research = self._identity_snapshot(sid)
        return {**row, **research}
    def _source_photo_bytes(self, _sid):
        if self.raw is None:
            raise ConflictError('source_unavailable', 'RAM source absent')
        return self.raw
    async def _candidate_reference_images(self, candidates, limit, *, story_id, evidence):
        return await reference_images(self, candidates, limit, story_id=story_id, evidence=evidence)


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(queue_module, 'record_identity_event', lambda *_args: None)
    candidate = {'candidate_id': 'wiki:1', 'name': 'Gate', 'url': 'https://history.example/gate',
        'reference_image_urls': [REF], 'identity_eligible': True}
    service = Service([candidate])
    adapter = HeadlessIdentity(service)
    session = SimpleNamespace(id='headless:direct:1', resource_id='story_direct', state={}, model='test-direct', closed=False)
    yield service, adapter, session
    service.store.db.close()


@pytest.mark.asyncio
async def test_reference_loader_passes_url_and_metadata_without_fetch_hash_or_disk():
    service = SimpleNamespace()
    evidence = []
    candidate = {'candidate_id': 'web:1', 'name': 'Gate', 'url': 'https://history.example/article',
        'reference_image_urls': [REF], 'reference_id': 'ref_one',
        'article_media': [{'image_url': REF, 'article_url': 'https://history.example/article',
                           'figcaption': 'A publisher caption', 'model_image_sha256': 'obsolete'}]}
    images = await reference_images(service, [candidate], 1, evidence=evidence)
    assert images == [('web:1', 'image/jpeg', REF)]
    assert evidence[0]['reference_id'] == 'ref_one'
    assert evidence[0]['figcaption'] == 'A publisher caption'
    assert not hasattr(service, '_identity_reference_cache')
    assert not any('sha' in key for key in evidence[0])


@pytest.mark.asyncio
async def test_pending_direct_source_and_ref_are_ram_only_and_survive_restart_with_same_operation(prepared):
    service, adapter, session = prepared
    await adapter._compare_place_images(session, {})
    pending = session.state['visual_comparison']['pending']
    assert pending['image_parts'] == [{'label': 'SOURCE', 'mime_type': 'image/jpeg',
        'data': base64.b64encode(RAW_SOURCE).decode()}, {'label': 'REF 1', 'mime_type': 'image/jpeg', 'url': REF}]
    saved = json.loads(service._story_row(service.store.db, session.resource_id)['research_json'])['visual_search_operation']
    assert saved['pending_descriptor']['id'] == pending['id']
    assert 'image_parts' not in saved['pending_descriptor'] and 'snapshot' not in saved['pending_descriptor']
    assert base64.b64encode(RAW_SOURCE).decode() not in json.dumps(saved)
    service.store.clock += 181
    restarted = SimpleNamespace(id='headless:direct:2', resource_id=session.resource_id, state={}, model='test-direct')
    await adapter._compare_place_images(restarted, {})
    assert restarted.state['visual_comparison']['pending']['id'] == pending['id']


@pytest.mark.asyncio
async def test_only_completed_verdict_advances_url_reference_cursor(prepared):
    service, adapter, session = prepared
    await adapter._compare_place_images(session, {})
    state = session.state['visual_comparison']
    pending = state['pending']
    assert state['reviewed_reference_ids'] == []
    result = adapter._record_place_comparison(session, 'completed-negative', {
        'comparison_id': pending['id'], 'status': 'mismatch', 'candidate_id': '',
        'confidence': 1, 'observations': ['Different visible opening.'], 'alternative_candidate_ids': []})
    assert result['matched'] is False
    assert state['reviewed_reference_ids'] == [pending['candidates'][0]['reference_id']]
    research = json.loads(service._story_row(service.store.db, session.resource_id)['research_json'])
    assert research['identity_progress']['images_reviewed_count'] == 1
    assert 'reviewed_image_sha256s' not in research['identity_progress']
    assert 'pending_descriptor' not in research['visual_search_operation']


@pytest.mark.asyncio
async def test_source_expiry_waits_without_acquisition_or_queue_mutation(prepared):
    service, adapter, session = prepared
    before = service._story_row(service.store.db, session.resource_id)['research_json']
    service.raw = None
    with pytest.raises(RetryableProviderError) as failure:
        await adapter._compare_place_images(session, {})
    assert str(failure.value) == 'source_unavailable'
    assert failure.value.retry_at == service.store.now() + 300
    assert service._story_row(service.store.db, session.resource_id)['research_json'] == before


def test_subject_gate_uses_sent_ref_id_and_url_without_image_digest():
    reference = {'candidate_id': 'web:1', 'reference_id': 'ref_one', 'name': 'Publisher title',
        'url': 'https://history.example/article', 'reference_image_urls': [REF]}
    physical = {'candidate_id': 'wiki:1', 'name': 'Physical gate', 'identity_eligible': True}
    evidence = {'candidate_id': 'web:1', 'reference_id': 'ref_one', 'source_url': REF,
        'article_url': 'https://history.example/article'}
    raw = {'status': 'match', 'candidate_id': 'web:1', 'reference_subject_candidate_id': 'wiki:1',
        '_references_sent': ['web:1'], '_reference_ids_sent': ['ref_one']}
    bound = bind_reference_subject(raw, [reference], [physical], [evidence])
    assert bound['status'] == 'bound'
    assert reference_binding_valid(bound['result'], [reference, physical])
    assert 'model_image_sha256' not in bound['binding']
    bad = bind_reference_subject(raw, [reference], [physical], [{**evidence, 'reference_id': 'other'}])
    assert bad['status'] == 'unresolved'


def test_mechanical_mime_reads_signature_without_modifying_source():
    assert reference_mime(RAW_SOURCE) == 'image/jpeg'
    assert reference_mime(b'\x89PNG\r\n\x1a\nOriginal') == 'image/png'
    assert reference_mime(b'RIFF1234WEBPOriginal') == 'image/webp'
    assert RAW_SOURCE.endswith(b'without image decoding')
    with pytest.raises(ValueError):
        reference_mime(b'not an image signature')


def test_progress_counts_reference_addresses_not_media_hashes():
    first = advance({}, 'identity_images_reviewed', {'reference_ids': ['ref_one']}, 1)
    second = advance(first, 'identity_images_reviewed', {'reference_ids': ['ref_one', 'ref_two']}, 2)
    assert first['images_reviewed_count'] == 1 and second['images_reviewed_count'] == 2
    assert 'reviewed_image_sha256s' not in second


@pytest.mark.asyncio
async def test_unknown_provider_operation_cannot_create_a_new_comparison(prepared):
    service, adapter, session = prepared
    service.store.db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?)',
        (session.resource_id, 'vision_native_direct', json.dumps({'phase': 'unknown',
            'photo_sha256': 'opaque-upload-token', 'generation': 0})))
    with pytest.raises(RetryableProviderError, match='research_visual_outcome_unknown'):
        await adapter._compare_place_images(session, {})
    assert not session.state['visual_comparison'].get('pending')
    assert session.state['visual_comparison']['reviewed_reference_ids'] == []


def ram_jpeg(*, orientation=1):
    from PIL import Image
    image = Image.new('RGB', (2400, 1200), 'blue')
    exif = Image.Exif()
    exif[274] = orientation
    output = BytesIO()
    image.save(output, format='JPEG', exif=exif)
    return output.getvalue()


def test_permitted_ram_preparation_rotates_and_downscales_without_file_io(monkeypatch):
    from PIL import Image
    raw = ram_jpeg(orientation=6)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Image persistence forbidden')
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    mime, prepared = normalize_reference(raw)
    assert mime == 'image/jpeg' and len(prepared) <= MAX_LIVE_BYTES
    with Image.open(BytesIO(prepared)) as decoded:
        assert decoded.width < decoded.height
        assert max(decoded.size) <= 1280
    with Image.open(BytesIO(raw)) as original:
        assert original.size == (2400, 1200)
        assert original.getexif()[274] == 6


@pytest.mark.asyncio
async def test_live_receives_separate_ram_source_and_reference_parts_without_montage(prepared, monkeypatch):
    from street_story import article_media
    service, adapter, session = prepared
    source, reference = ram_jpeg(orientation=6), ram_jpeg()
    async def fetch(_client, url, maximum):
        assert url == REF and maximum >= len(reference)
        return url, 'image/jpeg', reference
    monkeypatch.setattr(article_media, 'fetch_public', fetch)
    pending = {'reply': {'comparison_id': 'cmp_ram'}, 'image_parts': [
        {'label': 'SOURCE', 'mime_type': 'image/jpeg', 'data': base64.b64encode(source).decode()},
        {'label': 'REF 1', 'mime_type': 'image/jpeg', 'url': REF}]}
    result = await adapter._comparison_result(pending)
    assert len(result.parts) == 2
    assert [part['inlineData']['displayName'] for part in result.parts] == ['SOURCE', 'REF_1']
    assert all(len(base64.b64decode(part['inlineData']['data'])) <= MAX_LIVE_BYTES for part in result.parts)
    assert 'image_parts' not in result
