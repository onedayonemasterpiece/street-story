from direct_visual_fixture import visual_args
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from street_story.research_adapter import ProductResearchAdapter
from street_story.research_control import resume_research, stop_research
from street_story.service import ConflictError
from test_research_control import fixture


@pytest.mark.asyncio
async def test_spatial_native_reads_original_turn_without_fresh_availability_and_fences_source_scope(tmp_path):
    from street_story.errors import RetryableProviderError
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    class Native:
        available = True
        sends = 0
        async def compare_source_map(self, story, schema, prompt, images, binding, host_context):
            if not binding.get('turn_id'):
                self.sends += 1
                receipt = {'binding': binding, 'phase': 'unknown', 'turn_id': 'original', 'thread_id': 'thread',
                    'frozen_source_map': {'host_context': host_context}, 'profile_verified': True}
                await adapter.checkpoint(binding, receipt)
                raise RetryableProviderError('native_turn_outcome_unknown')
            assert binding['turn_id'] == 'original'
            frozen = binding['frozen_source_map']['host_context']
            receipt = {'binding': binding, 'phase': 'completed', 'turn_id': 'original',
                       'frozen_source_map': binding['frozen_source_map'], 'result': {'decision': 'uncertain'}}
            await adapter.checkpoint(binding, receipt)
            return {'result': receipt['result'], 'receipt': receipt, 'host_context': frozen}
    adapter.native_vision = Native()
    story = {'id': sid, 'photo_sha256': photo, '_identity_generation': 0}
    host = {'source_map_receipt': {'manifest': 'original-map'}}
    with pytest.raises(RetryableProviderError, match='native_turn_outcome_unknown'):
        await adapter.plan_source_map(story, 'original', {}, [('SOURCE', 'image/jpeg', b'pixels')], host)
    adapter.native_vision.available = False
    result = await adapter.plan_source_map(story, 'different', {}, [], {})
    assert result['host_context'] == host and adapter.native_vision.sends == 1
    assert adapter.source_map_receipt({**story, 'photo_sha256': 'new-source'}) is None
    assert adapter.source_map_receipt({**story, '_identity_research_control_revision': 1}) is None


@pytest.mark.asyncio
async def test_reference_download_failure_does_not_block_independent_fact_model(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from street_story.errors import RetryableProviderError
    from street_story.opencode_research import ResearchUnavailable
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.client = SimpleNamespace(endpoint='http://existing-opencode:4097', model_id='configured', provider_id='opencode')
    story = {'id': sid, 'photo_sha256': photo}
    async def unavailable(binding):
        raise ResearchUnavailable('research_image_reference_unavailable', {'provider_send_state': 'not_sent'})
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'vision', 'missing-ref', unavailable)
    route_key = 'research-route-health:' + hashlib.sha256(canonical([
        adapter.client.endpoint, adapter.client.model_id, None, None]).encode()).hexdigest()
    assert service.store.cache_get(route_key) is None
    # Recover the erroneous route-wide wait emitted by the prior release.
    service.store.cache_put(route_key, {'category': 'research_image_reference_unavailable',
        'retry_at': service.store.now()+300}, 300)
    async def independent(binding):
        return {'result': 'own grounded fact'}
    assert await adapter.run(story, 'facts', 'independent', independent) == {'result': 'own grounded fact'}
    service.store.cache_put(route_key, {'category': 'research_provider_credential_or_eligibility',
        'status': 403, 'retry_at': service.store.now()+3600}, 3600)
    with pytest.raises(RetryableProviderError):
        await adapter.run(story, 'facts', 'blocked-by-real-provider', independent)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['abort_outcome_unknown', 'unknown'])
