"""Regressions for scoped reuse, exact aliases and additive story projection."""
import json

import httpx
import pytest

from street_story.db import Store
from street_story.fact_conflicts import record_fact_review_scan
from street_story.fact_ledger import persist_fact_candidates, refresh_review_status
from street_story.poi_memory import (
    ensure_poi_identity, memory_keys, persist_research_memory, prior_facts, sync_poi_review_from_story,
)
from street_story.providers import GeminiClient
from street_story.research_runs import (
    begin_research_run, chunk_checkpoint, mark_chunk, persist_source_version, record_chunk_batch,
    run_manifest, source_coverage,
)
from test_backend import config
from test_facts_research_finish import URL, fallback
from test_live_editor import PHOTO, PHOTO_SHA, make_service, mark_identity_ready
from test_research_runs import create_story


def attach(db, run, text='An unchanged historical document. ' * 100):
    return persist_source_version(db, run_id=run, requested_url=URL, final_url=URL, title='History',
        content_type='text/html', http_status=200, redirect_chain=[], normalized_text=text,
        read_status='complete', now=2)


def begin(db, run, story, *, goal='All facts', poi='wiki:77'):
    return begin_research_run(db, story_id=story, poi_key=poi, goal=goal, expected_story_revision=0,
        identity_generation=0, run_id=run, now=1)


def complete(db, run, chunk, *, continuation=False):
    record_chunk_batch(db, run_id=run, chunk_id=chunk, batch_index=0,
        status='continuation' if continuation else 'completed', raw_fact_count=0, accepted_fact_count=0,
        continuation_needed=continuation, continuation_reason='', model_name='controlled',
        prompt_version='live-chunk-findings-v1', now=3,
        payload={'facts': [], 'no_claims': True, 'next_passage_cursor': 2 if continuation else 0,
                 'read_passage_ids': [0, 1]})
    mark_chunk(db, run_id=run, chunk_id=chunk, status='deferred' if continuation else 'no_claims',
        observation_count=0, model_name='controlled', prompt_version='live-chunk-findings-v1', now=3)


