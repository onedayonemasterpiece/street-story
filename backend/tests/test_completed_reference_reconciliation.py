"""Recover completed observations through the queue without another inference."""
import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from street_story.article_media import extract_media
from street_story.errors import RetryableProviderError
from street_story.headless_identity import HeadlessIdentity, VERDICT_SCHEMA
from street_story.identity_references import reference_images
from street_story.live import ensure_live_schema
from street_story.live_visual_comparison import comparison_sheet
from street_story.reference_image_codec import normalize_reference
from street_story.research_adapter import semantic_visual_context
from street_story.service import canonical, ConflictError
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


ARTICLE = 'https://de.wikipedia.org/wiki/Example_gate'
ORIGINAL = 'https://upload.wikimedia.org/wikipedia/commons/e/e0/Example_gate.jpg'
ATTEMPT = 'rattempt_' + 'a' * 24


@pytest_asyncio.fixture
async def prepared(tmp_path):
    service, _gemini = make_service(tmp_path)
    photo = jpeg((800, 600))
    photo_sha = hashlib.sha256(photo).hexdigest()
    created = service.create_story(key='reconciliation-create', client_story_id='reconciliation',
        photo_sha256=photo_sha, photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    story, _research = service._identity_snapshot(created['id'])
    ensure_live_schema(service)
    reference = jpeg((640, 480))
    mime, pixels = normalize_reference(reference)
    ref_sha = hashlib.sha256(pixels).hexdigest()
    cid = 'web:' + hashlib.sha256(ARTICLE.encode()).hexdigest()[:16]
    body = f'<main><h1>Illustrated article</h1><img src="{ORIGINAL}" width="640" height="480"></main>'.encode()
    article = {'final_url': ARTICLE, 'body': base64.b64encode(body).decode(),
        'sha256': hashlib.sha256(body).hexdigest(), 'mime': 'text/html'}
    service.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(ARTICLE.encode()).hexdigest(), article, 86400)
    physical = {'candidate_id': 'osm:way:123', 'name': 'Physical gate', 'osm_id': 'osm:way:123',
                'url': 'https://www.openstreetmap.org/way/123'}
    identity = {'status': 'uncertain', 'candidates': [physical]}
    generation, revision = 4, 3
    comparison_id = 'comparison_' + hashlib.sha256(canonical([photo_sha, generation, revision, [ref_sha]]).encode()).hexdigest()[:32]
    title, _media = extract_media(body, ARTICLE)
    candidate = {'candidate_id': cid, 'name': title}
    sheet = comparison_sheet(story['photo_path'], [(cid, mime, pixels)])
    reply = HeadlessIdentity._visual_reply(comparison_id, [candidate], identity, 0)
    unit = hashlib.sha256(sheet).hexdigest() + hashlib.sha256((semantic_visual_context(canonical(reply)) + canonical(VERDICT_SCHEMA)).encode()).hexdigest()
    logical = hashlib.sha256(canonical([story['id'], photo_sha, generation, 'vision_native', unit]).encode()).hexdigest()
    verdict = {'status': 'match', 'candidate_id': cid, 'reference_subject_candidate_id': physical['candidate_id'],
               'confidence': .91, 'observations': ['Distinctive arch and brick openings correspond.'],
               'reference_subject_observations': ['Physical subject corresponds to the visible gate.'],
               'alternative_candidate_ids': []}
    receipt = {'phase': 'completed', 'provider': 'codex_native', 'model': 'gpt-6-luna',
               'transport': 'native_codex_app_server', 'photo_sha256': photo_sha, 'generation': generation,
               'thread_id': 'saved-thread', 'turn_id': 'saved-turn', 'profile_verified': True,
               'model_image_sha256': hashlib.sha256(sheet).hexdigest(), 'result': verdict,
               'binding': {'attempt_id': ATTEMPT, 'request_id': logical, 'story_id': story['id'],
                           'photo_sha256': photo_sha, 'generation': generation, 'control_revision': revision}}
    native_file = tmp_path / 'stories' / story['id'] / 'native-comparisons' / (ATTEMPT + '.jpg')
    native_file.parent.mkdir(parents=True)
    native_file.write_bytes(sheet)
    history = {'comparison_id': comparison_id, 'model_status': 'match', 'status': 'uncertain', 'matched': False,
               'binding_reason': 'reference_provenance_missing', 'reference_candidate_id': cid,
               'reference_subject_candidate_id': physical['candidate_id'], 'photo_sha256': photo_sha,
               'generation': generation, 'control_revision': revision,
               'references': [{'candidate_id': cid, 'url': ARTICLE, 'image_urls': [ORIGINAL]}]}
    state = {'photo_sha256': photo_sha, 'generation': generation, 'control_revision': revision,
             'queue': [], 'sources': {}, 'searches': {}, 'query': 'Visible gate', 'query_seed': 'Visible gate',
             'seen_images': ['older-reviewed-image', ref_sha], 'fetch_failures': [],
             'browser_budget': {'remaining': 0}, 'verdict_history': [history]}
    research = {'identity_generation': generation, 'visual_identity': identity, 'visual_search_operation': state,
                'identity_progress': {'generation': generation, 'reviewed_image_sha256s': ['older-reviewed-image', ref_sha]},
                'research_controls': {'identity': {'revision': revision, 'photo_sha256': photo_sha,
                                                  'identity_generation': generation, 'stopped': False}}}
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='identity_uncertain',research_json=? WHERE id=?", (canonical(research), story['id']))
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
                   (ATTEMPT, logical, story['id'], 'vision_native', canonical(receipt), service.store.now(), service.store.now()))
    calls, requests = [], []

    async def forbidden_model(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('completed reconciliation must never call a model')

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=reference)

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    async def load(candidates, limit=6, *, story_id=None, evidence=None):
        return await reference_images(service, candidates, limit=limit, story_id=story_id, http=http, evidence=evidence)

    service._candidate_reference_images = load
    service.providers.research = SimpleNamespace(vision_available=True, vision_model='gpt-6-luna', visual_verdict=forbidden_model)
    job = {'id': 'job-reconciliation', 'story_id': story['id'], 'attempts': 1,
           'payload_json': canonical({'identity_generation': generation})}
    yield SimpleNamespace(service=service, story=story, job=job, receipt=receipt, research=research,
                          article=article, native_file=native_file, ref_sha=ref_sha, calls=calls, requests=requests)
    await http.aclose()


