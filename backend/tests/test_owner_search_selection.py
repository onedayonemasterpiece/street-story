"""Owner exact searches select reading URLs without rewriting original sends."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.headless_facts import HeadlessFacts
from test_opencode_research import Harness
from test_owner_fact_routes import prepared


CONTRACT = 'owner-exact-selected-urls-v1'


def exhausted():
    harness = Harness()
    harness.result = None
    harness.response_text = 'Maximum steps for this agent have been reached. No source selected.'
    original = harness.messages
    def messages():
        result = original()
        if len(result) > 1:
            result[1]['info']['finish'] = 'stop'
            result[1]['parts'][0]['state']['output'] = ''.join(
                f'Title: Nearby listing {n}\nURL: https://example.org/noise-{n}\nText: rental\n'
                for n in range(14))
        return result
    harness.messages = messages
    return harness


@pytest.mark.asyncio
async def test_max_steps_closed_owner_search_keeps_inventory_without_reading_authority():
    harness = exhausted()
    result = await harness.adapter().search_articles('Exact building', {
        'request_id': 'owner', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    assert result['sources'] == [] and len(result['receipt']['discovered_sources']) == 14
    assert result['receipt']['phase'] == 'completed'
    assert result['receipt']['provider_send_state'] == 'response_closed'
    assert result['receipt']['response_finish'] == 'stop'
    assert result['receipt']['source_selection']['reason'] == 'final_selection_json_unavailable'
    assert len(harness.sends) == 1
    prompt = harness.parts[0]['text']
    assert 'ONE websearch round' in prompt and 'at most two queries' in prompt
    assert 'Reserve the next assistant turn for final JSON' in prompt
    assert 'no source-count cap' in prompt


@pytest.mark.asyncio
async def test_owner_final_selection_keeps_selected_observed_urls_only():
    harness = Harness()
    harness.result = {'summary': 'Article about the exact building', 'selected_sources': [
        {'url': 'https://example.org/gallery', 'reason': 'Substantive building history'},
        {'url': 'https://invented.example/fake', 'reason': 'Unobserved'}]}
    result = await harness.adapter().search_articles('Exact building', {
        'request_id': 'owner-positive', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    assert [source['url'] for source in result['sources']] == ['https://example.org/gallery']
    assert result['receipt']['source_selection']['unobserved_count'] == 1
    assert result['receipt']['source_selection']['reason'] == 'Article about the exact building'


@pytest.mark.asyncio
async def test_owner_selection_has_no_read_source_count_cap():
    harness = exhausted()
    harness.result = {'summary': 'Four relevant building articles', 'selected_sources': [
        {'url': f'https://example.org/noise-{n}', 'reason': 'Building-specific history'} for n in range(4)]}
    result = await harness.adapter().search_articles('Exact building', {
        'request_id': 'owner-many', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    assert len(result['sources']) == 4 and len(result['receipt']['discovered_sources']) == 14


@pytest.mark.asyncio
async def test_unknown_original_search_replays_frozen_legacy_prompt_without_resend():
    harness = exhausted()
    client = harness.adapter()
    original = await client.search_articles('Legacy object', {'request_id': 'legacy', 'purpose': 'facts'})
    receipt = original['receipt']
    binding = {**receipt['binding'], **{key: receipt[key] for key in
        ('session_id', 'message_id', 'frozen_prompt', 'frozen_schema')}, 'phase': 'unknown'}
    result = await client.search_articles('Changed current owner capsule', binding)
    assert result['receipt']['frozen_prompt'] == receipt['frozen_prompt']
    assert result['receipt']['message_id'] == receipt['message_id']
    assert result['receipt']['source_selection']['status'] == 'legacy_discovery'
    assert len(result['sources']) == 14 and len(harness.sends) == 1


@pytest.mark.parametrize('phase', ['created', 'unknown', 'completed'])
def test_original_logical_search_binding_does_not_acquire_new_contract(tmp_path, phase):
    adapter, _, _, story, _ = prepared(tmp_path, owner=False)
    binding, _ = adapter.attempt(story, 'search', 'same query/run')
    saved = {'binding': binding, 'phase': phase, 'result': {'summary': 'Original'},
             'sources': [{'url': 'https://example.org/original'}]}
    if phase == 'unknown':
        saved.update(session_id='sesOriginal', message_id='msgOriginal', frozen_prompt='Original prompt')
    with adapter.service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                   (json.dumps(saved), binding['attempt_id']))
    story['research_json'] = json.dumps({'visual_identity': {
        'status': 'owner_confirmed', 'selection_basis': 'owner_exact'}})
    resumed, complete = adapter.attempt(story, 'search', 'same query/run')
    assert 'search_selection_contract' not in (resumed or complete['binding'])
    if phase == 'unknown':
        assert resumed['message_id'] == 'msgOriginal' and resumed['frozen_prompt'] == 'Original prompt'
    if phase == 'completed':
        assert complete == saved


def test_new_owner_search_flag_is_persisted_and_reuses_same_logical_attempt(tmp_path):
    adapter, _, _, story, _ = prepared(tmp_path)
    binding, _ = adapter.attempt(story, 'search', 'new query/run')
    repeated, _ = adapter.attempt(story, 'search', 'new query/run')
    assert binding['search_selection_contract'] == CONTRACT
    assert repeated == {**binding, 'phase': 'created'}
    with adapter.service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback_status', ['completed_empty', 'selection_unavailable'])
async def test_owner_unselected_inventory_keeps_unresolved_status_after_empty_fallback(tmp_path, fallback_status):
    adapter, _, _, story, _ = prepared(tmp_path)
    harness = exhausted()
    independent = await harness.adapter().search_articles('Exact building', {
        'request_id': 'owner-empty', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    calls = []
    async def search(query, current):
        calls.append('primary')
        return copy.deepcopy(independent)
    async def google(query):
        calls.append('google')
        return SimpleNamespace(grounding_sources=[], payload={'status': fallback_status})
    adapter.search_articles = search
    adapter.service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    first = await adapter.search_fact_articles('Exact building', story)
    assert first['outcome'] == 'selection_unavailable' and first['sources'] == []
    assert len(first['discovered_sources']) == 14
    assert first['receipt']['independent_search']['source_selection']['reason'] == 'final_selection_json_unavailable'
    assert await adapter.search_fact_articles('Exact building', story) == first
    assert calls == ['primary', 'google']


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_valid_empty', [False, True])
async def test_known_optional_fallback_failure_is_closed_once_not_retried(tmp_path, primary_valid_empty):
    from street_story.gemini import GeminiUnavailable
    adapter, _, _, story, _ = prepared(tmp_path)
    harness = exhausted()
    if primary_valid_empty:
        harness.result = {'summary': 'No suitable article', 'selected_sources': []}
    independent = await harness.adapter().search_articles('Exact building', {
        'request_id': 'optional-failed', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    calls = []
    async def search(query, current):
        calls.append('primary')
        return copy.deepcopy(independent)
    async def google(query):
        calls.append('google')
        raise GeminiUnavailable(123, 'article_url_discovery_unavailable')
    adapter.search_articles = search
    adapter.service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    first = await adapter.search_fact_articles('Exact building', story)
    assert first['outcome'] == 'selection_unavailable'
    assert first['receipt']['reason'] == 'optional_search_fallback_unavailable'
    assert first['receipt']['route_failures']['google'] == 'gemini:article_url_discovery_unavailable'
    assert await adapter.search_fact_articles('Exact building', story) == first
    assert calls == ['primary', 'google']


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['research_attempt_unknown', 'research_attempt_binding_changed'])
async def test_optional_fallback_unknown_is_not_closed_as_selection_unavailable(tmp_path, code):
    from street_story.errors import RetryableProviderError
    adapter, _, _, story, _ = prepared(tmp_path)
    harness = exhausted()
    independent = await harness.adapter().search_articles('Exact building', {
        'request_id': 'optional-unknown', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    async def search(query, current):
        return copy.deepcopy(independent)
    async def google(query):
        raise RetryableProviderError(code)
    adapter.search_articles = search
    adapter.service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    with pytest.raises(RetryableProviderError, match=code):
        await adapter.search_fact_articles('Exact building', story)


@pytest.mark.asyncio
async def test_positive_selected_fallback_resolves_missing_primary_selection(tmp_path):
    adapter, _, _, story, _ = prepared(tmp_path)
    harness = exhausted()
    independent = await harness.adapter().search_articles('Exact building', {
        'request_id': 'positive-fallback', 'purpose': 'facts', 'search_selection_contract': CONTRACT})
    async def search(query, current):
        return copy.deepcopy(independent)
    async def google(query):
        return SimpleNamespace(grounding_sources=[{'url': 'https://example.org/selected-building'}],
                               payload={'status': 'completed'})
    adapter.search_articles = search
    adapter.service.providers = SimpleNamespace(gemini=SimpleNamespace(discover_article_urls=google))
    result = await adapter.search_fact_articles('Exact building', story)
    assert result['outcome'] == 'completed' and len(result['sources']) == 1


@pytest.mark.asyncio
async def test_owner_valid_selected_empty_queries_can_finish_search_exhausted(tmp_path):
    from test_headless_facts import fixture
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, job['story_id'])
        research = json.loads(row['research_json'])
        research['visual_identity'].update(status='owner_confirmed', selection_basis='owner_exact')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
    calls = []
    async def empty(query, story):
        calls.append(query)
        return {'sources': [], 'outcome': 'completed_empty', 'receipt': {
            'source_selection': {'status': 'model_selected', 'selected_count': 0}}}
    researcher.search_articles = empty
    try:
        result = await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert result['reason'] == 'search_exhausted' and result['outcome'] == 'no_supported_facts'
        assert len(calls) == 2 and not researcher.pages
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('second_valid_empty', [False, True])
async def test_exhausted_owner_inventory_finishes_partial_after_bounded_continuation(tmp_path, second_valid_empty):
    from test_headless_facts import fixture
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, job['story_id'])
        research = json.loads(row['research_json'])
        research['visual_identity'].update(status='owner_confirmed', selection_basis='owner_exact')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
    receipts = []
    async def empty(query, story):
        harness = exhausted()
        if receipts and second_valid_empty:
            harness.result = {'summary': 'No suitable building article found', 'selected_sources': []}
        result = await harness.adapter().search_articles(query, {
            'request_id': hashlib.sha256(query.encode()).hexdigest(), 'purpose': 'facts',
            'search_selection_contract': CONTRACT})
        receipts.append(result['receipt'])
        return {**result, 'outcome': 'completed_empty' if second_valid_empty and len(receipts) == 2
                else 'selection_unavailable'}
    researcher.search_articles = empty
    try:
        runner = HeadlessFacts(svc)
        outcome = await runner.run(job, 'headless-run', 'Find historical facts', 'history')
        assert outcome == {'outcome': 'resource_blocked', 'reason': 'source_selection_unavailable',
                           'coverage_complete': False, 'eligible_count': 0}
        assert len(receipts) == 2 and all(len(receipt['discovered_sources']) == 14 for receipt in receipts)
        assert not researcher.pages
        assert await runner.run(job, 'headless-run', 'Find historical facts', 'history') == outcome
        assert len(receipts) == 2
        from street_story.research_budget import finish_attempt
        from street_story.research_runs import run_manifest
        with svc.store.tx() as db:
            assert run_manifest(db, 'headless-run')['run']['state'] == 'partial'
            assert finish_attempt(svc, db, job['story_id'], outcome=outcome['outcome'],
                                  reason=outcome['reason'], job=job, coverage_complete=False)
            assert run_manifest(db, 'headless-run')['run']['state'] == 'partial'
            research = json.loads(svc._story_row(db, job['story_id'])['research_json'])
            assert research['automatic_research_outcome']['outcome'] == 'resource_blocked'
            assert research['automatic_research_outcome']['reason'] == 'source_selection_unavailable'
            assert 'не завершено' in research['identity_progress']['steps'][-1]['label']
            assert research['live_web_searches'][-1]['query_attempts'][0]['outcome'] == 'selection_unavailable'
            assert db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'done'
    finally:
        await reader.search_http.aclose()