async def test_unknown_addressed_request_readback_is_not_blocked_by_inference_cooldown(tmp_path, phase):
    import hashlib
    from types import SimpleNamespace
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.client = SimpleNamespace(endpoint='http://existing-opencode:4097', model_id='configured', provider_id='opencode')
    story = {'id': sid, 'photo_sha256': photo}
    binding, _ = adapter.attempt(story, 'search', 'same-unit')
    saved = {'binding': binding, 'phase': phase, 'session_id': 'sesExisting', 'message_id': 'msgExisting'}
    with service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                   (canonical(saved), binding['attempt_id']))
    route_key = 'research-route-health:' + hashlib.sha256(canonical([
        adapter.client.endpoint, adapter.client.model_id, None, None]).encode()).hexdigest()
    health = {'category': 'research_provider_quota', 'retry_at': service.store.now()+3600}
    for key in [route_key, 'research-quota-health:opencode:configured']:
        service.store.cache_put(key, health, 3600)
    called = []
    async def readback(current):
        called.append(current)
        assert current['phase'] == phase
        assert current['session_id'] == 'sesExisting' and current['message_id'] == 'msgExisting'
        return {'result': 'existing-response'}
    assert await adapter.run(story, 'search', 'same-unit', readback) == {'result': 'existing-response'}
    assert len(called) == 1
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts WHERE story_id=?', (sid,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_all_search_routes_blocked_retains_independent_and_google_failures(tmp_path):
    from types import SimpleNamespace
    from street_story.errors import RetryableProviderError
    from street_story.gemini import GeminiUnavailable
    service, sid, _photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service

    async def independent(query, story):
        raise RetryableProviderError('RESOURCE_NO_CAPACITY', retry_at=120)

    async def google(query):
        raise GeminiUnavailable(240, 'article_url_discovery_unavailable')

    adapter.search_articles = independent
    service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    with pytest.raises(RetryableProviderError) as error:
        await adapter.search_fact_articles('place history', {'id': sid})
    assert error.value.route_failures == {'opencode': 'RESOURCE_NO_CAPACITY',
                                         'google': 'gemini:article_url_discovery_unavailable'}
    assert error.value.retry_at == 120
    assert 'RESOURCE_NO_CAPACITY' in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('google_status', ['completed_empty', 'selection_unavailable'])
async def test_completed_empty_search_uses_one_alternative_and_replays_closed_result(tmp_path, google_status):
    from types import SimpleNamespace
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    calls = []
    async def independent(query, story):
        calls.append('opencode')
        return {'sources': [], 'receipt': {'phase': 'completed'}}
    async def google(query):
        calls.append('google')
        return SimpleNamespace(grounding_sources=[], payload={'status': google_status,
            'discovered_sources': [{'url': 'https://example.org/a'}], 'source_selection': {'status': 'malformed'}})
    adapter.search_articles = independent
    service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    story = {'id': sid, 'photo_sha256': photo}
    first = await adapter.search_fact_articles('place history', story)
    assert first['outcome'] == google_status
    assert await adapter.search_fact_articles('place history', story) == first
    assert calls == ['opencode', 'google']


@pytest.mark.asyncio
async def test_unknown_fact_search_keeps_original_route_without_fallback(tmp_path):
    from types import SimpleNamespace
    from street_story.errors import RetryableProviderError
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    async def independent(query, story):
        raise RetryableProviderError('research_attempt_unknown')
    async def google(query):
        pytest.fail('UNKNOWN cannot authorize a replacement search')
    adapter.search_articles = independent
    service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    with pytest.raises(RetryableProviderError, match='unknown'):
        await adapter.search_fact_articles('place history', {'id': sid, 'photo_sha256': photo})


@pytest.mark.parametrize('phase', ['created', 'submitted', 'abort_outcome_unknown'])
def test_route_failure_diagnostics_never_reset_unknown_dispatch_phase(tmp_path, phase):
    import json
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    binding, _ = adapter.attempt({'id': sid, 'photo_sha256': photo}, 'search', 'query')
    before = {'binding': binding, 'phase': phase, 'session_id': 'saved-session', 'message_id': 'saved-message'}
    with service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                   (canonical(before), binding['attempt_id']))
    adapter._record_route_failure(binding, 'search', 'RESOURCE_NO_CAPACITY', 120)
    with service.store.connection() as db:
        after = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                                     (binding['attempt_id'],)).fetchone()[0])
    assert {key: after[key] for key in before} == before
    assert after['route_failure']['code'] == 'RESOURCE_NO_CAPACITY'


