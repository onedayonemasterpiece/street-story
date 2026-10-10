"""Explicit Wiki choices become acquired TEXT in one existing joint followup."""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story import identity_discovery
from street_story.identity_architectural_context import acquire_selected_wikipedia_text
from street_story.providers import GeminiClient, _read_article_text
from test_architectural_text_identity import TEXT, text_inputs
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload
from test_prussia39 import Cache

URL = 'https://ru.wikipedia.org/wiki/Observed_physical_structure'
RAW = ('<html><head><meta charset="utf-8"></head><body><nav>Not an article.</nav>'
       '<div class="mw-parser-output"><p>' + TEXT + '</p></div></body></html>').encode()
NORMALIZED = _read_article_text(RAW.decode())[0]


def prime(store, *, raw=RAW, stored_hash=None):
    store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(URL.encode()).hexdigest(),
        {'final_url': URL, 'mime': 'text/html', 'body': base64.b64encode(raw).decode(),
         'sha256': stored_hash or hashlib.sha256(raw).hexdigest(), 'acquired_at': store.now()}, 86400)


def selected(candidate_id='osm:way:2'):
    return {'selected_wikipedia_page_ids': ['13'], 'subject_article_bindings': [
        {'article_id': 'wiki:13', 'candidate_id': candidate_id, 'scope': 'The received physical structure',
         'binding_basis': 'The model binds this received page to the main mapped structure.',
         'physical_binding_resolved': True}]}


def pages():
    return [{'pageid': 13, 'title': 'Observed physical structure', 'url': URL,
             'extract': 'Metadata is a lead and must not replace the acquired article.'}]


def unit(tmp_path, *, reader=None):
    store = Cache(tmp_path / 'cache')
    candidate = {'candidate_id': 'osm:way:2', 'identity_eligible': True,
                 'map_object': {'provenance': 'osm.tags', 'tags': {'building': 'yes'}}}
    service = SimpleNamespace(store=store, providers=SimpleNamespace(gemini=SimpleNamespace(_fetch_page_documents=reader)))
    story = {'id': 'fixture', '_identity_observed_candidates': [candidate]}
    return service, story, [candidate]


@pytest.mark.asyncio
async def test_ordinary_reader_reuses_actual_cached_html_without_any_network_or_images(tmp_path):
    service, story, candidates = unit(tmp_path)
    prime(service.store)
    def forbidden(request):
        pytest.fail('The ordinary reader must reuse the acquired HTML, with no image or provider call')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        reader = SimpleNamespace(store=service.store, search_http=http, _safe_page_url=GeminiClient._safe_page_url)
        service.providers.gemini._fetch_page_documents = GeminiClient._fetch_page_documents.__get__(reader)
        articles, receipt = await acquire_selected_wikipedia_text(service, story, candidates, selected(), pages())
    assert receipt['status'] == 'completed' and len(articles) == 1
    article = articles[0]
    assert article['article_id'] == 'wiki:13' and article['url'] == URL
    assert article['text'] == NORMALIZED and 'Not an article' not in article['text']
    assert article['source_sha256'] == hashlib.sha256(RAW).hexdigest()
    assert article['text_sha256'] == hashlib.sha256(NORMALIZED.encode()).hexdigest()
    assert article['raw_body_sha256_verified'] is True and article['input_kind'] == 'acquired_article_text'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unselected', 'unknown_page', 'missing_url', 'three_pages',
    'duplicate_pages', 'no_binding', 'unclosed_binding', 'unknown_physical', 'tenant_only', 'ambiguous_binding'])
async def test_only_closed_model_selected_received_physical_pages_are_read(tmp_path, change):
    async def forbidden(*args):
        pytest.fail('An unselected or unresolved article must not be read')
    service, story, candidates = unit(tmp_path, reader=forbidden)
    request, inventory = selected(), pages()
    if change == 'unselected':
        request['selected_wikipedia_page_ids'] = []
    elif change == 'unknown_page':
        request['selected_wikipedia_page_ids'] = ['999']
    elif change == 'missing_url':
        inventory[0].pop('url')
    elif change == 'three_pages':
        request['selected_wikipedia_page_ids'] = ['13', '14', '15']
    elif change == 'duplicate_pages':
        request['selected_wikipedia_page_ids'] = ['13', '13']
    elif change == 'no_binding':
        request['subject_article_bindings'] = []
    elif change == 'unclosed_binding':
        request['subject_article_bindings'][0]['physical_binding_resolved'] = False
    elif change == 'unknown_physical':
        request['subject_article_bindings'][0]['candidate_id'] = 'osm:way:999'
    elif change == 'tenant_only':
        candidates[0].update(identity_eligible=False, identity_role='institution_at_physical_subject')
    else:
        request['subject_article_bindings'] *= 2
    articles, receipt = await acquire_selected_wikipedia_text(service, story, candidates, request, inventory)
    assert not articles and (not receipt or receipt['status'] == 'not_sent')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing_cache', 'bad_cache_hash', 'wrong_text', 'wrong_raw_hash',
    'partial_document', 'empty_document', 'transport_failure', 'sql_version_without_raw_hash'])
