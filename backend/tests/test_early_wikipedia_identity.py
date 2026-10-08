"""Early metadata is a selectable inventory, never a nearest-page verdict."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story import identity_discovery
from street_story.identity_source_selection import (compact_scene_manifest, compact_planner_packet,
    expand_planner_packet, grounded_wave_catalog)
from street_story.identity_wikipedia_metadata import (late_linked_page_ids, merge_metadata,
    metadata_scope, ready_metadata, selected_candidates)
from street_story.mapped_wikipedia import linked_pages
from street_story.providers import PermanentProviderError
from test_visual_search_continuation import prepared


def page(pid, *, linked_id=None, qid=None):
    return {'pageid': pid, 'title': f'Observed physical article {pid}',
        'url': f'https://ru.wikipedia.org/wiki/Observed_{pid}', 'extract': 'Observed intro',
        'image_url': f'https://upload.wikimedia.org/{pid}.jpg',
        'pageprops': {'wikibase_item': qid or f'Q{pid}'},
        **({'mapped_wikipedia_sources': [{'candidate_id': linked_id}]} if linked_id else {})}


def wiki_candidate(p):
    return {'candidate_id': 'wiki:' + str(p['pageid']), 'name': p['title'],
        'reference_image_urls': [p['image_url']], 'distance_m': p['pageid']}


@pytest.mark.asyncio
async def test_direct_links_include_far_full_pool_and_qid_only_in_cached_batches():
    cache, requests = {}, []
    def handle(request):
        requests.append(request)
        if request.url.host == 'www.wikidata.org':
            assert request.url.params['ids'] == 'Q42'
            return httpx.Response(200, json={'entities': {'Q42': {'sitelinks': {'ruwiki': {'title': 'Far body'}}}}})
        assert request.url.params['titles'] == 'Far body|Near gate'
        return httpx.Response(200, json={'query': {'pages': [
            {'pageid': 42, 'title': 'Far body', 'pageprops': {'wikibase_item': 'Q42'}},
            {'pageid': 43, 'title': 'Near gate'}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = SimpleNamespace(http=http, endpoint='https://ru.wikipedia.org/w/api.php',
            store=SimpleNamespace(cache_get=cache.get, cache_put=lambda k,v,t: cache.update({k:v})))
        osm = {'nearby': [], 'observed_pool': [
            {'type': 'way', 'id': 41, 'distance_m': 750, 'tags': {'building': 'yes', 'wikidata': 'Q42'}},
            {'type': 'way', 'id': 43, 'distance_m': 20, 'tags': {'historic': 'city_gate', 'wikipedia': 'ru:Near gate'}}]}
        first = await linked_pages(client, osm)
        second = await linked_pages(client, osm)
    assert len(requests) == 2 and first == second
    assert {item['pageid'] for item in first} == {42, 43}
    assert first[0]['mapped_wikipedia_sources'][0]['candidate_id'] == 'osm:way:41'
    assert 'lat' not in first[0]  # No borrowing mapped center coordinates.


def test_metadata_deduplicates_qid_and_retains_distinct_exact_link_provenance():
    first = page(1, linked_id='osm:way:1', qid='Q7')
    duplicate = page(2, linked_id='osm:way:2', qid='Q7')
    merged = merge_metadata([first], [duplicate, first])
    assert len(merged) == 1
    assert {link['candidate_id'] for link in merged[0]['mapped_wikipedia_sources']} == {'osm:way:1', 'osm:way:2'}


def test_shared_literal_and_label_packet_round_trip_keeps_every_id_and_address_suffix():
    packet = {'original_marker_literals': ['@42', '$42', '=abc', '==abc'],
        'map_scene': {'objects': {'columns': ['label', 'candidate_id'],
        'rows': [[i, f'osm:way:{i}'] for i in range(500)]}},
        'addresses': [[f'osm:way:{i}', 'Observed city literal', 'Observed street literal', '22–24а'] for i in range(500)],
        'memberships': {f'entry_{i}': [f'osm:way:{i}', 'osm_closed_way_node_membership'] for i in range(500)}}
    compact = compact_planner_packet(packet)
    assert expand_planner_packet(compact) == packet
    original_bytes = len(json.dumps(packet, ensure_ascii=False).encode())
    assert len(json.dumps(compact, ensure_ascii=False).encode()) < original_bytes * .8


def test_one_explicit_supported_bound_group_suffices_but_plausible_alternative_preserves_coverage():
    catalog = {'required_grounded_count': 2, 'options': {
        ('address', 'osm:node:1'): {'group_key': 'osm:way:2'}}}
    payload = {'first_wave_hypotheses': [{'kind': 'address', 'subject_id': 'osm:node:1'}],
        'spatial_hypotheses': [{'candidate_id': 'osm:way:2', 'support_status': 'spatially_supported',
            'basis': ['Observed contour corresponds to SOURCE geometry']} ]}
    assert grounded_wave_catalog(catalog, payload)['required_grounded_count'] == 1
    payload['spatial_hypotheses'].append({'candidate_id': 'osm:way:3', 'support_status': 'plausible'})
    assert grounded_wave_catalog(catalog, payload)['required_grounded_count'] == 2
    payload['spatial_hypotheses'] = [{'candidate_id': 'osm:way:2', 'support_status': 'spatially_supported', 'basis': []}]
    assert grounded_wave_catalog(catalog, payload)['required_grounded_count'] == 2


@pytest.mark.asyncio
async def test_bounded_join_retains_owned_http_work_during_existing_planning():
    gate = asyncio.Event()
    async def late():
        await gate.wait()
        return [page(2)]
    async def linked(osm):
        return [page(1)]
    async with metadata_scope() as owned:
        nearby = asyncio.create_task(late())
        result, pending, failures = await ready_metadata(SimpleNamespace(linked=linked), {}, nearby,
            timeout=.01, owned_tasks=owned)
        assert [p['pageid'] for p in result] == [1]
        assert pending == ['nearby'] and not failures and not nearby.cancelled()
        gate.set()
        await nearby
    assert nearby.done() and nearby.result()[0]['pageid'] == 2


@pytest.mark.asyncio
async def test_standalone_bounded_join_cancels_and_awaits_pending_http():
    stopped = asyncio.Event()
    async def late():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    task = asyncio.create_task(late())
    result, pending, errors = await ready_metadata(SimpleNamespace(), {}, task, timeout=.01)
    assert not result and pending == ['nearby'] and not errors
    assert stopped.is_set() and task.cancelled()


def test_selection_queues_far_semantic_choice_and_no_unselected_nearest_images():
    pages = [page(1), page(99)]
    observed = [wiki_candidate(p) for p in pages]
    active = [observed[0], {'candidate_id': 'osm:way:5', 'reference_image_urls': [pages[0]['image_url']]}]
    selected = selected_candidates(active, observed, pages, {'selected_wikipedia_page_ids': ['99']})
    assert selected[0]['candidate_id'] == 'wiki:99'
    assert not any(item['candidate_id'] == 'wiki:1' for item in selected)
    assert selected[1]['reference_image_urls'] == []
    assert observed[0]['reference_image_urls']  # Durable full metadata remains intact.


def test_late_reference_only_follows_exact_already_model_selected_physical_id():
    pages = [page(1, linked_id='osm:way:1'), page(2, linked_id='osm:way:2'), page(3)]
    plan = {'first_wave_hypotheses': [{'subject_id': 'osm:node:77', 'group_key': 'osm:way:2'}]}
    assert late_linked_page_ids(pages, plan) == ['2']
    assert late_linked_page_ids(pages, {}) == []


@pytest.mark.asyncio
async def test_ready_wiki_plan_joint_images_once_then_zero_paid_search(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    pages = [page(1), page(99)]
    observed = [wiki_candidate(p) for p in pages]
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_wikipedia_metadata': pages,
        '_identity_observed_candidates': observed}
    calls = []
    class Executor:
        async def execute(self, role, call):
            return await call('fixture', 5)
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert len(contents) == 3 and contents[0].inline_data and contents[1].inline_data
        contract = json.loads(config.system_instruction.split('\n', 1)[1].split('\n', 1)[0])
        assert 'selected_wikipedia_page_ids' in contract['required']
        packet = json.loads(contents[-1].split('Данные ниже — только контекст:\n')[1])
        assert len(packet['wikipedia_metadata']['rows']) == 2
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': 'Observed distinct geometry', 'commons_query': '', 'article_queries': [],
            'first_wave_hypotheses': [], 'selected_wikipedia_page_ids': ['99']}))
    async def scene(*args):
        return {'mime_type': 'image/png', 'bytes': b'fixture-map', 'manifest': {
            'image_sha256': 'fixture-map-sha', 'objects': {'columns': ['label', 'candidate_id'],
                'rows': [[1, 'osm:way:7']]}}}
    async def forbidden(*args, **kwargs):
        pytest.fail('Ready selected Wikipedia REF must not require paid web search')
    monkeypatch.setattr('street_story.identity_scene.planner_scene', scene)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    candidates = [observed[0]]
    history, _ = await identity_discovery.prepare_search_plan(svc, snapshot, '', candidates)
    assert history['planned_queries'] == []
    payload = history['search_plan']['payload']
    assert payload['source_map_receipt']['joint_image_input'] is True
    assert payload['source_map_receipt']['source_photo_sha256'] == story['photo_sha256']
    assert [c['candidate_id'] for c in candidates] == ['wiki:99']
    recovered = await identity_discovery.recover(svc, snapshot, '', candidates, set())
    assert recovered[0]['_comparison_deferred'] and len(calls) == 1


@pytest.mark.asyncio
async def test_new_wiki_plan_cannot_omit_explicit_selection_and_use_nearest_legacy_behavior(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    class Executor:
        async def execute(self, role, call):
            return await call('fixture', 5)
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [], 'visual_query': '',
            'commons_query': '', 'article_queries': [], 'first_wave_hypotheses': []}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    svc.providers.research = None
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_wikipedia_metadata': [page(1)]}
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(svc, snapshot, '', [])


def test_scene_compaction_keeps_all_ids_partial_contours_and_missing_levels():
    manifest = {'height_fields': ['height', 'building:levels', 'roof:levels'],
        'objects': {'columns': ['label', 'candidate_id', 'height_levels', 'geometry_status', 'contours_complete', 'raw_geometry'],
        'rows': [[n, f'osm:way:{n}', [None, None, None], 'observed', False, [0]*500]
            for n in range(500)]}}
    compact = compact_scene_manifest(manifest)
    assert len(compact['objects']['rows']) == 500
    assert 'raw_geometry' not in compact['objects']['columns']
    geometry = compact['physical_geometry']
    assert geometry['rows'][0][geometry['columns'].index('contours_complete')] is False
    assert compact['height_fields'] == ['height', 'building:levels', 'roof:levels']
    assert len(json.dumps(compact, separators=(',', ':')).encode()) < 60_000


@pytest.mark.asyncio
async def test_map_and_metadata_start_concurrently_and_map_checkpoint_precedes_link_lookup(tmp_path):
    from test_identity_lifecycle import make_service, create, FakeOSM, FakeWikipedia
    svc, _ = make_service(tmp_path)
    story = create(svc)
    metadata_started = asyncio.Event()
    map_ready = asyncio.Event()
    class Map(FakeOSM):
        async def lookup(self, lat, lon):
            await metadata_started.wait()
            result = await super().lookup(lat, lon)
            map_ready.set()
            return result
    class Wiki(FakeWikipedia):
        async def nearby(self, lat, lon):
            metadata_started.set()
            await map_ready.wait()
            return await super().nearby(lat, lon)
        async def linked(self, osm):
            _, saved = svc._identity_snapshot(story['id'])
            assert saved['osm'] == osm
            assert saved['visual_identity']['observed_candidates']
            return None
    svc.providers.osm, svc.providers.wikipedia = Map(), Wiki()
    await asyncio.wait_for(svc.resolve_identity(story['id']), 2)
    assert svc._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'match'


@pytest.mark.asyncio
async def test_initial_match_can_adopt_late_metadata_without_unbound_rejected_ids(tmp_path, monkeypatch):
    from test_identity_lifecycle import make_service, create, FakeWikipedia
    from street_story.identity_wikipedia_metadata import ready_metadata as actual_ready
    svc, gemini = make_service(tmp_path)
    story = create(svc)
    gate, finished = asyncio.Event(), asyncio.Event()
    class Wiki(FakeWikipedia):
        async def linked(self, osm):
            await gate.wait()
            finished.set()
            return [page(88, linked_id='osm:way:7')]
    async def bounded(client, osm, nearby_task, **kwargs):
        return await actual_ready(client, osm, nearby_task, timeout=.01, owned_tasks=kwargs['owned_tasks'])
    original_identify = gemini.identify_photo
    async def match(*args):
        gate.set()
        await finished.wait()
        return await original_identify(*args)
    async def prepared_plan(service, snapshot, transcript, candidates):
        identity_discovery._retain_article_discovery(service, snapshot, [], search_plan={'payload': {
            'first_wave_contract': 'grounded-subjects-v1', 'selected_wikipedia_page_ids': ['77'],
            'observed_candidate_ids': ['osm:way:7']}})
    gemini.identify_photo = match
    gemini._generate = lambda *args: None
    gemini.executor = SimpleNamespace(execute=lambda *args: None)
    svc.providers.wikipedia = Wiki()
    monkeypatch.setattr('street_story.identity_wikipedia_metadata.ready_metadata', bounded)
    monkeypatch.setattr(identity_discovery, 'prepare_search_plan', prepared_plan)
    await asyncio.wait_for(svc.resolve_identity(story['id']), 2)
    research = svc._identity_snapshot(story['id'])[1]
    assert research['visual_identity']['status'] == 'match'
    assert research['identity_article_discovery']['search_plan']['payload']['late_mapped_wikipedia_page_ids'] == ['88']


@pytest.mark.asyncio
async def test_explicit_model_geography_question_does_not_dispatch_queries(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    snapshot = {**svc._identity_snapshot(story['id'])[0], 'latitude': None, 'longitude': None}
    identity_discovery._retain_article_discovery(svc, snapshot, [], planned_queries=['Saved alternative'],
        search_plan={'payload': {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '',
            'commons_query': '', 'clarification_question': 'В каком городе сделано фото?'}})
    async def forbidden(*args, **kwargs):
        pytest.fail('An explicit useful owner question must not be followed by another paid search')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    result = await identity_discovery.recover(svc, snapshot, '', [], set())
    assert result[0]['observations'] == ['В каком городе сделано фото?']


@pytest.mark.asyncio
async def test_mapped_429_retry_after_blocks_nearby_on_same_endpoint(tmp_path):
    from street_story.providers import WikipediaClient
    from street_story.errors import RetryableProviderError
    cache, requests = {}, []
    store = SimpleNamespace(cache_get=cache.get, cache_put=lambda k,v,t:cache.update({k:v}), now=lambda:100.)
    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '25'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        wiki = WikipediaClient(store, http)
        osm = {'observed_pool': [{'type':'way','id':7,'distance_m':500,
            'tags':{'building':'yes','wikipedia':'ru:Observed body'}}]}
        with pytest.raises(RetryableProviderError) as failed:
            await wiki.linked(osm)
        assert failed.value.retry_at == 125
        with pytest.raises(RetryableProviderError) as cooled:
            await wiki.nearby(1,2)
        assert cooled.value.retry_at == 125
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_nearby_positive_region_cache_reuses_actual_page_coords_and_original_lookup_scope():
    from street_story.providers import WikipediaClient
    cache, requests = {}, []
    store = SimpleNamespace(cache_get=cache.get, cache_put=lambda k,v,t:cache.update({k:v}), now=lambda:100.)
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'query': {'pages': [{'pageid': 7, 'title': 'Observed body',
            'coordinates': [{'lat': 54.7, 'lon': 20.5, 'primary': True}], 'pageprops': {'wikibase_item': 'Q7'}}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        wiki = WikipediaClient(store, http)
        first = await wiki.nearby(54.7001, 20.5001)
        second = await wiki.nearby(54.7002, 20.5002)
    assert len(requests) == 1
    assert (second[0]['lat'], second[0]['lon']) == (54.7,20.5)
    assert second[0]['distance_m'] > first[0]['distance_m']
    assert second[0]['metadata_query'] == first[0]['metadata_query']
    assert second[0]['metadata_query']['scope'] == 'positive_area_inventory_only'
