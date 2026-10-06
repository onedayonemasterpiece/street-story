"""Real prompt construction and durable frozen-unit retry deadlines."""
import copy
import json
from types import SimpleNamespace

import pytest
from street_story.errors import RetryableProviderError
from street_story.headless_facts import HeadlessFacts
from street_story.opencode_research import OpenCodeResearch, ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter, fact_page_capsule
from test_gigachat_dispatch_checkpoint import PAGE
from test_headless_fact_pool import RUN
from test_headless_fact_pool import fixture as frozen_fixture
from test_verified_fact_pool import setup


def large_context():
    claim = 'В 2027 году планируют открыть новую выставку, если реконструкция будет завершена. '
    facts = [{'fact_id': f'known-{i}', 'text': claim + f'Проект номер {i}.',
              'qualifiers': ['запланировано', 'если реконструкция будет завершена'],
              'eligibility': 'eligible', 'sources': [{'text': 'repeated ledger DTO' * 100}]}
             for i in range(60)]
    return {'confirmed_identity': {'status': 'match', 'candidate_id': 'wiki:77', 'candidate_name': 'Gate'},
            'coverage_goal': 'Check the source', 'known_facts': facts[:50], '_known_fact_inventory': facts,
            'known_inventory_total': 60, 'prior_poi_facts': facts,
            'previously_processed_sources': [
                {'url': f'https://archive.example/source-{i}', 'title': f'History {i}',
                 'extraction_coverage': [{'extraction_scope': 'previous aspect ' * 20,
                                          'review_status': 'completed'} for _ in range(1 if i < 32 else 0)]}
                for i in range(37)], 'previously_processed_sources_omitted_count': 5}


@pytest.mark.asyncio
async def test_both_opencode_routes_use_bounded_hints_without_truncating_evidence_or_qualifiers(tmp_path):
    adapter, extra, story, entries = setup(tmp_path)
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': entries[1:]}, ttl_seconds=3600)
    context = large_context()
    frozen = copy.deepcopy(context)
    page = {**PAGE, 'evidence_passages': [{'passage_id': 9, 'text': 'До 2027 года это только план, а не открытая выставка. ' * 40}],
            'source_title': 'Exact SOURCE title'}
    prompts, capsules = [], []
    for client in (adapter.client, extra):
        original = client.extract_facts

        async def transport(role, prompt, binding, schema, original=original):
            assert role == 'facts' and len(prompt) <= 24000
            prompts.append(prompt)
            content = json.loads(prompt.split('Capsule:\n', 1)[1])
            capsules.append(content)
            return await original(content, binding)

        client._run = transport
        # Construct the exact production OpenCode prompt, without a model send.
        client.extract_facts = lambda capsule, binding, client=client: OpenCodeResearch.extract_facts(client, capsule, binding)
    for ordinal in (0, 1):
        await adapter.extract_fact_page({**page, '_unit_id': f'bounded-{ordinal}', '_extractor_ordinal': ordinal}, story, context)
    assert len(prompts) == len(capsules) == 2
    assert context == frozen
    for capsule in capsules:
        assert capsule['sources'] == [{'source_version_id': page['source_version_id'], 'url': page['source_url'],
                                      'title': page['source_title'], 'passages': page['evidence_passages']}]
        hints = capsule['known_fact_inventory']
        assert 0 < len(hints) < 60
        assert capsule['context']['known_inventory_complete'] is False
        assert capsule['context']['known_inventory_total'] == 60
        assert capsule['context']['known_inventory_omitted_count'] == 60 - len(hints)
        assert capsule['context']['previously_processed_sources_total'] == 42
        assert capsule['context']['previously_processed_sources_omitted_count'] == 42 - len(capsule['context']['previously_processed_sources'])
        for hint in hints:
            actual = next(fact for fact in frozen['_known_fact_inventory'] if fact['fact_id'] == hint['fact_id'])
            assert hint['text'] == actual['text'] and hint['qualifiers'] == actual['qualifiers']
        assert '_known_fact_inventory' not in capsule
        assert all('extraction_coverage' not in item for item in capsule['context']['previously_processed_sources'])


