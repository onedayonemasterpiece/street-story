"""Ended workers release queue ownership, not submitted inference receipts."""
import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from visual_queue_fixture import queued_reference_count

from street_story.errors import RetryableProviderError
from street_story.headless_identity import HeadlessIdentity
from street_story.identity_references import reference_images
from street_story.live import ensure_live_schema
from street_story.native_quota import NativeQuotaPermission
from street_story.native_vision import MODEL, TRANSPORT, VERIFICATION_KEY, NativeVisionProvider
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical
from test_identity_lifecycle import make_service
from test_native_vision import NativeClient
from test_reference_image_codec import jpeg


class ControlledClient(NativeClient):
    def __init__(self):
        super().__init__()
        self.first_read_error = False
        self.wait_for_read = False
        self.read_entered, self.allow_read = asyncio.Event(), asyncio.Event()

    async def request(self, method, params, timeout=30):
        if method == 'thread/read':
            self.read_entered.set()
            if self.wait_for_read:
                await self.allow_read.wait()
            if self.first_read_error:
                self.first_read_error = False
                self.calls.append((method, copy.deepcopy(params)))
                raise RetryableProviderError('controlled_read_unavailable', retry_at=1001)
        result = await super().request(method, params, timeout)
        if method == 'thread/read':
            start = next(body for name, body in self.calls if name == 'turn/start' and body['threadId'] == params['threadId'])
            message = result['thread']['turns'][0]['items'][1]
            verdict = json.loads(message['text'])
            verdict['candidate_id'] = start['outputSchema']['properties']['candidate_id']['enum'][1]
            message['text'] = canonical(verdict)
        return result


@pytest_asyncio.fixture
async def prepared(tmp_path):
    service, _gemini = make_service(tmp_path)
    service.settings = replace(service.settings, native_vision_reserve=True)
    clock = [1000.0]
    service.store.now = lambda: clock[0]
    photo = jpeg((800, 600))
    story = service.create_story(key='lease-create', client_story_id='lease-create',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    ensure_live_schema(service)
    candidate = {'candidate_id': 'wiki:77', 'name': 'Physical gate', 'url': 'https://ru.wikipedia.org/wiki/Gate',
                 'reference_image_urls': ['https://upload.wikimedia.org/wikipedia/commons/1/1b/Gate.jpg']}
    research = {'identity_generation': 0, 'visual_identity': {'status': 'uncertain', 'candidates': [candidate]}}
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='needs_review',research_json=? WHERE id=?", (canonical(research), story['id']))
        semantic = f"identity-visual:{story['id']}:0:{story['photo_sha256']}"
        job_id = service._enqueue_job(db, story['id'], 'identity_visual', semantic, {'identity_generation': 0})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))))

    async def load(candidates, limit=6, *, story_id=None, evidence=None):
        return await reference_images(service, candidates, limit=limit, http=http, story_id=story_id, evidence=evidence)

    service._candidate_reference_images = load
    client, sends, finalizations = ControlledClient(), [], []
    controls = SimpleNamespace(before_send_failure=False)

    @asynccontextmanager
    async def admission(binding, workload):
        async def before_send(metadata):
            if controls.before_send_failure:
                controls.before_send_failure = False
                raise RetryableProviderError('controlled_budget_wait', retry_at=1001)
            sends.append(copy.deepcopy(metadata))

        async def finalize(metadata, state):
            finalizations.append(state)

        yield SimpleNamespace(before_send=before_send, finalize=finalize)

    adapter = object.__new__(ProductResearchAdapter)
    adapter.service, adapter.client, adapter.giga = service, None, None
    adapter.primary_vision = SimpleNamespace(available=False, _verified_routes=lambda: [])
    permission = NativeQuotaPermission(service.store, binding_stamp=lambda: 'fixture-owner')
    async def native_reference(url):
        assert url == candidate['reference_image_urls'][0]
        return 'image/jpeg', jpeg((640, 480))

    adapter.native_vision = NativeVisionProvider(service, admission=admission, checkpoint=adapter.checkpoint,
        client_factory=lambda: client, permission=permission, public_image_loader=native_reference)
    adapter.native_vision.poll_seconds = .001
    service.providers.research = adapter
    service.store.cache_put(VERIFICATION_KEY, {'model': MODEL, 'transport': TRANSPORT,
        'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}}, 3600)
    yield SimpleNamespace(service=service, sid=story['id'], photo_sha=story['photo_sha256'], job_id=job_id,
                          clock=clock, controls=controls, client=client, sends=sends, adapter=adapter)
    await http.aclose()
    await adapter.native_vision.close()


def snapshot(fixture):
    with fixture.service.store.connection() as db:
        row = db.execute('SELECT * FROM stories WHERE id=?', (fixture.sid,)).fetchone()
        research = json.loads(row['research_json'])
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (fixture.job_id,)).fetchone())
        attempts = [dict(record) for record in db.execute("SELECT * FROM research_provider_attempts WHERE story_id=? AND role='vision_native'", (fixture.sid,))]
        return research, job, attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['before_send', 'submitted', 'unknown'])
