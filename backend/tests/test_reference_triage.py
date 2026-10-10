import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from street_story.reference_triage import apply_triage, build_atlas, validate_verdict, triage_queue


def image(colour, size=240):
    out = io.BytesIO()
    Image.new('RGB', (size, size + 20), colour).save(out, format='JPEG')
    return out.getvalue()


def frame(index):
    return {'candidate_id': f'web:{index}', 'reference_id': f'ref:{index}', 'name': f'Article {index}',
            'url': f'https://example.com/article/{index}', 'reference_image_urls': [f'https://example.com/{index}.jpg']}


def ratings(atlas):
    return {'tiles': [{'tile_id': tile['tile_id'], 'kind': 'diagram' if i == 1 else 'unclear',
        'applicability': 'exterior_geometry' if i == 2 else 'unclear',
        'priority': 'promising' if i == 2 else 'unlikely' if i == 1 else 'unclear',
        'reason': 'Visible target geometry' if i == 2 else 'Need original detail'}
        for i, tile in enumerate(atlas['manifest']['tiles'])]}


def test_exact_pixels_and_article_mapping_survive_deduplication_reordering_and_source_change():
    originals = [image(colour) for colour in ('red', 'green', 'blue')]
    frames = [frame(i) for i in range(4)]
    atlas = build_atlas(image('white'), list(zip(frames, [*originals, originals[0]])), {'lens': 24})
    assert len(atlas['manifest']['tiles']) == 3
    assert [ref['reference_id'] for ref in atlas['manifest']['tiles'][0]['references']] == ['ref:0', 'ref:3']
    assert atlas['manifest']['tiles'][0]['references'][1]['article_url'] == frames[3]['url']
    ordered = apply_triage([frames[1], frames[2], frames[0], frames[3], frames[2]], atlas, ratings(atlas))
    assert [c['reference_id'] for c in ordered] == ['ref:2', 'ref:0', 'ref:3', 'ref:1']
    assert ordered[-1]['reference_triage']['kind'] == 'diagram'
    assert set(c['reference_id'] for c in ordered) == {c['reference_id'] for c in frames}
    assert build_atlas(image('black'), list(zip(frames, [*originals, originals[0]])), {'lens': 24})['atlas_id'] != atlas['atlas_id']
    assert build_atlas(image('white'), list(zip(frames, [*originals, originals[0]])), {'lens': 72})['atlas_id'] != atlas['atlas_id']
    with Image.open(io.BytesIO(atlas['bytes'])) as sheet:
        assert sheet.size == (1020, 366)


@pytest.mark.parametrize('bad', ['foreign', 'duplicate', 'missing'])
def test_unknown_duplicate_or_missing_tile_id_cannot_change_queue(bad):
    atlas = build_atlas(image('white'), [(frame(i), image(colour)) for i, colour in enumerate(('red', 'green', 'blue'))], {})
    result = ratings(atlas)
    if bad == 'foreign':
        result['tiles'][0]['tile_id'] = 'tile_other'
    elif bad == 'duplicate':
        result['tiles'][1]['tile_id'] = result['tiles'][0]['tile_id']
    else:
        result['tiles'].pop()
    with pytest.raises(ValueError):
        validate_verdict(result, atlas)


def test_single_and_double_pool_are_triaged_and_structural_noise_is_deferred():
    for count in (1, 2):
        atlas = build_atlas(image('white'), [(frame(i), image(c)) for i, c in enumerate(('red', 'blue')[:count])], {})
        assert len(atlas['manifest']['tiles']) == count
    assert build_atlas(image('white'), [(frame(2), image('blue', 1)), (frame(3), b'corrupt-file')], {}) is None