def update_research(fixture):
    with fixture.service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(fixture.research), fixture.story['id']))


def update_receipt(fixture):
    with fixture.service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?', (canonical(fixture.receipt), ATTEMPT))


@pytest.mark.asyncio
async def test_normal_headless_queue_reconciles_completed_match_without_model_or_seen_reset(prepared):
    fixture = prepared
    await HeadlessIdentity(fixture.service).run(fixture.job)
    story, research = fixture.service._identity_snapshot(fixture.story['id'])
    assert story['state'] == 'identity_ready'
    identity = research['visual_identity']
    assert identity['candidate_id'] == 'osm:way:123' and identity['visual_reference_verified'] is True
    assert identity['provider_receipt'] == fixture.receipt
    evidence = identity['reference_evidence'][0]
    assert evidence['article_url'] == ARTICLE and evidence['source_url'] == ORIGINAL
    assert evidence['article_source_sha256'] == fixture.article['sha256']
    assert evidence['model_image_sha256'] == fixture.ref_sha
    assert '/thumb/' in evidence['resolved_image_url']
    assert research['visual_search_operation']['seen_images'] == ['older-reviewed-image', fixture.ref_sha]
    assert research['visual_search_operation']['verdict_history'][-1]['matched'] is True
    assert not fixture.calls and len(fixture.requests) == 1
    # A further ordinary job sees the resolved identity, with no extra fetch/send.
    await HeadlessIdentity(fixture.service).run(fixture.job)
    assert not fixture.calls and len(fixture.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason', [
    ('unknown', 'completed_receipt_missing'), ('missing_turn', 'completed_receipt_missing'),
    ('unverified_profile', 'completed_receipt_missing'), ('receipt_control', 'completed_receipt_missing'),
    ('sheet_hash', 'semantic_unit_changed'), ('catalog', 'semantic_unit_changed'),
    ('missing_input', 'immutable_input_missing'), ('changed_input', 'immutable_input_missing'),
    ('source_pixels', 'source_pixels_changed'), ('source_version', 'immutable_article_missing'),
    ('schema_unit', 'semantic_unit_changed'), ('missing_article', 'immutable_article_missing'),
    ('missing_extraction', 'article_extraction_unproved'), ('reference_pixels', 'reference_pixels_changed'),
    ('receipt_photo', 'completed_receipt_missing'), ('receipt_generation', 'completed_receipt_missing'),
    ('receipt_transport', 'completed_receipt_missing'),
])
async def test_missing_or_changed_proof_waits_without_inference_and_preserves_prior_verdict(prepared, change, reason):
    fixture = prepared
    if change == 'unknown':
        fixture.receipt['phase'] = 'turn_start_pending'
    elif change == 'missing_turn':
        fixture.receipt['turn_id'] = None
    elif change == 'unverified_profile':
        fixture.receipt['profile_verified'] = False
    elif change == 'receipt_control':
        fixture.receipt['binding']['control_revision'] -= 1
    elif change == 'sheet_hash':
        fixture.receipt['model_image_sha256'] = 'f' * 64
    elif change == 'catalog':
        fixture.research['visual_identity']['candidates'][0]['name'] = 'Changed catalog hypothesis'
        update_research(fixture)
    elif change == 'missing_input':
        fixture.native_file.unlink()
    elif change == 'changed_input':
        fixture.native_file.write_bytes(jpeg((640, 480)))
    elif change == 'source_pixels':
        from pathlib import Path
        Path(fixture.story['photo_path']).write_bytes(jpeg((400, 300)))
    elif change == 'source_version':
        fixture.article['sha256'] = 'f' * 64
        fixture.service.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(ARTICLE.encode()).hexdigest(), fixture.article, 86400)
    elif change == 'schema_unit':
        with fixture.service.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET logical_id=? WHERE attempt_id=?', ('different-schema-unit', ATTEMPT))
    elif change == 'missing_article':
        with fixture.service.store.tx() as db:
            db.execute('DELETE FROM cache')
    elif change == 'missing_extraction':
        body = b'<main><h1>Illustrated article</h1></main>'
        fixture.article.update(body=base64.b64encode(body).decode(), sha256=hashlib.sha256(body).hexdigest())
        fixture.service.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(ARTICLE.encode()).hexdigest(), fixture.article, 86400)
    elif change == 'reference_pixels':
        original = fixture.service._candidate_reference_images

        async def changed(*args, **kwargs):
            images = await original(*args, **kwargs)
            changed_pixels = normalize_reference(jpeg((320, 240)))[1]
            kwargs['evidence'][0]['model_image_sha256'] = hashlib.sha256(changed_pixels).hexdigest()
            return [(images[0][0], images[0][1], changed_pixels)]

        fixture.service._candidate_reference_images = changed
    elif change == 'receipt_photo':
        fixture.receipt['photo_sha256'] = 'f' * 64
    elif change == 'receipt_generation':
        fixture.receipt['generation'] -= 1
    elif change == 'receipt_transport':
        fixture.receipt['transport'] = 'text_only'
    update_receipt(fixture)
    with pytest.raises(RetryableProviderError) as failure:
        await HeadlessIdentity(fixture.service).run(fixture.job)
    assert str(failure.value) == 'identity_completed_reference_' + reason
    _story, research = fixture.service._identity_snapshot(fixture.story['id'])
    state = research['visual_search_operation']
    assert research['visual_identity']['status'] == 'uncertain'
    assert state['seen_images'] == ['older-reviewed-image', fixture.ref_sha]
    assert state['verdict_history'][0]['binding_reason'] == 'reference_provenance_missing'
    assert state['completed_reconciliation']['reason'] == reason
    assert not fixture.calls