async def test_normal_worker_retry_releases_lease_and_resumes_same_pair_without_new_turn(prepared, failure):
    fixture = prepared
    fixture.controls.before_send_failure = failure == 'before_send'
    fixture.client.first_read_error = failure in {'submitted', 'unknown'}
    assert await fixture.service.run_once()
    research, job, attempts = snapshot(fixture)
    state = research['visual_search_operation']
    assert job['state'] == 'retry' and state['lease_owner'] is None and state['lease_until'] == 0
    assert queued_reference_count(state) == 1 and not state['reviewed_reference_ids']
    assert len(attempts) == 1
    first = json.loads(attempts[0]['receipt_json'])
    assert first['phase'] == ('created' if failure == 'before_send' else 'submitted')
    original_thread, original_turn = first['thread_id'], first['turn_id']
    if failure == 'unknown':
        first['phase'] = 'unknown'
        with fixture.service.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                       (canonical(first), attempts[0]['attempt_id']))
    # The queue can resume one second later, well before its former 180s lease.
    fixture.clock[0] = job['available_at']
    assert await fixture.service.run_once()
    research, job, resumed = snapshot(fixture)
    assert job['state'] == 'retry' and job['last_error'] is None
    assert job['available_at'] == fixture.clock[0]  # Progress gets no failure backoff.
    assert len(resumed) == 1 and resumed[0]['attempt_id'] == attempts[0]['attempt_id']
    completed = json.loads(resumed[0]['receipt_json'])
    assert completed['phase'] == 'completed' and completed['result']['status'] == 'mismatch'
    assert completed['thread_id'] == original_thread
    if original_turn:
        assert completed['turn_id'] == original_turn
        assert completed['binding']['job_id'] == first['binding']['job_id']
        assert completed['binding']['job_attempt'] == first['binding']['job_attempt']
        assert completed['quota_permission'] == first['quota_permission']
    assert sum(name == 'thread/start' for name, _ in fixture.client.calls) == 1
    assert sum(name == 'turn/start' for name, _ in fixture.client.calls) == 1
    assert len(fixture.sends) == 1
    assert len(research['visual_search_operation']['verdict_history']) == 1
    assert len(research['visual_search_operation']['reviewed_reference_ids']) == 1
    assert research['identity_progress']['images_reviewed_count'] == 1
    assert research['visual_search_operation']['lease_owner'] is None
    assert snapshot(fixture)[0]['identity_progress']['images_reviewed_count'] == 1


@pytest.mark.asyncio
async def test_generic_worker_exception_releases_only_lease_and_keeps_pending_pair(prepared):
    fixture = prepared

    async def failure(*args, **kwargs):
        raise RuntimeError('controlled product failure')

    fixture.adapter.visual_verdict = failure
    assert await fixture.service.run_once()
    research, job, attempts = snapshot(fixture)
    assert job['state'] == 'retry' and job['last_error'] == 'worker_failure:RuntimeError'
    state = research['visual_search_operation']
    assert state['lease_owner'] is None and state['lease_until'] == 0
    assert queued_reference_count(state) == 1 and not state['reviewed_reference_ids']
    assert state['query'] == 'Physical gate'
    assert state['sources'] and not attempts


