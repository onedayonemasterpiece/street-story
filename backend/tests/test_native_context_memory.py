import copy
import json
from types import SimpleNamespace

import pytest

from street_story.native_context_pages import memory_page, prompt_context
from street_story.native_source_memory import source_memory
from street_story.native_source_reads import source_page
from street_story.research_runs import persist_source_version
from test_headless_facts import URL, fixture


def test_all_large_history_pages_are_recoverable_and_bound_to_frozen_content():
    original = {'prior_search_leads': [{'url': f'https://example.org/{n}', 'title': 'Archive ' * 40}
        for n in range(600)], 'prior_search_queries': [], 'known': [{'text': 'Preserve this claim'}]}
    untouched = copy.deepcopy(original)
    context, memory = prompt_context(original)
    assert context['known'] == original['known'] and context['prior_search_leads']['item_count'] == 600
    assert len(str(context)) < 1000
    items, cursor = [], None
    while True:
        page = memory_page(memory, 'prior_search_leads', cursor)
        items.extend(page['items'])
        if not page['has_more']:
            break
        cursor = page['next_cursor']
    assert items == original['prior_search_leads'] and original == untouched
    changed = copy.deepcopy(memory)
    changed['prior_search_leads']['items'][0]['url'] += '/changed'
    with pytest.raises(ValueError, match='integrity_invalid'):
        memory_page(changed, 'prior_search_leads')
    foreign = prompt_context({'prior_search_leads': original['prior_search_leads'] + [{'url': 'foreign'}]})[1]
    with pytest.raises(ValueError, match='cursor_invalid'):
        memory_page(foreign, 'prior_search_leads', cursor)


@pytest.mark.asyncio
async def test_native_callback_reads_exact_frozen_history_without_network_or_source_proof(tmp_path):
    from street_story.fact_agent_providers import AgentRoute, run_tool_agent
    service, _, _, reader, _ = await fixture(tmp_path)
    history = [{'title': 'History ' * 40, 'url': f'https://example.org/{n}'} for n in range(400)]
    receipts = []
    class Native:
        async def run(self, **kwargs):
            wire = json.loads(kwargs['prompt'].split('\nINPUT=', 1)[1])
            assert wire['prior_search_leads']['item_count'] == 400
            cursor, items = None, []
            while True:
                args = {'section': 'prior_search_leads', **({'cursor': cursor} if cursor else {})}
                page = await kwargs['callback']('read_research_memory', args)
                assert page['evidence_role'] == 'prior_discovery_context_not_evidence'
                items.extend(page['items'])
                if not page['has_more']:
                    break
                cursor = page['next_cursor']
            assert items == history and kwargs['receipt']['sources'] == []
            kwargs['receipt'].update(phase='completed', provider_send_state='response_closed')
            return {'text': '{"facts":[]}', 'receipt': kwargs['receipt']}
    adapter = SimpleNamespace(service=service,
        _checkpoint_receipt=lambda binding, receipt: receipts.append(copy.deepcopy(receipt)))
    try:
        await run_tool_agent(adapter, AgentRoute('codex_native', 'gpt-6-luna', 'native', Native()),
            {}, {'run_id': 'headless-run', 'prior_search_leads': history},
            binding={'attempt_id': 'memory'}, guard=lambda: None)
        assert receipts[-1]['frozen_research_memory']['prior_search_leads']['items'] == history
        assert all(t['name'] == 'read_research_memory' for t in receipts[-1]['tool_receipts'])
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name', ['webfetch', 'retain_native_source'])
async def test_source_memory_counts_inherited_calls_once_and_exposes_actual_unread_chunks(tmp_path, tool_name):
    service, _, _, reader, _ = await fixture(tmp_path)
    try:
        with service.store.tx() as db:
            version = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='Archive', content_type='text/plain', http_status=200, redirect_chain=[],
                normalized_text='Actual archive paragraph. ' * 2500, read_status='complete',
                access_scope='public', now=service.store.now())
            first = source_page(db, run_id='headless-run', url=URL,
                version=version['source_version_id'], byte_budget=1)
            assert first['has_more']
            call = {'phase': 'completed', 'input': {'thread_id': 'own', 'name': tool_name,
                'arguments': {'url': URL}}, 'result': {'documents': [first]}}
            pending = {**call, 'phase': 'submitted'}
            receipts = [{'native_tool_calls': {'one': call, 'unknown': pending}},
                {'native_tool_calls': {'one': call}}]
            known = [{'fact_id': 'claim', 'sources': [{'supports': [{'source_version_id': version['source_version_id']}]}]}]
            memory = source_memory(db, receipts, known)
            source = memory['sources'][0]
            assert source['model_read_requests'] == source['initial_read_requests'] == 1
            if tool_name == 'retain_native_source':
                assert source['native_retention_requests'] == 1
                assert source['native_read_scope'] == 'actual_received_excerpt_not_full_publication'
            assert source['delivery_state'] == 'partial' and source['acquisition_complete']
            assert source['extracted_claim_ids'] == ['claim']
            next_page = source_page(db, run_id='headless-run', url=URL, cursor=source['next_unread_cursor'])
            assert not {c['chunk_id'] for c in first['chunks']} & {c['chunk_id'] for c in next_page['chunks']}
            assert source['network_refetches'] == source['cache_hits'] == 'not_recorded'
            assert source['question_exhausted'] == 'not_recorded'
    finally:
        await reader.search_http.aclose()