@pytest.mark.asyncio
async def test_competing_physical_object_still_blocks_replayed_match(prepared):
    fixture = prepared
    # Preserve the original supplied catalog; a competing ID in the saved result
    # reaches the same common acceptance gate and cannot be dropped by recovery.
    fixture.receipt['result']['alternative_candidate_ids'] = ['outside-original-shortlist']
    update_receipt(fixture)
    await HeadlessIdentity(fixture.service).run(fixture.job)
    _story, research = fixture.service._identity_snapshot(fixture.story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert research['visual_search_operation']['verdict_history'][-1]['matched'] is False
    assert not fixture.calls


@pytest.mark.asyncio
async def test_stop_during_reference_load_fences_reconciliation_commit(prepared):
    fixture = prepared
    original = fixture.service._candidate_reference_images

    async def stopping(*args, **kwargs):
        images = await original(*args, **kwargs)
        fixture.research['research_controls']['identity']['revision'] += 1
        fixture.research['research_controls']['identity']['stopped'] = True
        update_research(fixture)
        return images

    fixture.service._candidate_reference_images = stopping
    # Headless catches the superseded scope; late pixels never reach the gate.
    await HeadlessIdentity(fixture.service).run(fixture.job)
    _story, research = fixture.service._identity_snapshot(fixture.story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert research['visual_search_operation']['verdict_history'][0]['matched'] is False
    assert not fixture.calls


@pytest.mark.asyncio
async def test_lease_loss_during_reference_load_prevents_any_reconciled_commit(prepared):
    fixture = prepared
    original = fixture.service._candidate_reference_images

    async def replacing(*args, **kwargs):
        images = await original(*args, **kwargs)
        _story, research = fixture.service._identity_snapshot(fixture.story['id'])
        research['visual_search_operation']['lease_owner'] = 'another-worker'
        with fixture.service.store.tx() as db:
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), fixture.story['id']))
        return images

    fixture.service._candidate_reference_images = replacing
    with pytest.raises(ConflictError):
        await HeadlessIdentity(fixture.service).run(fixture.job)
    _story, research = fixture.service._identity_snapshot(fixture.story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert not fixture.calls