@pytest.mark.asyncio
async def test_admission_wait_cannot_send_old_worker_after_stop_resume(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter._active_binding = ContextVar('test_binding', default=None)
    sends = []
    class Lease:
        async def before_send(self, metadata):
            sends.append(metadata)
        async def finalize(self, metadata, state):
            pass
    @asynccontextmanager
    async def waiting(binding, workload):
        stop_research(service, sid, purpose='identity')
        resume_research(service, sid, purpose='identity')
        yield Lease()
    binding = {'story_id': sid, 'photo_sha256': photo, 'generation': 0,
               'purpose': 'identity', 'control_revision': 0}
    async with adapter.fenced_admission(waiting)(binding, {}) as lease:
        with pytest.raises(ConflictError, match='остановлено'):
            await lease.before_send({})
    assert not sends


def test_search_history_retains_source_reuse_context_without_search_call_ceiling(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    from street_story.service import canonical
    sources = [{'url': f'https://example.org/article/{i}', 'title': f'Article {i}'} for i in range(25)]
    with service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   ('search', 'logical', sid, 'search', canonical({'sources': sources, 'search_calls': [{'query': 'first query'}]}), 1, 1))
    history = adapter.search_history({'id': sid, 'photo_sha256': photo})
    assert len(history['found_sources']) == 25
    assert history['prior_search_queries'] == ['first query']
    assert history['completed_for_scope'] == []
    assert history['omitted_history_items'] == 0


@pytest.mark.asyncio
async def test_new_comparison_id_after_stop_resume_requires_new_native_observation(tmp_path):
    from types import SimpleNamespace
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.primary_vision = SimpleNamespace(available=False)
    calls = []
    class Native:
        available = True
        async def compare_visual(self, snapshot, story, schema, context, binding):
            calls.append(binding)
            receipt = {'phase': 'completed', 'binding': binding, 'result': {'status': 'mismatch'}}
            await adapter.checkpoint(binding, receipt)
            return {'result': receipt['result'], 'receipt': receipt}
    adapter.native_vision = Native()
    adapter.client = None
    story = {'id': sid, 'photo_sha256': photo, '_identity_generation': 0}
    context = {'comparison_id': 'old-lease', 'remaining_illustrations': 3,
               'references': [{'candidate_id': 'wiki:1'}], 'physical_candidates': [{'candidate_id': 'wiki:1'}]}
    first = await adapter.visual_verdict(*visual_args(b'pixels', story, {}, canonical(context)))
    stop_research(service, sid, purpose='identity')
    resume_research(service, sid, purpose='identity')
    with service.store.connection() as db:
        import json
        state = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
    resumed = {**story, '_identity_research_control_revision': state['research_controls']['identity']['revision']}
    second = await adapter.visual_verdict(*visual_args(b'pixels', resumed, {},
                                          canonical({**context, 'comparison_id': 'new-lease', 'remaining_illustrations': 8})))
    assert first['result'] == second['result'] and len(calls) == 2
    # Reading the first operation's completed result does not submit a third turn.
    await adapter.visual_verdict(*visual_args(b'pixels', resumed, {}, canonical(context)))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_native_reserve_refusal_reaches_qualified_opencode_vision_fallback(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from street_story.errors import RetryableProviderError
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.primary_vision = SimpleNamespace(available=False)
    adapter.client = SimpleNamespace(model_id='verified-vision', endpoint='http://existing-opencode:4097')
    service.store.cache_put('research-vision-verification-v1', {
        'model_id': adapter.client.model_id, 'endpoint': adapter.client.endpoint,
        'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}, ttl_seconds=3600)
    async def native(*args):
        raise RetryableProviderError('RESOURCE_NO_CAPACITY', retry_at=service.store.now()+60)
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    calls = []
    async def compare(pixels, *args):
        assert pixels is None
        calls.append(pixels)
        return {'result': {'status': 'match'}, 'receipt': {'provider_id': 'qualified-vision'}}
    adapter.compare_image = compare
    output = io.BytesIO()
    Image.new('RGB', (32, 32)).save(output, format='JPEG')
    result = await adapter.visual_verdict(*visual_args(output.getvalue(), {'id': sid, 'photo_sha256': photo}, {}, canonical({
        'references': [{'candidate_id': 'wiki:1'}], 'physical_candidates': [{'candidate_id': 'wiki:1'}]})))
    assert result['result']['status'] == 'match' and len(calls) == 1


def test_later_search_keeps_real_nearest_address_hypotheses_without_full_verdicts(tmp_path):
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    nearby = [{'type': 'node', 'id': number, 'distance_m': metres, 'lat': 54.7, 'lon': 20.5,
        'tags': {'addr:street': 'Fixture Street', 'addr:housenumber': str(number)}}
        for number, metres in [(31, 24), (33, 39)]]
    with service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({'osm': {'nearby': nearby}}), sid))
    context = adapter.identity_search_context({'id': sid, 'photo_sha256': photo,
        '_identity_query_context': {'last_verdict': {'status': 'mismatch', 'observations': ['interior']},
            'recent_verdicts': ['unused detailed context' * 2000]}})
    assert [x['map_address']['house_number'] for x in context['nearby_address_hypotheses']] == ['31', '33']
    assert all(x['map_address']['scope'] == 'mapped_entry_only' for x in context['nearby_address_hypotheses'])
    assert context['last_verdict']['status'] == 'mismatch'
    assert 'recent_verdicts' not in context
    assert len(canonical(context)) < 6000


@pytest.mark.asyncio
async def test_search_capsule_fits_after_large_visual_history_and_retains_address_alternatives(tmp_path):
    import json
    from types import SimpleNamespace
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.search_history = lambda _: {'found_sources': [{'url': 'https://fixture.example/' + 'x' * 11500}]}
    captures = []
    async def search(capsule, binding):
        captures.append(capsule)
        return {'sources': []}
    adapter.client = SimpleNamespace(search_articles=search)
    async def invoke(story, role, unit, call):
        return await call({'purpose': 'identity'})
    adapter.run = invoke
    address = {'candidate_id': 'osm:node:31', 'distance_m': 24,
        'map_address': {'street': 'Fixture Street', 'house_number': '31', 'scope': 'mapped_entry_only'}}
    await adapter.search_articles('Fixture Street 31', {'id': sid, 'photo_sha256': photo,
        '_identity_search_context': {'nearby': [address]},
        '_identity_query_context': {'recent_verdicts': ['private long repeated DTO' * 2000],
            'last_verdict': {'status': 'uncertain', 'observations': ['reason' * 2000]}}})
    assert len(captures[0]) < 20000
    context = json.loads(captures[0])['visual_evidence_context']
    assert context['nearby_address_hypotheses'][0]['map_address']['house_number'] == '31'
    assert len(context['last_verdict']['observations'][0]) == 400


@pytest.mark.asyncio
async def test_completed_receipt_records_only_novel_current_owned_evidence(tmp_path):
    import json
    from street_story.research_budget import ensure_budget
    service, sid, photo = fixture(tmp_path)
    clock = [service.store.now()]
    service.store.now = lambda: clock[0]
    original = ensure_budget(service, sid, explicit=True)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    with service.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE semantic_key='control-research'").fetchone())
    story = {'id': sid, 'photo_sha256': photo, '_research_job_id': job['id'],
             '_research_job_attempt': job['attempts']}
    binding, _ = adapter.attempt(story, 'facts', 'evidence-unit')
    clock[0] += 10
    await adapter.checkpoint(binding, {'phase': 'submitted', 'result': {'partial': True}})
    assert ensure_budget(service, sid)['last_progress_at'] == original['started_at']
    await adapter.checkpoint(binding, {'phase': 'completed', 'result': {}})
    assert ensure_budget(service, sid)['last_progress_at'] == original['started_at']
    receipt = {'phase': 'completed', 'result': {'facts': ['source-supported evidence']}}
    await adapter.checkpoint(binding, receipt)
    saved = ensure_budget(service, sid)
    assert saved['last_progress_at'] == clock[0]
    assert saved['evidence_units'] == [binding['attempt_id']]
    assert saved['deadline_at'] == original['deadline_at']
    clock[0] += 10
    await adapter.checkpoint(binding, receipt)
    assert ensure_budget(service, sid) == saved
    stale, _ = adapter.attempt(story, 'facts', 'stale-owner-unit')
    with service.store.tx() as db:
        db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
    await adapter.checkpoint(stale, receipt)
    assert ensure_budget(service, sid) == saved
    with service.store.connection() as db:
        archived = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                                        (stale['attempt_id'],)).fetchone()[0])
    assert archived == receipt