def test_capsule_omits_whole_oversized_metadata_and_reports_unseen_inventory():
    context = large_context()
    context['_known_fact_inventory'][0]['text'] = 'Planned, contingent on approval. ' * 2000
    context['previously_processed_sources'][0]['title'] = 'large metadata' * 2000
    context['known_inventory_total'] = 65
    capsule = fact_page_capsule(PAGE, context)
    assert capsule['_known_fact_inventory'][0]['text'] == context['_known_fact_inventory'][0]['text']
    assert capsule['known_fact_inventory'] == []  # No chopped claim loses its conditions.
    assert capsule['context']['known_inventory_complete'] is False
    assert capsule['context']['known_inventory_omitted_count'] == 65
    assert all(item['url'] != context['previously_processed_sources'][0]['url'] for item in capsule['context']['previously_processed_sources'])
    assert capsule['sources'][0]['passages'] == PAGE['evidence_passages']


@pytest.mark.asyncio
async def test_route_pool_uses_earliest_existing_provider_deadline_and_capacity_cache(tmp_path):
    adapter, extra, story, entries = setup(tmp_path)
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': entries[1:]}, ttl_seconds=3600)
    now = adapter.service.store.now()
    calls = []
    for client, delay in ((adapter.client, 600), (extra, 1200)):
        async def refused(capsule, binding, client=client, delay=delay):
            calls.append(client.model_id)
            receipt = {'binding': binding, 'phase': 'failed', 'model_id': client.model_id,
                       'provider_send_state': 'not_sent', 'retry_safe': True,
                       'provider_status': 429, 'provider_retry_after': delay}
            await adapter.checkpoint(binding, receipt)
            raise ResearchUnavailable('research_provider_failed', receipt)
        client.extract_facts = refused
    with pytest.raises(RetryableProviderError) as error:
        await adapter.extract_fact_page({**PAGE, '_extractor_ordinal': 0}, story, {'coverage_goal': 'Read'})
    assert now + 599 <= error.value.retry_at <= now + 601
    for unit in (PAGE['_unit_id'], 'independent-later-unit'):
        with pytest.raises(RetryableProviderError, match='pool_waiting') as replay:
            await adapter.extract_fact_page({**PAGE, '_unit_id': unit}, story, {'coverage_goal': 'Read'})
        assert replay.value.retry_at == error.value.retry_at
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [3, 5])
async def test_all_failed_frozen_units_preserve_cooldown_not_ten_second_unvisited_loop(tmp_path, count):
    svc, job = frozen_fixture(tmp_path, count=count)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = svc
    now = svc.store.now()
    due = now + 900
    calls = []

    async def refused(page, story, context):
        calls.append(page['_unit_id'])
        binding, _ = adapter.attempt(story, 'facts', page['_unit_id'])
        await adapter.checkpoint(binding, {'binding': binding, 'phase': 'failed',
            'provider_send_state': 'not_sent', 'retry_safe': True,
            'route_failure': {'code': 'research_fact_pool_waiting', 'retry_at': due}})
        raise RetryableProviderError('research_fact_pool_waiting', retry_at=due)

    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=refused)
    with pytest.raises(RetryableProviderError) as error:
        await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
    assert error.value.retry_at == due and len(calls) == 3
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM facts WHERE story_id=?', (job['story_id'],)).fetchone()[0] == 0
        phases = [json.loads(row[0]) for row in db.execute("SELECT value_json FROM research_checkpoints WHERE stage LIKE 'headless_fact_unit:%'")]
        assert len(phases) == 3 and all(phase['retry_at'] == due and phase['phase'] == 'closed_error' for phase in phases)
    if count == 3:
        with pytest.raises(RetryableProviderError) as replay:
            await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
        assert replay.value.retry_at == due
        assert len(calls) == 3  # Durable deadline survives a new intake object.