def provider_fixture(svc, monkeypatch, *, unknown=False, decide=None):
    from street_story.research_adapter import ProductResearchAdapter
    provider = ProductResearchAdapter(svc, admission=object())
    class Executor:
        async def execute(self, operation, call):
            assert operation == 'grounded_research'
            return await call('offline-fixture', 20)
    monkeypatch.setattr(provider.primary_vision, '_verified_routes', lambda: [('fixture', None, None, Executor())])
    downloads, sends = [], []
    async def load(url):
        downloads.append(url)
        colour = {'a': 'red', 'b': 'green', 'c': 'blue', 'd': 'yellow'}[url.rsplit('/', 1)[-1][0]]
        return 'image/jpeg', image(colour)
    async def generate(key, timeout, contents, config, **kwargs):
        kwargs['before_provider_send']()
        assert timeout <= 20 and len(contents) == 5
        manifest = json.loads(contents[-1].split('Manifest: ')[1])
        sends.append(manifest)
        if unknown:
            raise asyncio.TimeoutError('unknown fixture')
        return SimpleNamespace(text=json.dumps(decide(manifest) if decide else ratings({'manifest': manifest})), response_id='offline-triage')
    monkeypatch.setattr(provider.primary_vision, '_load_public_reference', load)
    monkeypatch.setattr(provider.primary_vision.client, '_generate', generate, raising=False)
    svc.providers.research = provider
    return provider, downloads, sends


@pytest.mark.asyncio
async def test_contact_sheet_hook_orders_real_worker_pairs_without_using_tiles_as_proof(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path, count=3)
    provider, downloads, sends = provider_fixture(svc, monkeypatch)
    pairs = []
    async def verdict(snapshot, item, schema, context):
        pairs.append(item)
        assert len(item['_visual_image_parts']) == 2
        assert item['_visual_reference_mapping'][0]['candidate_id'] == 'gate:c'
        assert base64.b64decode(item['_visual_image_parts'][1]['data']) == image('blue')
        assert item['_visual_reference_mapping'][0]['source_url'] == 'https://example.com/c.jpg'
        assert 'CONTACT SHEET' not in json.dumps(item['_visual_image_parts'])
        return response(item, 'offline-full-pair', 'match')
    monkeypatch.setattr(provider, 'visual_verdict', verdict)
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(sends) == 1 and len(downloads) == 3 and len(pairs) == 1
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:c'
    _, research = svc._identity_snapshot(story['id'])
    atlas = next(iter(research['visual_search_operation']['reference_triage_atlases'].values()))
    assert atlas['phase'] == 'completed'
    assert len(research['visual_search_operation']['queue']) == 2
    assert 'bytes' not in atlas
    with svc.store.connection() as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='reference_triage'").fetchone()[0])
    assert receipt['phase'] == 'completed' and receipt['provider_send_state'] == 'response_closed'


