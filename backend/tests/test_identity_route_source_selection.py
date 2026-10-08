import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.db import Store
from street_story.errors import RetryableProviderError
from street_story.identity_source_selection import model_selection
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical
from test_backend import config
from test_research_control import fixture


OBSERVED = [{'url': 'https://example.com/article', 'title': 'Physical building'},
            {'url': 'https://example.com/gallery', 'title': 'Gallery'}]


def choice(url='https://example.com/article'):
    return {'summary': 'Search choices', 'selected_sources': [{'url': url, 'reason': 'Shows the external facade'}]}


@pytest.mark.parametrize('payload,status,count', [
    (choice(), 'model_selected', 1),
    (choice('https://invented.example/none'), 'selection_unavailable', 0),
    ({'summary': '', 'selected_sources': [{'url': OBSERVED[0]['url'], 'reason': '  '}]}, 'selection_unavailable', 0),
    ({'summary': '', 'selected_sources': []}, 'model_selected', 0),
    ({'summary': 'ignored prose'}, 'selection_unavailable', 0),
    ({'summary': '', 'selected_sources': [{'url': OBSERVED[0]['url']}]}, 'selection_unavailable', 0),
])
def test_only_observed_model_selected_urls_with_reasons(payload, status, count):
    selected, receipt = model_selection(OBSERVED, payload)
    assert receipt['status'] == status and len(selected) == count
    assert all(s['url'] in {x['url'] for x in OBSERVED} for s in selected)
    if count:
        assert selected[0]['source_selection_reason'] == 'Shows the external facade'
    assert receipt['discovered_count'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('body,expected', [(choice(), 1), ({'summary': '', 'selected_sources': []}, 0), ('malformed', 0)])
async def test_google_selects_from_same_observed_grounding_response(tmp_path, body, expected):
    client = GeminiClient(config(tmp_path), Store(tmp_path/'db'))
    calls = []
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)
    client.web_search_routes = [('configured-model', None, object(), Executor())]
    async def generate(key, timeout, contents, configuration, **kwargs):
        calls.append(contents)
        assert configuration.tools[0].google_search is not None
        return SimpleNamespace(text=json.dumps(body) if isinstance(body, dict) else body,
            candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(uri=s['url'], title=s['title'])) for s in OBSERVED]))])
    client._generate = generate
    result = await client.discover_article_urls('plain mapped address city', purpose='identity')
    assert len(calls) == 1 and len(result.grounding_sources) == expected
    assert len(result.payload['discovered_sources']) == 2
    assert result.payload['source_selection']['status'] == ('selection_unavailable' if body == 'malformed' else 'model_selected')
    # Existing facts callers still receive grounding inventory even when the
    # assistant did not provide identity's additional semantic selection.
    facts = await client.discover_article_urls('facts about confirmed building')
    assert len(facts.grounding_sources) == 2


@pytest.mark.asyncio
async def test_public_inventory_selection_uses_text_quota_without_search_tools(tmp_path):
    client = GeminiClient(config(tmp_path), Store(tmp_path/'db'))
    class Executor:
        async def execute(self, operation, call):
            assert operation == 'grounded_research'
            return await call('fixture', 5)
    client.research_routes = [('configured-model', None, object(), Executor())]
    async def generate(key, timeout, contents, configuration, **kwargs):
        assert not configuration.tools
        assert kwargs['operation'] == 'grounded_research'
        return SimpleNamespace(text=json.dumps(choice()))
    client._generate = generate
    result = await client.select_identity_sources('plain address', OBSERVED, {})
    assert result['source_selection']['status'] == 'model_selected'
    assert [source['url'] for source in result['sources']] == [OBSERVED[0]['url']]


@pytest.mark.asyncio
async def test_source_choice_receives_original_pixels_and_map_alternatives(tmp_path):
    from test_reference_image_codec import jpeg
    from street_story.reference_image_codec import normalize_reference
    client = GeminiClient(config(tmp_path), Store(tmp_path/'db'))
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)
    client.research_routes = [('configured-model', None, object(), Executor())]
    image = normalize_reference(jpeg())
    async def generate(key, timeout, contents, configuration, **kwargs):
        assert contents[0].inline_data.data == image[1]
        assert not configuration.tools
        supplied = json.loads(contents[1].split('Return JSON.\n')[1])
        assert supplied['physical_candidates'][0]['map_address']['street'] == 'Alternate Road'
        assert supplied['observed_sources'][0]['snippet'] == 'Literal observed snippet'
        return SimpleNamespace(text=json.dumps(choice()))
    client._generate = generate
    inventory = [{**OBSERVED[0], 'supports': [{'text': 'Literal observed snippet'}]}]
    await client.select_identity_sources('Unverified first guess', inventory, {
        '_identity_selection_image': image, 'research_json': canonical({'visual_identity': {'candidates': [
            {'candidate_id': 'osm:way:42', 'map_address': {'street': 'Alternate Road'}}]}})})


