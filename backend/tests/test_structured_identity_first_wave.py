"""A model choice must become an actual distinct, literal search hypothesis."""
import json
from types import SimpleNamespace

import pytest

from street_story import article_media, identity_discovery
from street_story.identity_source_selection import first_wave_catalog, render_first_wave
from street_story.research_budget import reserve_work
from test_observed_address_search_context import observed
from test_visual_search_continuation import prepared


def choice(kind, subject_id='', query=''):
    return {'kind': kind, 'subject_id': subject_id, 'query': query, 'reason': 'Model-selected hypothesis'}


def payload(hypotheses, **extra):
    return {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '', 'commons_query': '',
        'article_queries': [], 'first_wave_hypotheses': hypotheses, **extra}


def test_selected_addresses_render_literal_suffix_and_range_and_ignore_unbound_query_prose():
    pool = observed()
    pool[-1]['map_address']['house_number'] = '13Б/2-4'
    catalog = first_wave_catalog({'_identity_observed_candidates': pool})
    result = render_first_wave(catalog, [choice('address', 'osm:node:1', 'Generic street architecture'),
        choice('address', 'osm:node:3', 'Same guess in other words')])
    assert [item['query'] for item in result] == ['Observed City Exact avenue 7A',
        'Observed City Exact avenue 13Б/2-4']
    assert result[0]['group_key'] == 'osm:way:4'
    assert result[1]['group_key'] == 'address-entry:osm:node:3'
    assert result[1]['literal_subject']['address']['house_number'] == '13Б/2-4'
    assert result[0]['literal_subject']['scope'] == 'search_hypothesis_only'


@pytest.mark.parametrize('second', [choice('address', 'osm:node:2'), choice('observed_named', 'osm:way:4')])
def test_exact_membership_and_name_alias_do_not_count_one_building_twice(second):
    pool = observed()
    pool[0]['map_object']['tags']['name'] = 'Observed municipal hall'
    catalog = first_wave_catalog({'_identity_observed_candidates': pool})
    assert catalog['required_grounded_count'] == 2
    rendered = render_first_wave(catalog, [choice('address', 'osm:node:1'), second])
    assert len(rendered) == 1 and rendered[0]['subject_id'] == 'osm:node:1'


def test_ambiguous_membership_does_not_choose_a_building_or_invent_binding():
    pool = observed()
    pool.append({'candidate_id': 'osm:way:5', 'map_object': {'tags': {'building': 'yes'},
        'building_entrances': {'proof': 'osm_closed_way_node_membership', 'candidate_ids': ['osm:node:1']}}})
    catalog = first_wave_catalog({'_identity_observed_candidates': pool})
    option = catalog['options'][('address', 'osm:node:1')]
    assert option['group_key'] == 'address-entry:osm:node:1'
    assert 'physical_candidate_id' not in option and 'source_identity' not in option


def test_one_named_municipal_group_and_no_gps_need_only_one_literal_hypothesis():
    building = {'candidate_id': 'osm:way:8', 'name': 'Display label',
        'map_object': {'tags': {'building': 'civic', 'name': 'Literal Civic Hall'}}}
    story = {'latitude': None, 'longitude': None, '_identity_observed_candidates': [building],
        '_identity_search_context': {'reverse_address': {'city': 'Observed City'}}}
    catalog = first_wave_catalog(story)
    assert catalog['required_grounded_count'] == 1
    assert render_first_wave(catalog, [choice('observed_named', 'osm:way:8')])[0]['query'] == 'Literal Civic Hall Observed City'
    assert 'map_address' not in building


def test_no_gps_or_mapped_subject_retains_unmapped_named_and_appearance_without_default_city():
    catalog = first_wave_catalog({'latitude': None, 'longitude': None}, [
        {'candidate_id': 'osm:way:8', 'name': 'Generated unnamed building', 'map_object': {'tags': {'building': 'yes'}}}])
    assert catalog['required_grounded_count'] == 0 and not catalog['options']
    queries = ['Literal SOURCE inscription civic hall', 'brick tower arched windows']
    result = render_first_wave(catalog, [choice('unmapped_named', query=queries[0]), choice('appearance', query=queries[1])])
    assert [item['query'] for item in result] == queries
    assert all(item['group_key'] == '' for item in result)
    assert render_first_wave(catalog, []) == []  # Unknown SOURCE is allowed to remain unknown.