@pytest.mark.asyncio
async def test_unknown_triage_retains_original_operation_without_new_send_on_wakeup(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare
    from street_story.headless_identity import HeadlessIdentity
    svc, story, source = prepare(tmp_path, count=3)
    provider, downloads, sends = provider_fixture(svc, monkeypatch, unknown=True)
    _, research = svc._identity_snapshot(story['id'])
    state = {'queue': [{**candidate, 'reference_id': f'ref:{i}'} for i, candidate in
                      enumerate(research['visual_identity']['candidates'])]}
    worker = HeadlessIdentity(svc)
    session = SimpleNamespace(id='offline-live', resource_id=story['id'], state={})
    monkeypatch.setattr(worker, '_save_visual_queue', lambda *args: None)
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    original = [c['reference_id'] for c in state['queue']]
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    assert len(sends) == 1 and len(downloads) == 3
    assert [c['reference_id'] for c in state['queue']] == original
    assert not state.get('reference_triage_deferred')
    assert not state.get('reviewed_reference_ids')
    with svc.store.connection() as db:
        rows = list(db.execute("SELECT logical_id,receipt_json FROM research_provider_attempts WHERE role='reference_triage'"))
    assert len(rows) == 1 and json.loads(rows[0]['receipt_json'])['phase'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('requested,expected', [(None, 8192), (1024, 1024), (8192, 8192), (8193, 8192), (0, 8192)])
async def test_explicit_small_output_cap_reduces_shared_reservation_and_keeps_provider_admission(requested, expected):
    pytest.importorskip('ai_resource_control.client', reason='Private pinned admission SDK: verified by the full devserver suite')
    from google.genai import types
    from street_story.providers import GeminiClient
    calls = []
    class Quota:
        async def run(self, key, timeout, size, invoke):
            calls.append(('reservation', size))
            return await invoke()
    async def request(key, timeout, contents, config, **kwargs):
        calls.append(('provider', config.max_output_tokens))
        return SimpleNamespace(text='offline')
    client = SimpleNamespace(_provider_request=request)
    config = types.GenerateContentConfig(max_output_tokens=requested)
    await GeminiClient._generate(client, 'offline-fixture', 20, ['fixture'], config,
                                 model='fixture', quota=Quota())
    from ai_resource_control.client import estimate_input_tokens
    envelope = {'contents': [{'text': 'fixture'}],
        'config': config.model_dump(mode='json', exclude_none=True)}
    assert calls == [('reservation', 1000 + expected + estimate_input_tokens(envelope)), ('provider', expected)]
    class Denied:
        async def run(self, key, timeout, size, invoke):
            raise RuntimeError('offline-admission-denied')
    calls.clear()
    with pytest.raises(RuntimeError, match='offline-admission-denied'):
        await GeminiClient._generate(client, 'offline-fixture', 20, ['fixture'], config,
                                     model='fixture', quota=Denied(), before_provider_send=lambda: calls.append('sent'))
    assert calls == []


@pytest.mark.asyncio
async def test_four_atlas_envelope_preserves_pairs_without_download_and_two_images_can_be_triaged(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare
    from street_story.headless_identity import HeadlessIdentity
    svc, story, source = prepare(tmp_path, count=3)
    _, downloads, sends = provider_fixture(svc, monkeypatch)
    _, research = svc._identity_snapshot(story['id'])
    worker = HeadlessIdentity(svc)
    monkeypatch.setattr(worker, '_save_visual_queue', lambda *args: None)
    session = SimpleNamespace(id='offline-live', resource_id=story['id'], state={})
    candidates = [{**c, 'reference_id': f'ref:{i}'} for i, c in enumerate(research['visual_identity']['candidates'])]
    state = {'queue': candidates.copy(), 'reference_triage_atlases': {str(i): {} for i in range(4)}}
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    assert state['reference_triage_budget_exhausted'] and state['queue'] == candidates
    assert not state.get('reference_triage_deferred')
    assert downloads == [] and sends == []
    state = {'queue': candidates[:2]}
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    assert len(downloads) == 2 and len(sends) == 1
    assert all(c['reference_triage']['applicability'] == 'unclear' for c in state['queue'])


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_real_worker_defers_even_small_map_batches_without_exact_pairs(tmp_path, monkeypatch, count):
    from test_parallel_identity_pairs import prepare
    svc, story, _ = prepare(tmp_path, count=count)
    def decision(manifest):
        return {'tiles': [{'tile_id': t['tile_id'], 'kind': 'map', 'applicability': 'not_comparable',
            'priority': 'unlikely', 'reason': 'Location map has no facade geometry'} for t in manifest['tiles']]}
    provider, downloads, sends = provider_fixture(svc, monkeypatch, decide=decision)
    pairs = []
    async def pair(*args):
        pairs.append(args)
        raise AssertionError('A deferred map cannot become an exact image comparison')
    monkeypatch.setattr(provider, 'visual_verdict', pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(downloads) == count and len(sends) == 1 and pairs == []
    _, research = svc._identity_snapshot(story['id'])
    queue = research['visual_search_operation']
    assert len(queue['reference_triage_deferred']) == count
    assert not queue['reviewed_reference_ids']
    assert all(c['triage_deferral'] == 'model_declared_not_comparable' for c in queue['reference_triage_deferred'])
    assert svc.story(story['id'])['visual_identity']['status'] != 'match'


@pytest.mark.asyncio
async def test_real_worker_accepts_model_applicable_architecture_render_after_map_deferral(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path, count=2)
    def decision(manifest):
        return {'tiles': [{'tile_id': t['tile_id'], 'kind': 'diagram' if i else 'map',
            'applicability': 'exterior_geometry' if i else 'not_comparable',
            'priority': 'promising' if i else 'unlikely', 'reason': 'Fixture applicability receipt'}
            for i, t in enumerate(manifest['tiles'])]}
    provider, _, sends = provider_fixture(svc, monkeypatch, decide=decision)
    pairs = []
    async def pair(snapshot, item, schema, context):
        pairs.append(item['_visual_reference_mapping'][0]['candidate_id'])
        assert base64.b64decode(item['_visual_image_parts'][1]['data']) == image('green')
        return response(item, 'offline-render-full-pair', 'match')
    monkeypatch.setattr(provider, 'visual_verdict', pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(sends) == 1 and pairs == ['gate:b']
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
    _, research = svc._identity_snapshot(story['id'])
    queue = research['visual_search_operation']
    assert len(queue['reviewed_reference_ids']) == 1
    assert queue['reference_triage_deferred'][0]['candidate_id'] == 'gate:a'


@pytest.mark.asyncio
async def test_unknown_atlas_allows_independent_next_atlas_but_never_resends_original(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare
    from street_story.headless_identity import HeadlessIdentity
    svc, story, source = prepare(tmp_path, count=2)
    _, downloads, sends = provider_fixture(svc, monkeypatch, unknown=True)
    _, research = svc._identity_snapshot(story['id'])
    worker = HeadlessIdentity(svc)
    monkeypatch.setattr(worker, '_save_visual_queue', lambda *args: None)
    session = SimpleNamespace(id='offline-live', resource_id=story['id'], state={})
    originals = [{**c, 'reference_id': f'ref:{i}'} for i, c in enumerate(research['visual_identity']['candidates'])]
    state = {'queue': [originals[0]]}
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    state['queue'].append(originals[1])
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    assert len(downloads) == len(sends) == 2
    assert {c['reference_id'] for c in state['queue']} == {'ref:0', 'ref:1'}
    assert not state.get('reference_triage_deferred')
    with svc.store.connection() as db:
        attempts = list(db.execute("SELECT logical_id FROM research_provider_attempts WHERE role='reference_triage'"))
    assert len(attempts) == len(set(row[0] for row in attempts)) == 2


@pytest.mark.asyncio
async def test_source_specific_rating_is_not_reused_for_different_source_pixels(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare
    from street_story.headless_identity import HeadlessIdentity
    svc, story, source = prepare(tmp_path, count=1)
    _, _, sends = provider_fixture(svc, monkeypatch)
    _, research = svc._identity_snapshot(story['id'])
    candidate = {**research['visual_identity']['candidates'][0], 'reference_id': 'ref:0'}
    worker = HeadlessIdentity(svc)
    monkeypatch.setattr(worker, '_save_visual_queue', lambda *args: None)
    session = SimpleNamespace(id='offline-live', resource_id=story['id'], state={})
    state = {'queue': [candidate]}
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    old = state['queue'][0]['reference_triage']['atlas_id']
    await triage_queue(worker, session, state, story, image('black'), research['visual_identity'], generation=0, control_revision=0)
    assert len(sends) == 2 and state['queue'][0]['reference_triage']['atlas_id'] != old


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown', [True, False])
async def test_optional_triage_failure_preserves_pair_and_original_receipt(tmp_path, monkeypatch, unknown):
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path, count=1)
    provider, _, sends = provider_fixture(svc, monkeypatch, unknown=unknown, decide=lambda manifest: {'tiles': []})
    pairs = []
    async def compare(snapshot, item, schema, context):
        pairs.append(item)
        assert len(item['_visual_image_parts']) == 2
        return response(item, 'independent-native-fixture', 'match')
    monkeypatch.setattr(provider, 'visual_verdict', compare)
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(sends) == len(pairs) == 1
    assert svc.story(story['id'])['visual_identity']['status'] == 'match'
    with svc.store.connection() as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='reference_triage'").fetchone()[0])
    assert receipt['phase'] == ('unknown' if unknown else 'failed')
    assert receipt['provider_send_state'] == ('possibly_sent' if unknown else 'response_closed')
    _, research = svc._identity_snapshot(story['id'])
    assert len(research['visual_search_operation']['reviewed_reference_ids']) == 1


@pytest.mark.asyncio
async def test_optional_atlas_allowance_does_not_block_full_pair(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare, response
    import street_story.reference_triage as triage
    svc, story, _ = prepare(tmp_path, count=1)
    provider, _, sends = provider_fixture(svc, monkeypatch)
    monkeypatch.setattr(triage, 'MAX_ATLASES', 0)
    pairs = []
    async def compare(snapshot, item, schema, context):
        pairs.append(item)
        return response(item, 'independent-native-fixture', 'match')
    monkeypatch.setattr(provider, 'visual_verdict', compare)
    assert await svc.run_once(claim_kind='identity_visual')
    assert sends == [] and len(pairs) == 1
    assert svc.story(story['id'])['visual_identity']['status'] == 'match'
    _, research = svc._identity_snapshot(story['id'])
    assert research['visual_search_operation']['reference_triage_budget_exhausted']
    assert len(research['visual_search_operation']['reviewed_reference_ids']) == 1


@pytest.mark.asyncio
async def test_unavailable_triage_restores_legacy_originals_without_resending_unknown(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare
    from street_story.headless_identity import HeadlessIdentity
    svc, story, source = prepare(tmp_path, count=1)
    provider, downloads, sends = provider_fixture(svc, monkeypatch, unknown=True)
    _, research = svc._identity_snapshot(story['id'])
    worker = HeadlessIdentity(svc)
    monkeypatch.setattr(worker, '_save_visual_queue', lambda *args: None)
    session = SimpleNamespace(id='offline-live', resource_id=story['id'], state={})
    candidate = {**research['visual_identity']['candidates'][0], 'reference_id': 'ref:0'}
    state = {'queue': [candidate]}
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    state['reference_triage_deferred'] = [{**state['queue'].pop(), 'triage_deferral': 'triage_unknown'}]
    monkeypatch.setattr(provider.primary_vision, '_verified_routes', lambda: [])
    await triage_queue(worker, session, state, story, source, research['visual_identity'], generation=0, control_revision=0)
    assert state['queue'] == [candidate] and not state['reference_triage_deferred']
    assert len(sends) == len(downloads) == 1
    assert next(iter(state['reference_triage_atlases'].values()))['phase'] == 'unknown'


@pytest.mark.asyncio
async def test_two_originals_use_distinct_providers_before_duplicate_google_slots(tmp_path, monkeypatch):
    from test_parallel_identity_pairs import prepare, response
    from street_story.errors import RetryableProviderError
    svc, story, _ = prepare(tmp_path, count=2)
    provider, _, _ = provider_fixture(svc, monkeypatch)
    routes = provider.primary_vision._verified_routes()
    pool = SimpleNamespace(snapshot=lambda operation: {'healthy_keys': 4})
    monkeypatch.setattr(provider.primary_vision, '_verified_routes',
                        lambda: [(model, pool, quota, executor) for model, _, quota, executor in routes])
    provider.native_vision = SimpleNamespace(available=True)
    seen = []
    async def compare(route, snapshot, item, schema, context):
        seen.append((route, item['_visual_reference_mapping'][0]['candidate_id'],
                     json.loads(context)['comparison_id']))
        if route == 'google':
            raise RetryableProviderError('original_google_outcome_unknown', retry_at=svc.store.now()+300)
        assert route == 'native' and len(item['_visual_image_parts']) == 2
        return response(item, 'independent-native-fixture', 'match')
    monkeypatch.setattr(provider, 'visual_pair_route', compare)
    assert await svc.run_once(claim_kind='identity_visual')
    assert [(route, cid) for route, cid, _ in seen] == [('google', 'gate:a'), ('native', 'gate:b')]
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
    _, research = svc._identity_snapshot(story['id'])
    pairs = research['visual_search_operation']['parallel_pairs']
    assert pairs[0]['phase'] == 'submitted' and pairs[0]['id'] == seen[0][2]
    assert pairs[1]['phase'] == 'completed' and pairs[1]['id'] == seen[1][2]