async def test_unavailable_partial_or_corrupt_bodies_never_become_verified_text(tmp_path, change):
    document = {'normalized_text': NORMALIZED, 'final_url': URL, 'read_status': 'complete',
                'raw_content_sha256': hashlib.sha256(RAW).hexdigest(), 'source_version_id': 'existing-version'}
    async def reader(*args):
        if change == 'transport_failure':
            raise httpx.ConnectError('Offline transport failure')
        return {} if change == 'empty_document' else {URL: document}
    service, story, candidates = unit(tmp_path, reader=reader)
    if change != 'missing_cache':
        prime(service.store, stored_hash='0' * 64 if change == 'bad_cache_hash' else None)
    if change == 'wrong_text':
        document['normalized_text'] += ' An invented addition.'
    if change == 'wrong_raw_hash':
        document['raw_content_sha256'] = '0' * 64
    if change == 'partial_document':
        document['read_status'] = 'partial_text_limit'
    if change == 'sql_version_without_raw_hash':
        document.pop('raw_content_sha256')
    articles, receipt = await acquire_selected_wikipedia_text(service, story, candidates, selected(), pages())
    if change == 'sql_version_without_raw_hash':
        assert articles[0]['raw_body_sha256_verified'] and articles[0]['source_sha256'] == hashlib.sha256(RAW).hexdigest()
    else:
        assert not articles and receipt['status'] != 'completed'
    assert receipt['status'] != 'completed_empty'


@pytest.mark.asyncio
async def test_valid_initial_geometry_does_not_read_even_explicit_selected_text(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    story['_identity_wikipedia_metadata'] = pages()
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=json.dumps({**payload(geometry_decision()), **selected()}))
    async def forbidden(*args, **kwargs):
        pytest.fail('Accepted initial geometry needs no article or additional model call')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate, _fetch_page_documents=forbidden)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['joint'] and story['_identity_geometry_result']['proof_kind'] == 'geometry'


@pytest.mark.asyncio
async def test_explicit_regional_nomination_keeps_prussia_route_instead_of_wiki_replacement(tmp_path, monkeypatch):
    from street_story import identity_architectural_context
    service, story, active = geometry_setup(tmp_path)
    story['_identity_wikipedia_metadata'] = pages()
    _story, _catalog, decision, receipt = text_inputs(candidate_id='osm:way:2')
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:3',
        'reason': 'The actual SOURCE/text bay and cornice arrangement differs from the neighboring return ordering.'}]
    uncertain = geometry_decision()
    uncertain['decision'] = 'uncertain'
    initial = {**payload(uncertain), **selected(), 'regional_lookup': {
        'route': 'address', 'candidate_ids': ['osm:way:2'],
        'reason': 'The model explicitly requests the regional architectural description.'}}
    reads, calls = [], []
    async def regional(*args):
        reads.append(args[-1])
        return receipt['articles'], {'status': 'completed'}
    async def forbidden(*args, **kwargs):
        pytest.fail('An explicit Prussia nomination must not be replaced by Wiki, REF or another planner')
    monkeypatch.setattr(identity_architectural_context, 'acquire_regional_text', regional)
    monkeypatch.setattr(identity_architectural_context, 'acquire_selected_wikipedia_text', forbidden)
    async def generate(*args, **kwargs):
        calls.append('joint')
        from test_architectural_text_identity import with_received_physical_links
        result = initial if len(calls) == 1 else with_received_physical_links(decision, args[2])
        return SimpleNamespace(text=json.dumps(result))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(reads) == 1 and calls == ['joint', 'joint']
    assert story['_identity_geometry_result']['proof_kind'] == 'architectural_text'


@pytest.mark.asyncio
async def test_foreign_geometry_pointer_and_genuine_selected_wiki_text_share_one_followup(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    story['_identity_wikipedia_metadata'] = pages()
    prime(service.store)
    bad = geometry_decision()
    bad['rejected_alternatives'][0]['candidate_id'] = 'osm:way:999'
    _story, _catalog, decision, _receipt = text_inputs(candidate_id='osm:way:2', url=URL)
    decision = copy.deepcopy(decision)
    decision['article_bindings'][0]['article_id'] = 'wiki:13'
    decision['correspondences'][0]['article_id'] = 'wiki:13'
    calls, reads = [], []
    def forbidden_http(request):
        pytest.fail('Selected cached HTML needs no new request, REF or image')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_http)) as http:
        ordinary = SimpleNamespace(store=service.store, search_http=http, _safe_page_url=GeminiClient._safe_page_url)
        async def reader(urls, context):
            assert len(calls) == 1, 'No article acquisition before the model selection'
            reads.append(urls)
            return await GeminiClient._fetch_page_documents(ordinary, urls, context)
        async def generate(key, timeout, contents, config, **kwargs):
            calls.append(contents)
            if len(calls) == 1:
                assert not reads and NORMALIZED not in contents[-1]
                return SimpleNamespace(text=json.dumps({**payload(bad), **selected()}))
            assert len(calls) == 2 and reads == [[URL]]
            assert contents[0].inline_data.data == calls[0][0].inline_data.data
            assert contents[1].inline_data.data == calls[0][1].inline_data.data
            assert NORMALIZED in contents[-1] and 'unreceived_alternative_ids' in contents[-1]
            assert 'wiki:13' in config.system_instruction
            # The independent text proof may succeed while the foreign
            # geometry pointer still correctly fails host validation.
            from test_architectural_text_identity import with_received_physical_links
            final = with_received_physical_links(decision, contents)
            assert 'accepted_geometry' not in config.system_instruction
            return SimpleNamespace(text=json.dumps(final))
        async def forbidden(*args, **kwargs):
            pytest.fail('No third judge, qualified replan, REF or image is needed')
        service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate, _fetch_page_documents=reader)
        service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
        history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    result = story['_identity_geometry_result']
    assert len(calls) == 2 and len(reads) == 1
    assert result['status'] == 'match' and result['proof_kind'] == 'architectural_text'
    assert result['candidate_id'] == 'osm:way:2' and result['visual_reference_verified'] is False
    assert history['planned_queries'] == []
    saved = history['search_plan']['payload']
    assert saved['rejected_geometry']['reason'] == 'identity_geometry_proof_invalid'
    assert saved['architectural_text_proof']['source_text_receipt']['articles'][0]['source_sha256'] == hashlib.sha256(RAW).hexdigest()