@pytest.mark.asyncio
async def test_fast_selected_route_persisted_while_peer_search_waits(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    retained = []
    async def slow_search(query, story):
        entered.set()
        await release.wait()
        return {'sources': [], 'receipt': {'discovered_sources': [], 'source_selection': {'status': 'model_selected'}}}
    async def fast_google(query, *, purpose):
        assert purpose == 'identity' and query == 'plain address'
        return GroundedResearch({'discovered_sources': OBSERVED, 'source_selection': {'status': 'model_selected'}}, [OBSERVED[0]])
    service = SimpleNamespace(providers=SimpleNamespace(research=SimpleNamespace(search_articles=slow_search),
        gemini=SimpleNamespace(discover_article_urls=fast_google)), store=SimpleNamespace(now=lambda: 100))
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery', lambda svc, story, sources, **kwargs: retained.append((sources, kwargs)))
    monkeypatch.setattr(identity_discovery, 'record_identity_event', lambda *args: None)
    task = asyncio.create_task(identity_discovery.web_image_sources(service, '', '', story={'id':'fixture','_identity_search_query':'plain address'}))
    await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    assert any(sources == [OBSERVED[0]] for sources, _ in retained)
    assert any(kwargs.get('discovered_sources') == OBSERVED for _, kwargs in retained)
    release.set()
    assert await task == [OBSERVED[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize('selection_fails', [False, True])
async def test_public_inventory_never_bypasses_model_choice(monkeypatch, selection_fails):
    retained = []
    async def raw(query):
        return GroundedResearch({}, OBSERVED)
    async def unavailable_search(*args):
        raise RetryableProviderError('route_closed')
    async def select(query, inventory, story):
        assert inventory == OBSERVED
        if selection_fails:
            raise RetryableProviderError('identity_source_selection_outcome_unknown')
        return {'sources': [], 'source_selection': {'status':'model_selected'}}
    service = SimpleNamespace(providers=SimpleNamespace(research=SimpleNamespace(search_articles=unavailable_search, select_identity_sources=select),
        gemini=SimpleNamespace(_public_web_search=raw)), store=SimpleNamespace(now=lambda:100))
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery', lambda svc, story, sources, **kwargs: retained.append((sources, kwargs)))
    monkeypatch.setattr(identity_discovery, 'record_identity_event', lambda *args:None)
    with pytest.raises(RetryableProviderError):
        # The unrelated unavailable peer leaves a partial query; raw URLs still
        # do not become sources when public's model selected nothing.
        await identity_discovery.web_image_sources(service, 'hypothesis', '', story={'id':'fixture'})
    assert not any(sources for sources, _ in retained)
    assert any(kwargs.get('discovered_sources') == OBSERVED for _, kwargs in retained)
    if selection_fails:
        assert any(next(iter(kwargs.get('source_selections', {}).values()), {}).get('status') == 'selection_unavailable'
                   for _, kwargs in retained)


def adapter_fixture(tmp_path):
    svc, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = svc
    adapter.client = SimpleNamespace(endpoint='http://existing-opencode:4097', model_id='configured', provider_id='opencode')
    svc.store.cache_put('research-text-verification-v1', {'model_id':'configured','endpoint':adapter.client.endpoint,
        'semantic_contract_verified':True}, 600)
    return svc, adapter, {'id':sid,'photo_sha256':photo}


@pytest.mark.asyncio
async def test_public_selection_existing_fenced_text_operation_and_completed_readback(tmp_path):
    svc, adapter, story = adapter_fixture(tmp_path)
    calls = []
    async def run(role, prompt, binding, schema):
        calls.append((role,prompt,binding))
        assert role == 'facts' and 'selected_sources' in schema['properties']
        receipt = {'phase':'completed','result':choice(),'binding':binding}
        await adapter.checkpoint(binding,receipt)
        return {'result':choice(),'receipt':receipt}
    adapter.client._run = run
    result = await adapter.select_identity_sources('plain address', OBSERVED, story)
    assert [s['url'] for s in result['sources']] == [OBSERVED[0]['url']]
    assert len(calls)==1
    # Existing completion is reused even if qualification later disappears.
    svc.store.cache_put('research-text-verification-v1',{},600)
    cached = await adapter.select_identity_sources('plain address', OBSERVED, story)
    assert cached['sources']==result['sources'] and len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('addressed', [False, True])
async def test_unknown_original_selection_never_new_send(tmp_path, addressed):
    svc, adapter, story = adapter_fixture(tmp_path)
    inventory=[{'url':s['url'],'title':s['title'],'snippet':''} for s in OBSERVED]
    unit=canonical(['plain address',inventory])
    binding,_=adapter.attempt(story,'identity_source_selection',unit)
    receipt={'phase':'unknown','binding':binding,'model_id':'configured'}
    if addressed:
        receipt.update(session_id='original-session',message_id='original-message')
    await adapter.checkpoint(binding,receipt)
    calls=[]
    async def observe(role,prompt,current,schema):
        calls.append(current)
        assert current['phase']=='unknown' and current['session_id']=='original-session'
        return {'result':choice(),'receipt':receipt}
    adapter.client._run=observe
    if addressed:
        assert (await adapter.select_identity_sources('plain address',OBSERVED,story))['sources']
        assert len(calls)==1
    else:
        with pytest.raises(RetryableProviderError,match='outcome_unknown'):
            await adapter.select_identity_sources('plain address',OBSERVED,story)
        assert not calls
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts WHERE story_id=?',(story['id'],)).fetchone()[0]==1


@pytest.mark.asyncio
async def test_all_37_real_inventory_urls_fit_one_small_text_unit(tmp_path):
    svc,adapter,story=adapter_fixture(tmp_path)
    urls=[{'url':f'https://news.example/articles/{i}/long-article-path','title':'Article '*40,
           'supports':[{'text':'Snippet '*1000}]} for i in range(37)]
    async def run(role,prompt,binding,schema):
        assert len(prompt)<17000
        supplied=json.loads(prompt.split('Query and inventory:\n')[1])['observed_sources']
        assert len(supplied)==37
        assert [s['url'] for s in supplied]==[s['url'] for s in urls]
        return {'result':{'summary':'','selected_sources':[]},'receipt':{}}
    adapter.client._run=run
    result=await adapter.select_identity_sources('plain address',urls,story)
    assert result['discovered_sources']==urls and result['sources']==[]


@pytest.mark.asyncio
async def test_restart_uses_original_public_inventory_before_unknown_selection(tmp_path, monkeypatch):
    svc, _adapter, story, _session = __import__('test_visual_search_continuation').prepared(tmp_path)
    story['_identity_search_query'] = 'plain mapped address'
    searched, selected = [], []
    async def search(query):
        searched.append(query)
        assert len(searched) == 1  # Restart may not generate another inventory/unit.
        return GroundedResearch({}, OBSERVED)
    async def opencode(*args):
        return {'sources': [], 'receipt': {'source_selection': {'status': 'model_selected'}}}
    async def select(query, inventory, current):
        selected.append(inventory)
        if len(selected) == 1:
            raise RetryableProviderError('identity_source_selection_outcome_unknown')
        return {'sources': [inventory[0]], 'source_selection': {'status': 'model_selected'}}
    svc.providers.research = SimpleNamespace(search_articles=opencode, select_identity_sources=select)
    svc.providers.gemini = SimpleNamespace(_public_web_search=search)
    with pytest.raises(RetryableProviderError, match='selection_unavailable'):
        await identity_discovery.web_image_sources(svc, '', '', story=story)
    with svc.store.connection() as db:
        history = json.loads(svc._story_row(db, story['id'])['research_json'])['identity_article_discovery']
    assert history['sources'] == [] and history['discovered_sources'] == OBSERVED
    assert history['source_selections']['public_web:plain mapped address']['discovered_sources'] == OBSERVED
    assert await identity_discovery.web_image_sources(svc, '', '', story=story) == [OBSERVED[0]]
    assert searched == ['plain mapped address'] and selected == [OBSERVED, OBSERVED]


@pytest.mark.asyncio
async def test_empty_public_search_needs_no_semantic_provider(monkeypatch):
    async def search(query):
        return GroundedResearch({}, [])
    service = SimpleNamespace(providers=SimpleNamespace(gemini=SimpleNamespace(_public_web_search=search)),
        store=SimpleNamespace(now=lambda: 100))
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery', lambda *args, **kwargs: None)
    monkeypatch.setattr(identity_discovery, 'record_identity_event', lambda *args: None)
    assert await identity_discovery.web_image_sources(service, '', '', story={'id': 'empty'}) == []


@pytest.mark.asyncio
async def test_unqualified_public_selector_waits_without_call_or_fake_empty_result(tmp_path):
    svc, adapter, story = adapter_fixture(tmp_path)
    svc.store.cache_put('research-text-verification-v1', {}, 600)
    async def forbidden(*args):
        raise AssertionError('Unqualified route must not run')
    adapter.client._run = forbidden
    with pytest.raises(RetryableProviderError, match='identity_source_selection_unavailable'):
        await adapter.select_identity_sources('plain address', OBSERVED, story)
    with svc.store.connection() as db:
        assert not db.execute('SELECT 1 FROM research_provider_attempts WHERE story_id=?', (story['id'],)).fetchone()


@pytest.mark.asyncio
async def test_unknown_selection_model_change_never_uses_new_route(tmp_path):
    _svc, adapter, story = adapter_fixture(tmp_path)
    inventory = [{'url': s['url'], 'title': s['title'], 'snippet': ''} for s in OBSERVED]
    binding, _ = adapter.attempt(story, 'identity_source_selection', canonical(['plain address', inventory]))
    await adapter.checkpoint(binding, {'phase': 'unknown', 'binding': binding, 'model_id': 'original-model',
        'session_id': 'original-session', 'message_id': 'original-message'})
    async def forbidden(*args):
        raise AssertionError('Model change must not create another request')
    adapter.client._run = forbidden
    with pytest.raises(RetryableProviderError, match='binding_changed'):
        await adapter.select_identity_sources('plain address', OBSERVED, story)