@pytest.mark.asyncio
async def test_no_pending_provider_work_releases_owned_lease_without_replacing_state(prepared):
    fixture = prepared

    class EmptyUnit(HeadlessIdentity):
        async def _compare_place_images(self, session, args, **kwargs):
            state = self._visual_lease(session, expected=kwargs['expected_scope'])
            state.update(photo_sha256=fixture.photo_sha, generation=0, queue=[], query='saved query',
                         reviewed_reference_ids=['earlier'], sources={}, searches={'earlier query': {'status': 'completed'}})
            session.state['visual_comparison'] = state
            self._save_visual_queue(session, state)
            return {'partial': True}

    with fixture.service.store.connection() as db:
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (fixture.job_id,)).fetchone())
    with pytest.raises(RetryableProviderError, match='identity_background_waiting'):
        await EmptyUnit(fixture.service).run(job)
    state = snapshot(fixture)[0]['visual_search_operation']
    assert state['lease_owner'] is None and state['lease_until'] == 0
    assert state['query'] == 'saved query' and state['reviewed_reference_ids'] == ['earlier']
    assert state['searches'] == {'earlier query': {'status': 'completed'}}
    assert not fixture.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stop', 'stop_resume', 'photo', 'generation', 'replacement'])
async def test_awaiting_worker_keeps_lease_and_late_exit_cannot_clear_changed_owner_scope(prepared, change):
    fixture = prepared
    fixture.client.wait_for_read = True
    task = asyncio.create_task(fixture.service.run_once())
    await asyncio.wait_for(fixture.client.read_entered.wait(), timeout=5)
    research, job, _attempts = snapshot(fixture)
    active = research['visual_search_operation']
    assert active['lease_owner'] == f"headless:{fixture.job_id}:{job['attempts']}"
    assert active['lease_until'] > fixture.clock[0]
    if change in {'stop', 'stop_resume'}:
        research['research_controls'] = {'identity': {'stopped': change == 'stop',
            'photo_sha256': fixture.photo_sha, 'identity_generation': 0,
            'revision': 1 if change == 'stop' else 2}}
    elif change == 'generation':
        research['identity_generation'] = 1
    elif change == 'replacement':
        active.update(lease_owner='another-worker', lease_until=1500)
    with fixture.service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), fixture.sid))
        if change == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('f' * 64, fixture.sid))
    expected_state = copy.deepcopy(active)
    fixture.client.allow_read.set()
    assert await task
    latest, _job, _attempts = snapshot(fixture)
    assert latest['visual_search_operation'] == expected_state
    assert not latest['visual_search_operation']['reviewed_reference_ids']
    assert not latest.get('identity_progress', {}).get('images_reviewed_count')
    assert sum(name == 'turn/start' for name, _ in fixture.client.calls) == 1


@pytest.mark.asyncio
async def test_worker_task_cancellation_releases_owned_queue_but_preserves_unknown_turn(prepared):
    fixture = prepared
    fixture.client.wait_for_read = True
    task = asyncio.create_task(fixture.service.run_once())
    await asyncio.wait_for(fixture.client.read_entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    research, _job, attempts = snapshot(fixture)
    assert research['visual_search_operation']['lease_owner'] is None
    assert queued_reference_count(research['visual_search_operation']) == 1
    receipt = json.loads(attempts[0]['receipt_json'])
    assert receipt['phase'] == 'unknown' and receipt['thread_id'] and receipt['turn_id']
    assert sum(name == 'turn/start' for name, _ in fixture.client.calls) == 1


@pytest.mark.asyncio
async def test_native_readback_rejects_caller_without_current_job_lease(prepared):
    import base64
    from street_story.headless_identity import VERDICT_SCHEMA
    from street_story.service import ConflictError
    fixture = prepared
    fixture.client.first_read_error = True
    assert await fixture.service.run_once()
    research, job, attempts = snapshot(fixture)
    assert job['state'] == 'retry'  # No worker currently owns this job.
    pending = research['visual_search_operation']['pending_descriptor']
    context = pending['reply']
    story = {'id': fixture.sid, '_identity_generation': 0,
        '_research_job_id': fixture.job_id, '_research_job_attempt': job['attempts'],
        '_visual_reference_mapping': context['references'],
        '_visual_image_parts': [{'label': 'SOURCE', 'mime_type': 'image/jpeg',
            'data': base64.b64encode(fixture.service._source_photo_bytes(fixture.sid)).decode()},
            {'label': 'REF 1', 'url': pending['candidates'][0]['reference_image_urls'][0]}]}
    calls_before = len(fixture.client.calls)
    with pytest.raises(ConflictError, match='не владеет'):
        await fixture.adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
    assert len(fixture.client.calls) == calls_before
    assert len(fixture.sends) == 1
    assert snapshot(fixture)[2] == attempts