def test_unmapped_alternatives_cannot_replace_available_distinct_grounded_coverage():
    catalog = first_wave_catalog({'_identity_observed_candidates': observed()})
    rendered = render_first_wave(catalog, [choice('appearance', query='Generic facade'),
        choice('unmapped_named', query='Another named guess')])
    assert len(rendered) == 2 and all(item['group_key'] == '' for item in rendered)
    assert render_first_wave(catalog, [choice('address', 'osm:node:999')]) == []


def test_locality_query_context_does_not_rewrite_an_address_record():
    anchor = {'candidate_id': 'osm:node:8', 'map_address': {'street': 'Full street type', 'house_number': '8A'}}
    catalog = first_wave_catalog({'_identity_observed_candidates': [anchor],
        '_identity_search_context': {'reverse_address': {'city': 'Observed City'}}})
    item = render_first_wave(catalog, [choice('address', 'osm:node:8')])[0]
    assert item['query'] == 'Observed City Full street type 8A'
    assert 'city' not in item['literal_subject']['address']
    assert item['literal_subject']['locality_context'] == 'Observed City'
    assert 'city' not in anchor['map_address']


def test_literal_occupant_names_are_useful_leads_without_building_coverage():
    occupants = [{'candidate_id': f'osm:node:{index}', 'map_object': {
        'tags': {'name': name, tag: value}, 'provenance': 'osm.tags'}} for index, name, tag, value in
        [(20, 'Literal Shop', 'shop', 'books'), (21, 'Literal Office', 'office', 'company')]]
    catalog = first_wave_catalog({'_identity_observed_candidates': occupants})
    assert catalog['required_grounded_count'] == 0
    selected = [choice('observed_named', item['candidate_id']) for item in occupants]
    rendered = render_first_wave(catalog, selected)
    assert [item['query'] for item in rendered] == ['Literal Shop', 'Literal Office']
    assert all(item['group_key'] == '' for item in rendered)
    assert all(item['literal_subject']['coverage_scope'] == 'mapped_occupant_context' for item in rendered)
    # They remain selectable, but cannot replace available physical/address coverage.
    mixed = first_wave_catalog({'_identity_observed_candidates': [*observed(), *occupants]})
    assert render_first_wave(mixed, selected) == rendered


def test_context_first_model_selection_does_not_occupy_required_grounded_transport_slots():
    occupant = {'candidate_id': 'osm:node:20', 'map_object': {'tags': {'name': 'Literal Shop', 'shop': 'books'}}}
    catalog = first_wave_catalog({'_identity_observed_candidates': [*observed(), occupant]})
    model_order = [choice('observed_named', 'osm:node:20'), choice('address', 'osm:node:3'),
        choice('address', 'osm:node:1')]
    rendered = render_first_wave(catalog, model_order)
    assert [item['subject_id'] for item in rendered] == ['osm:node:3', 'osm:node:1', 'osm:node:20']
    assert all(item['group_key'] for item in rendered[:2])
    assert [item['subject_id'] for item in model_order] == ['osm:node:20', 'osm:node:3', 'osm:node:1']