@pytest.mark.parametrize('change', ['none', 'scope', 'poi', 'version', 'missing_payload', 'corrupt_payload', 'policy', 'invalid_text', 'invalid_source'])
def test_reuse_requires_exact_version_poi_scope_and_valid_checkpoint(tmp_path, change):
    store = Store(tmp_path / 'reuse.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'old', story)
        old = attach(db, 'old')
        complete(db, 'old', old['chunks'][0]['chunk_id'])
        if change == 'missing_payload':
            db.execute("UPDATE research_chunk_batches SET payload_json='',payload_sha256='' WHERE run_id='old'")
        if change == 'corrupt_payload':
            db.execute("UPDATE research_chunk_batches SET payload_sha256='invalid' WHERE run_id='old'")
        if change == 'policy':
            db.execute("UPDATE research_chunk_runs SET prompt_version='obsolete' WHERE run_id='old'")
        if change == 'invalid_text':
            db.execute("UPDATE research_chunk_runs SET error_code='not_article_text' WHERE run_id='old'")
        if change == 'invalid_source':
            db.execute("UPDATE research_run_sources SET error_code='not_article_text',status='deferred' WHERE run_id='old'")
        begin(db, 'new', story, goal='A different question' if change == 'scope' else 'All facts',
              poi='wiki:other' if change == 'poi' else 'wiki:77')
        new = attach(db, 'new', 'Changed historical document. ' * 100) if change == 'version' else attach(db, 'new')
        checkpoint = chunk_checkpoint(db, 'new', new['chunks'][0]['chunk_id'])
        assert checkpoint['terminal'] is (change == 'none')
        assert run_manifest(db, 'new')['counts']['chunks_skipped_completed'] == (1 if change == 'none' else 0)
        assert db.execute('SELECT COUNT(*) FROM poi_research_observations').fetchone()[0] == 0


def test_partial_import_continues_at_saved_batch_and_page(tmp_path):
    store = Store(tmp_path / 'partial.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'old', story)
        old = attach(db, 'old')
        complete(db, 'old', old['chunks'][0]['chunk_id'], continuation=True)
        begin(db, 'new', story)
        new = attach(db, 'new')
        checkpoint = chunk_checkpoint(db, 'new', new['chunks'][0]['chunk_id'])
        assert not checkpoint['terminal']
        assert checkpoint['next_batch_index'] == 1 and checkpoint['passage_cursor'] == 2
        assert checkpoint['read_passage_ids'] == [0, 1]
        assert run_manifest(db, 'new')['counts']['chunks_resumed_partial'] == 1
        assert source_coverage(db, ['wiki:77'])[URL][0]['completed'] is False


def test_verified_cluster_shares_existing_memory_without_name_or_alternative_leak(tmp_path):
    store = Store(tmp_path / 'aliases.sqlite3')
    a = {'candidate_id': 'wiki:77', 'candidate_name': 'Gate'}
    b = {'candidate_id': 'commonscat:gate', 'candidate_name': 'Sackheim gate'}
    unrelated = {'candidate_id': 'wiki:other', 'candidate_name': 'Gate'}
    source = {'url': URL, 'supports': [{'kind': 'search_snippet', 'source_url': URL, 'text': 'Source fact.'}]}
    with store.tx() as db:
        for identity, fact_id in ((a, 'first'), (b, 'second'), (unrelated, 'other')):
            persist_research_memory(db, identity, [{'fact_id': fact_id, 'claim_key': fact_id, 'text': 'Source fact.',
                'confidence': .9, 'sources': [source]}], [source], 'all', 1)
        assert memory_keys(db, a) == ['wiki:77']
        uncertain = {**a, 'status': 'uncertain', 'alternative_candidate_ids': ['commonscat:gate']}
        ensure_poi_identity(db, uncertain, now=2)
        assert memory_keys(db, a) == ['wiki:77']
        verified = {**a, 'status': 'match', 'visual_reference_verified': True,
                    'candidates': [{**a, 'discovery': 'wikimedia_entity_cluster', 'alias_candidate_ids': ['commonscat:gate']}]}
        ensure_poi_identity(db, verified, now=3)
        assert set(memory_keys(db, a)) == {'wiki:77', 'commonscat:gate'}
        assert {f['fact_id'] for f in prior_facts(db, a, 'unused')} == {'first', 'second'}
        assert {f['fact_id'] for f in prior_facts(db, b, 'unused')} == {'first', 'second'}
        assert {f['fact_id'] for f in prior_facts(db, unrelated, 'unused')} == {'other'}
        assert db.execute('SELECT COUNT(*) FROM poi_research_assertions').fetchone()[0] == 3


def test_nonempty_story_gets_all_new_eligible_memory_without_selection_or_draft_reset(tmp_path):
    svc, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    with svc.store.connection() as db:
        identity = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (session.resource_id,)).fetchone()[0])['visual_identity']

    def seed(numbers):
        facts = [{'fact_id': f'claim-{n}', 'claim_key': f'key-{n}', 'text': f'Literal controlled fact {n}.', 'confidence': .9,
                  'sources': [{'url': URL, 'supports': [{'kind': 'page_excerpt', 'source_url': URL, 'text': f'Literal controlled fact {n}.'}]}]}
                 for n in numbers]
        with svc.store.tx() as db:
            ids = persist_fact_candidates(db, story_id=session.resource_id, poi_key='wiki:77', facts=facts,
                run_id='seed', batch_id='seed-' + str(numbers.start), model_name='controlled', prompt_version='controlled', now=svc.store.now())
            for fact, assertion_id in zip(facts, ids, strict=True):
                fact['fact_id'] = assertion_id
            persist_research_memory(db, identity, facts, [], 'All facts', svc.store.now())
            bundle = dict(db.execute('SELECT assertion_id,revision_digest FROM fact_assertions WHERE story_id=?', (session.resource_id,)))
            record_fact_review_scan(svc, session.resource_id, 'wiki:77', detector='controlled', run_id='seed',
                revision_bundle=bundle, conflict_ids=[], coverage_complete=True, missing_aspects=[], connection=db)
            refresh_review_status(db, session.resource_id, svc.store.now())
            sync_poi_review_from_story(db, session.resource_id, svc.store.now())
        return ids

    first_id = seed(range(1))[0]
    target = svc.create_story(key='target', client_story_id='target', photo_sha256=PHOTO_SHA, photo_mime_type='image/jpeg',
        photo_bytes=PHOTO, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)['id']
    mark_identity_ready(svc, target)
    assert len(svc.story(target)['facts']) == 1
    adapter._select_facts(target, 'choose', {'fact_ids': [first_id]})
    adapter._edit_text(target, 'draft', {'expected_text_revision': 0, 'new_text': 'Literal controlled fact 0.', 'change_summary': 'Owner draft.'})
    before = svc.story(target)
    seed(range(1, 72))
    after = svc.story(target)
    assert len(after['facts']) == 72
    assert all(f['eligibility'] == 'eligible' for f in after['facts'])
    assert [f['fact_id'] for f in after['facts'] if f['selected']] == [first_id]
    assert after['draft_text'] == before['draft_text']
    assert svc.story(target)['revision'] == after['revision']
    first = adapter._get_facts(target, {'limit': 50})
    second = adapter._get_facts(target, {'limit': 50, 'cursor': first['next_cursor']})
    assert len(first['facts']) + len(second['facts']) == 72 and not second['has_more']