@pytest.mark.asyncio
async def test_exact_pair_cap_does_not_block_completed_or_original_unknown_readback(tmp_path):
    from types import SimpleNamespace
    from street_story.research_budget import ResearchTerminated, ensure_budget, reserve_work
    service, sid, photo = fixture(tmp_path)
    ensure_budget(service, sid, explicit=True)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    with service.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE semantic_key='control-identity_visual'").fetchone())
    args = visual_args(None, {'id': sid, 'photo_sha256': photo,
        '_research_job_id': job['id'], '_research_job_attempt': job['attempts']}, {}, {
        'comparison_id': 'cap-fixture', 'references': [{'candidate_id': 'wiki:1', 'reference_id': 'new-ref'}]})
    _, story, schema, context = args
    reserve_work(service, sid, 'exact_pairs', [f'old-ref-{n}' for n in range(service.settings.identity_max_exact_pairs)])
    adapter.visual_pair_receipts = lambda *_: {}
    with pytest.raises(ResearchTerminated, match='identity_exact_pair_envelope_exhausted'):
        await adapter._visual_pair_route_owned('native', story, schema, context, 'unit')
    result = {'status': 'mismatch'}
    completed = {'phase': 'completed', 'result': result}
    adapter.visual_pair_receipts = lambda *_: {'vision_native': completed}
    assert (await adapter._visual_pair_route_owned('native', story, schema, context, 'unit'))['result'] == result
    original = {'binding': {'story_id': sid, 'visual_scope': True, 'generation': 0,
                 'purpose': 'identity', 'control_revision': 0}, 'phase': 'unknown',
                'thread_id': 'original-thread', 'turn_id': 'original-turn'}
    adapter.visual_pair_receipts = lambda *_: {'vision_native': original}
    async def readback(snapshot, owned, supplied_schema, supplied_context, binding):
        assert binding['thread_id'] == 'original-thread' and binding['turn_id'] == 'original-turn'
        return {'result': result, 'receipt': completed}
    adapter.native_vision = SimpleNamespace(compare_visual=readback)
    assert (await adapter._visual_pair_route_owned('native', story, schema, context, 'unit'))['result'] == result
    assert len(ensure_budget(service, sid)['work_units']['exact_pairs']) == service.settings.identity_max_exact_pairs


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['google', 'native', 'opencode'])
async def test_standalone_visual_cap_rejects_before_provider_dispatch(tmp_path, route):
    import json
    from types import SimpleNamespace
    from street_story.research_budget import ResearchTerminated, ensure_budget, reserve_work
    service, sid, photo = fixture(tmp_path)
    ensure_budget(service, sid, explicit=True)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter._active_binding = ContextVar('standalone-cap-binding', default=None)
    with service.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE semantic_key='control-identity_visual'").fetchone())
    args = visual_args(None, {'id': sid, 'photo_sha256': photo,
        '_research_job_id': job['id'], '_research_job_attempt': job['attempts']}, {}, {
        'comparison_id': 'standalone-cap-fixture',
        'references': [{'candidate_id': 'wiki:1', 'reference_id': 'new-standalone-ref'}]})
    reserve_work(service, sid, 'exact_pairs', [f'old-ref-{n}' for n in range(service.settings.identity_max_exact_pairs)])
    async def dispatch(*args):
        pytest.fail('Exact-reference envelope must reject before provider dispatch')
    adapter.primary_vision = SimpleNamespace(available=route == 'google', compare_visual=dispatch)
    adapter.native_vision = SimpleNamespace(available=route == 'native', compare_visual=dispatch)
    adapter.client = SimpleNamespace(endpoint='qualified-opencode', provider_id='opencode',
                                    model_id='qualified-model', compare_image=dispatch)
    service.store.cache_put('research-vision-verification-v1', {
        'model_id': adapter.client.model_id, 'endpoint': adapter.client.endpoint,
        'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}, ttl_seconds=3600)
    with pytest.raises(ResearchTerminated, match='identity_exact_pair_envelope_exhausted'):
        await adapter.visual_verdict(*args)
    with service.store.connection() as db:
        receipts = [json.loads(row[0]) for row in db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=?', (sid,))]
    assert all(receipt['phase'] in {'created', 'failed'} for receipt in receipts)
    assert len(ensure_budget(service, sid)['work_units']['exact_pairs']) == service.settings.identity_max_exact_pairs


@pytest.mark.asyncio
async def test_native_unsent_pair_wait_preserves_original_due_then_reuses_unit(tmp_path):
    from types import SimpleNamespace
    from street_story.errors import RetryableProviderError
    service, sid, photo = fixture(tmp_path)
    clock = [service.store.now()]
    service.store.now = lambda: clock[0]
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.client = None
    args = visual_args(None, {'id': sid, 'photo_sha256': photo}, {}, {
        'comparison_id': 'same-admission-unit', 'references': [{'candidate_id': 'wiki:1'}]})
    calls = []
    async def native(snapshot, story, schema, context, binding):
        calls.append(binding['attempt_id'])
        if len(calls) == 1:
            receipt = {'binding': binding, 'phase': 'created', 'provider_send_state': 'not_sent',
                       'retry_safe': True, 'route_failure': {'code': 'RESOURCE_NO_CAPACITY', 'retry_at': clock[0]+3}}
            await adapter.checkpoint(binding, receipt)
            raise RetryableProviderError('RESOURCE_NO_CAPACITY', retry_at=clock[0]+3)
        assert 'turn_id' not in binding and 'thread_id' not in binding
        return {'result': {'status': 'uncertain'}, 'receipt': {'phase': 'completed'}}
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    with pytest.raises(RetryableProviderError) as refused:
        await adapter.visual_pair_route('native', *args)
    assert refused.value.retry_at == clock[0]+3
    assert refused.value.provider_send_state == 'not_sent' and refused.value.retry_safe is True
    with pytest.raises(RetryableProviderError) as cooling:
        await adapter.visual_pair_route('native', *args)
    assert cooling.value.retry_at == refused.value.retry_at and len(calls) == 1
    clock[0] += 3
    assert (await adapter.visual_pair_route('native', *args))['result']['status'] == 'uncertain'
    assert calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('structured_original', [False, True])
@pytest.mark.parametrize('dedicated_wrapper', [False, True])
async def test_new_planner_schema_reads_old_unknown_unit_then_replays_closed_plan(tmp_path, structured_original, dedicated_wrapper):
    import hashlib
    from types import SimpleNamespace
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter._active_binding = ContextVar('planner-old-schema-binding', default=None)
    client = SimpleNamespace(endpoint='same-existing-opencode', provider_id='opencode', model_id='qualified-text')
    adapter.client = client
    # Losing availability/qualification cannot invent a replacement operation.
    adapter._fact_pool_routes = lambda: [{'provider_id': client.provider_id, 'model_id': client.model_id,
        'endpoint': client.endpoint, 'client': client, 'qualified': False, 'available': False}]
    old_prompt = 'Original address planning capsule'
    old_schema = {'type': 'object', 'properties': {'article_queries': {'type': 'array'}}, 'required': ['article_queries']}
    if structured_original:
        old_schema['properties']['first_wave_hypotheses'] = {'type': 'array'}
        old_schema['required'].append('first_wave_hypotheses')
    old_unit = canonical(['identity-search-plan-v1', old_prompt, old_schema])
    route_unit = canonical([old_unit, client.provider_id, client.model_id, client.endpoint])
    story = {'id': sid, 'photo_sha256': photo, '_fact_pool_unit_id': old_unit,
             '_fact_pool_input_sha256': hashlib.sha256(old_unit.encode()).hexdigest()}
    binding, _ = adapter.attempt(story, 'identity_search_plan', route_unit)
    receipt = {'binding': binding, 'phase': 'unknown', 'session_id': 'original-session',
        'message_id': 'original-message', 'provider_id': client.provider_id, 'model_id': client.model_id,
        'frozen_prompt': old_prompt, 'frozen_schema': old_schema}
    await adapter.checkpoint(binding, receipt)
    assert adapter.has_identity_search_plan_readback(story)
    calls = []
    async def read_original(role, prompt, current, schema):
        calls.append(current)
        assert role == 'facts' and prompt == old_prompt and schema == old_schema
        assert current['session_id'] == 'original-session' and current['message_id'] == 'original-message'
        assert current['attempt_id'] == binding['attempt_id'] and current['fact_unit_id'] == old_unit
        payload = {'article_queries': ['Original observed address']}
        if structured_original:
            payload['first_wave_hypotheses'] = [{'kind': 'address', 'subject_id': 'osm:node:1',
                'query': 'Original observed address', 'reason': 'Original observed address anchor'}]
        from jsonschema import Draft202012Validator
        Draft202012Validator(old_schema).validate(payload)
        closed = {**receipt, 'phase': 'completed', 'result': payload}
        await adapter.checkpoint(binding, closed)
        return {'result': closed['result'], 'receipt': closed}
    if dedicated_wrapper:
        async def planner(prompt, current, schema):
            return await read_original('facts', prompt, current, schema)
        client.plan_identity_search = planner
        async def forbidden_generic(*args):
            pytest.fail('Original planner must use its dedicated wrapper')
        client._run = forbidden_generic
    else:
        client._run = read_original
    new_schema = {**old_schema, 'required': ['article_queries', 'first_wave_hypotheses']}
    # A new text transport and a recreated service must read the original
    # addressed operation with its own exact frozen prompt and strict schema.
    from street_story.identity_discovery import identity_text_fallback_prompt
    restarted = object.__new__(ProductResearchAdapter)
    restarted.service = type(service)(service.settings, providers=service.providers)
    restarted.client = client
    restarted._active_binding = ContextVar('restarted-planner-binding', default=None)
    restarted._fact_pool_routes = adapter._fact_pool_routes
    adapter = restarted
    new_prompt = identity_text_fallback_prompt({'map_scene': None, 'literal_context': 'New received context'})
    answer = await adapter.plan_identity_search(story, new_prompt, new_schema)
    assert bool(answer.get('original_schema_readback')) is not structured_original
    assert answer['original_schema'] == old_schema
    assert answer['result']['article_queries'] == ['Original observed address']
    # Covers result readback -> durable plan persistence crash boundary.
    again = await adapter.plan_identity_search(story, new_prompt, new_schema)
    assert again == answer and len(calls) == 1
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM research_provider_attempts WHERE role='identity_search_plan'").fetchone()[0] == 1
    assert not adapter.has_identity_search_plan_readback({**story, '_identity_research_control_revision': 1})