@pytest.mark.asyncio
async def test_new_upload_persists_bound_first_wave_before_search_without_second_planner(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    plans, searched = [], []
    class Executor:
        async def execute(self, operation, call):
            plans.append(operation)
            return await call('fixture', 5)
    async def generate(key, timeout, contents, config, **kwargs):
        contract = json.loads(config.system_instruction.split('\n', 1)[1].split('\n', 1)[0])
        assert 'first_wave_hypotheses' in contract['required']
        return SimpleNamespace(text=json.dumps(payload([choice('address', 'osm:node:1'),
            choice('address', 'osm:node:3')], article_queries=['Unbound model paraphrase'])))
    async def search(service, entity, visual, *, story, first_ready):
        query = story['_identity_search_query']
        saved = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']['search_plan']
        assert saved['policy_version'] == 'bounded-search-plan-v3'
        assert saved['payload']['first_wave_contract'] == 'grounded-subjects-v1'
        assert saved['payload']['first_wave_hypotheses'][0]['subject_id'] == 'osm:node:1'
        searched.append(query)
        return []
    async def empty(*args, **kwargs):
        return []
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(identity_discovery, 'retrieve', empty)
    monkeypatch.setattr(article_media, 'article_candidates', empty)
    await identity_discovery.recover(svc, snapshot, '', [], set())
    assert searched[:2] == ['Observed City Exact avenue 7A', 'Observed City Exact avenue 13']
    assert len(plans) == 1


@pytest.mark.asyncio
async def test_original_planner_readback_precedes_google_and_planner_work_admission(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    for unit in ('original', 'existing-second-plan'):
        reserve_work(svc, story['id'], 'planner_calls', [unit])
    old = {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '', 'commons_query': '',
        'article_queries': ['Frozen old literal query']}
    calls = []
    async def readback(snapshot, prompt, schema):
        calls.append(schema)
        return {'result': old, 'original_schema_readback': True}
    class Forbidden:
        async def execute(self, *args):
            pytest.fail('Original planner readback cannot dispatch Google or use another planner allowance')
    svc.providers.gemini = SimpleNamespace(executor=Forbidden(), _generate=object())
    svc.providers.research = SimpleNamespace(has_identity_search_plan_readback=lambda _: True,
        plan_identity_search=readback)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    await identity_discovery.suggest(svc, snapshot, '', [])
    assert len(calls) == 1 and snapshot['_identity_article_queries'] == ['Frozen old literal query']
    assert snapshot['_identity_search_plan_payload']['original_schema_readback'] is True


@pytest.mark.asyncio
async def test_missing_first_wave_preserves_valid_query_without_inventing_identity(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': '', 'commons_query': '', 'article_queries': ['Six paraphrases remain unbound']}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = svc._identity_snapshot(story['id'])[0]
    await identity_discovery.suggest(svc, snapshot, '', [])
    assert snapshot['_identity_article_queries'] == ['Six paraphrases remain unbound']
    assert '_identity_geometry_result' not in snapshot


@pytest.mark.asyncio
async def test_good_query_and_duplicate_preserve_one_real_operation_without_paid_repair(tmp_path, monkeypatch):
    from pydantic import SecretStr
    from street_story.gemini import GeminiExecutor, GeminiKeyPool
    svc, _adapter, story, _sessions = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        return SimpleNamespace(text=json.dumps(payload([choice('address', 'osm:node:1'),
            choice('address', 'osm:node:2')])))  # One physical group twice.
    async def qualified(snapshot, prompt, schema):
        calls.append('qualified')
        return {'result': payload([choice('address', 'osm:node:1'), choice('address', 'osm:node:3')])}
    pool = GeminiKeyPool(svc.store, (SecretStr('fixture-a'), SecretStr('fixture-b')), 'fixture-model')
    svc.providers.gemini = SimpleNamespace(executor=GeminiExecutor(pool), _generate=generate, research_routes=[])
    svc.providers.research = SimpleNamespace(plan_identity_search=qualified)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    await identity_discovery.prepare_search_plan(svc, snapshot, '', [])
    assert calls == ['google']
    assert snapshot['_identity_article_queries'] == ['Observed City Exact avenue 7A']
    import httpx
    fetched, searches = [], []
    def respond(request):
        fetched.append(request.url.path)
        return httpx.Response(200, text='<article>Own public description<img src="https://example.org/body.jpg"></article>')
    async def resolver(host):
        return '93.184.216.34'
    async def search(*args, **kwargs):
        searches.append(kwargs['story']['_identity_search_query'])
        return [{'url': 'https://example.org/body', 'title': 'Own source'}]
    original = article_media.article_candidates
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async def acquire(*args, **kwargs):
            return await original(*args, **kwargs, http=client, resolver=resolver)
        monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
        monkeypatch.setattr(article_media, 'article_candidates', acquire)
        await identity_discovery.recover(svc, snapshot, '', [], set())
    assert calls == ['google'] and searches == ['Observed City Exact avenue 7A']
    assert fetched == ['/body']