@pytest.mark.asyncio
async def test_completed_run_does_not_deliver_old_source_to_semantic_extractor(tmp_path):
    svc, adapter, session, _, old_run, _, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    first = await adapter._get_research_chunk(session, {'run_id': old_run})
    await adapter._save_research_facts(session, 'old-page', {'facts': [], 'batch_reviewed': True, 'source_matches_poi': True})
    assert run_manifest_read(svc, old_run)['run']['state'] == 'completed'
    new = await adapter._search_web(session, 'new-search', {'query': 'Исследуй объект без подсказок ответа.'})
    result = await adapter._get_research_chunk(session, {'run_id': new['research_run_id']})
    assert result['all_chunks_processed'] and 'evidence_passages' not in result
    manifest = run_manifest_read(svc, new['research_run_id'])
    assert manifest['counts']['chunks_skipped_completed'] == 1
    assert first['source_version_id'] == manifest['sources'][0]['source_version_id']
    assert adapter._get_facts(session.resource_id, {})['facts'] == []
    await reader.search_http.aclose()


def run_manifest_read(svc, run):
    with svc.store.connection() as db:
        return run_manifest(db, run)


@pytest.mark.asyncio
async def test_live_resume_uses_saved_page_and_menu_is_not_completed_article(tmp_path):
    svc, adapter, session, _, run, _, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={'content-type': 'text/html'}, text='<main>' + 'Navigation item. ' * 300 + '</main>')))
    first = await adapter._get_research_chunk(session, {'run_id': run})
    await adapter._save_research_facts(session, 'partial-page', {'facts': [], 'batch_reviewed': True, 'source_matches_poi': True})
    session.state['research_passages_seen'] = {}
    session.state['research_pending_page'] = {}
    resumed = await adapter._get_research_chunk(session, {'run_id': run})
    assert min(p['passage_id'] for p in resumed['evidence_passages']) > max(p['passage_id'] for p in first['evidence_passages'])
    await adapter._save_research_facts(session, 'invalid-page', {'facts': [], 'batch_reviewed': True,
        'source_matches_poi': True, 'source_content_valid': False})
    with svc.store.connection() as db:
        coverage = source_coverage(db, ['wiki:77'])
        assert not coverage[URL][0]['completed']
        assert db.execute('SELECT COUNT(*) FROM poi_research_assertions').fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_current_discovered_url_addresses_version_first_saved_under_redirect(tmp_path):
    svc, adapter, session, _, run, _, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    await reader.search_http.aclose()
    body = 'Literal historical account. ' * 10
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={'content-type': 'text/html'}, text='<main>' + body + '</main>')))
    with svc.store.tx() as db:
        begin(db, 'older-redirect', session.resource_id)
        persist_source_version(db, run_id='older-redirect', requested_url='https://redirect.example/history',
            final_url=URL, title='History', content_type='text/html', http_status=200, redirect_chain=[],
            normalized_text=body.strip(), read_status='complete', now=svc.store.now())
    page = await adapter._get_research_chunk(session, {'run_id': run, 'source_url': URL})
    assert page['source_url'] == URL and page['evidence_passages']
    with svc.store.connection() as db:
        stored = db.execute('SELECT requested_url FROM source_versions WHERE source_version_id=?',
                            (page['source_version_id'],)).fetchone()
        assert stored['requested_url'] == 'https://redirect.example/history'
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_live_first_search_skips_only_fresh_completed_scope_and_keeps_snippets(tmp_path):
    store = Store(tmp_path / 'search.sqlite3')
    client = GeminiClient(config(tmp_path), store)
    body = ''.join(f'<div class="result"><a class="result__a" href="{url}">{title}</a>'
                   '<a class="result__snippet">Source discovery text.</a></div>'
                   for url, title in ((URL, 'Done'), ('https://new.example/article', 'New')))
    client.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
    context = {'live_first': True, 'coverage_goal': 'All facts', 'previously_processed_sources': [
        {'url': URL, 'supports': [], 'extraction_coverage': [{'scope': 'all facts', 'completed': True, 'checked_at': store.now()}]},
        {'url': 'https://new.example/article', 'supports': [{'text': 'Snippet only.'}]}]}
    result = await client.search_web('more facts', context)
    assert [s['url'] for s in result.grounding_sources] == ['https://new.example/article']
    assert result.payload['sources_skipped_completed'] == 1
    different = await client.search_web('verify a disputed claim', {**context, 'coverage_goal': 'Verify restoration cost'})
    assert len(different.grounding_sources) == 2
    context['previously_processed_sources'][0]['extraction_coverage'][0]['checked_at'] -= 86401
    stale = await client.search_web('more facts', context)
    assert len(stale.grounding_sources) == 2
    await client.search_http.aclose()
