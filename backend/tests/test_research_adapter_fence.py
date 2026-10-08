from direct_visual_fixture import visual_args
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from street_story.research_adapter import ProductResearchAdapter
from street_story.research_control import resume_research, stop_research
from street_story.service import ConflictError
from test_research_control import fixture


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
