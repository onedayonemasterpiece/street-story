import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.headless_identity import HeadlessIdentity
from street_story.service import canonical
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


def topic_with_vision(tmp_path):
    service, _ = make_service(tmp_path)
    photo = jpeg()
    topic = service.create_story(key='overlap', client_story_id='overlap',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg',
        photo_bytes=photo, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    service.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture')
    return service, topic, photo


@pytest.mark.asyncio
async def test_ready_reference_runs_while_discovery_waits_and_late_discovery_keeps_match(tmp_path, monkeypatch):
    svc, topic, photo = topic_with_vision(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def discovery(*args):
        entered.set()
        await release.wait()
        return None

    async def images(candidates, limit, *, story_id, evidence):
        c = candidates[0]
        evidence.append({'candidate_id': c['candidate_id'], 'source_url': c['reference_image_urls'][0]})
        return [(c['candidate_id'], 'image/jpeg', photo)]

    async def verdict(_photo, story, schema, context):
        assert entered.is_set() and not release.is_set()
        assert len(story['_visual_image_parts']) == 2
        c = story['_visual_reference_mapping'][0]['candidate_id']
        return {'result': {'status': 'match', 'candidate_id': c, 'confidence': .99,
                'observations': ['Same distinctive arch and facade'], 'alternative_candidate_ids': []},
                'receipt': {'phase': 'completed', 'model': 'fixture'}}

    monkeypatch.setattr(identity_discovery, 'recover', discovery)
    svc._candidate_reference_images = images
    svc.providers.research.visual_verdict = verdict
    svc.ensure_identity(topic['id'])
    task = asyncio.create_task(svc.run_once(exclude_kind='identity_visual'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert svc.story(topic['id'])['visual_identity']['candidates']
        assert await svc.run_once(claim_kind='identity_visual')
        matched = svc.story(topic['id'])
        assert matched['visual_identity']['status'] == 'match'
        assert matched['visual_identity']['candidate_id'] == 'wiki:77'
        release.set()
        assert await task
        result = svc.story(topic['id'])
        assert result['visual_identity']['status'] == 'match'
        assert result['state'] == 'identity_ready'
        assert result['place_name'] == matched['place_name']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_visual_lane_cannot_claim_research_or_publication(tmp_path):
    svc, topic, _ = topic_with_vision(tmp_path)
    with svc.store.tx() as db:
        for kind in ['research', 'visual', 'publish', 'identity']:
            svc._enqueue_job(db, topic['id'], kind, kind, {})
    assert not await svc.run_once(claim_kind='identity_visual')
    with svc.store.connection() as db:
        assert {r['state'] for r in db.execute('SELECT state FROM jobs')} == {'ready'}


@pytest.mark.asyncio
@pytest.mark.parametrize('exhausted', [True, False])
async def test_finite_exhaustion_finishes_but_transient_wait_remains_resumable(tmp_path, monkeypatch, exhausted):
    svc, topic, _ = topic_with_vision(tmp_path)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': [{'candidate_id': 'gate'}]}}), topic['id']))
    async def empty(self, session, args, **kwargs):
        return {'exhausted': True} if exhausted else {'partial': True}
    monkeypatch.setattr(HeadlessIdentity, '_compare_place_images', empty)
    assert await svc.run_once(claim_kind='identity_visual')
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity_visual'").fetchone())
    assert job['state'] == ('done' if exhausted else 'retry')
    assert not await svc.run_once(claim_kind='identity_visual')


@pytest.mark.asyncio
async def test_explicit_owner_hint_reopens_attempted_identity_but_not_confirmed(tmp_path, monkeypatch):
    svc, topic, _ = topic_with_vision(tmp_path)
    calls = []
    async def recover(service, story, transcript, candidates, excluded):
        calls.append(transcript)
        return None
    monkeypatch.setattr(identity_discovery, 'recover', recover)
    await svc.resolve_identity(topic['id'])
    assert calls == ['']
    await svc.resolve_identity(topic['id'], 'unrelated previous transcript')
    assert calls == ['']
    await svc.resolve_identity(topic['id'], 'Owner: exact new address', owner_hint='exact new address')
    assert calls == ['', 'Owner: exact new address']
    with svc.store.tx() as db:
        r = {'identity_attempted_generation': 0, 'visual_identity': {'status': 'match', 'candidate_id': 'wiki:77'}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(r), topic['id']))
    await svc.resolve_identity(topic['id'], 'another address', owner_hint='another address')
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_completed_suggestion_does_not_start_search_after_parallel_match(tmp_path, monkeypatch):
    svc, topic, _ = topic_with_vision(tmp_path)
    svc.providers.gemini._generate = object()
    svc.providers.gemini.executor = object()
    async def suggest(service, story, transcript, candidates):
        with service.store.tx() as db:
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
                'visual_identity': {'status': 'match', 'candidate_id': 'wiki:77'}}), topic['id']))
        return 'gate', [], 'gate', ''
    async def forbidden(*args, **kwargs):
        pytest.fail('proved identity must not start more discovery work')
    monkeypatch.setattr(identity_discovery, 'suggest', suggest)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    story, _ = svc._identity_snapshot(topic['id'])
    assert await identity_discovery.recover(svc, story, '', [], set()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('browser_result,expected', [('completed', 'completed'),
    ('partial', 'temporary_failure'), ('failed', 'temporary_failure')])
async def test_no_image_page_finishes_only_after_complete_browser_enumeration(tmp_path, browser_result, expected):
    import httpx
    from street_story import article_media
    svc, topic, _ = topic_with_vision(tmp_path)
    async def resolve(host):
        return '93.184.216.34'
    async def browser(url):
        if browser_result == 'failed':
            raise OSError('browser not available')
        return 'Image-free page', article_media.RenderedMedia([], 1, browser_result == 'partial', 0)
    receipts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            headers={'content-type': 'text/html'}, text='<article><h1>Image-free page</h1></article>'))) as http:
        assert await article_media.article_candidates(svc, topic, [{'url': 'https://example.com/building'}], set(),
            http=http, resolver=resolve, browser=browser, receipts=receipts) == []
    assert receipts[0]['status'] == expected
    assert receipts[0]['image_count'] == 0


@pytest.mark.asyncio
async def test_image_free_page_without_browser_slot_remains_resumable(tmp_path):
    import httpx
    from street_story import article_media
    svc, topic, _ = topic_with_vision(tmp_path)
    async def resolve(host):
        return '93.184.216.34'
    async def browser(url):
        return 'Empty', article_media.RenderedMedia([], 1, False)
    receipts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            headers={'content-type': 'text/html'}, text='<article>No images</article>'))) as http:
        await article_media.article_candidates(svc, topic,
            [{'url': f'https://example.com/empty-{i}'} for i in range(3)], set(),
            http=http, resolver=resolve, browser=browser, receipts=receipts)
    assert sum(r['status'] == 'completed' for r in receipts) == 2
    assert sum(r['status'] == 'temporary_failure' for r in receipts) == 1
