"""Known-unsent optional TEXT work preserves an original valid model plan."""
import copy
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from street_story import identity_architectural_context, identity_discovery
from street_story.identity_plan_diagnostics import joint_operation_marker
from street_story.providers import PermanentProviderError, RetryableProviderError
from test_architectural_text_identity import text_inputs
from test_geometry_identity_plan import Executor, geometry_setup, geometry_decision, payload


def prepared_plan(tmp_path, *, literal_addresses=False):
    service, story, active = geometry_setup(tmp_path)
    if literal_addresses:
        for candidate in [*story['_identity_observed_candidates'], *active]:
            if candidate['candidate_id'] in {'osm:way:2', 'osm:way:3'}:
                candidate['map_address'] = {'city': 'Город', 'street': 'Тестовая улица',
                    'house_number': '6' if candidate['candidate_id'] == 'osm:way:2' else '6А'}
    story['_identity_wikipedia_metadata'] = [{'pageid': 99, 'title': 'Received building article',
        'thumbnail_url': 'https://example.org/received-facade.jpg'}]
    active.append({'candidate_id': 'wiki:99', 'name': 'Received building article',
        'reference_image_urls': ['https://example.org/received-facade.jpg']})
    uncertain = geometry_decision()
    uncertain['decision'] = 'uncertain'
    initial = payload(uncertain)
    initial['selected_wikipedia_page_ids'] = ['99']
    initial['subject_article_bindings'] = [{'article_id': 'wiki:99', 'candidate_id': 'osm:way:2',
        'scope': 'Main physical body', 'binding_basis': 'Actual received building article and facade lead',
        'physical_binding_resolved': True}]
    return service, story, active, initial


def current_snapshot(service, seed):
    return {**seed, **service._identity_snapshot(seed['id'])[0]}


def install(service, initial, monkeypatch, *, phase='not_sent'):
    calls, acquisitions = [], []
    _, _, _, receipt = text_inputs(candidate_id='osm:way:2')
    articles = [{**receipt['articles'][0], 'article_id': 'wiki:99'}]
    async def acquire(svc, story, candidates, received_payload, pages):
        acquisitions.append(received_payload['selected_wikipedia_page_ids'])
        marker = joint_operation_marker(svc, story, stage='initial')
        if received_payload.get('selected_wikipedia_page_ids'):
            assert marker.get('closed_plan')  # Committed before any selected body read.
            return articles, {'status': 'completed'}
        return [], {}
    monkeypatch.setattr(identity_architectural_context, 'acquire_selected_wikipedia_text', acquire)
    async def generate(*args, **kwargs):
        calls.append('initial' if not calls else 'followup')
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial), response_id='original-closed-response')
        error = RetryableProviderError('offline_followup_admission' if phase == 'not_sent' else 'offline_followup_unknown')
        if phase == 'not_sent':
            error.provider_send_state = 'not_sent'
        raise error
    async def forbidden(*args, **kwargs):
        pytest.fail('Reuse must not open another semantic planner')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    return calls, acquisitions


@pytest.mark.asyncio
async def test_unsent_text_followup_preserves_closed_wiki_plan_and_normal_visual_queue(tmp_path, monkeypatch):
    service, story, active, initial = prepared_plan(tmp_path)
    calls, acquired = install(service, initial, monkeypatch)
    result, candidates = await identity_discovery.recover(service, story, '', active, set())
    assert calls == ['initial', 'followup'] and acquired == [['99']]
    assert result['status'] == 'uncertain' and result['_comparison_deferred'] is True
    assert any(item['candidate_id'] == 'wiki:99' and item['reference_image_urls'] for item in candidates)
    research = service._identity_snapshot(story['id'])[1]
    plan = research['identity_article_discovery']['search_plan']['payload']
    assert plan['selected_wikipedia_page_ids'] == ['99']
    assert plan['subject_article_bindings'] == initial['subject_article_bindings']
    assert not plan.get('geometry_proof') and not plan.get('architectural_text_proof')
    assert plan['source_text_receipt']['provider_send_state'] == 'not_sent'
    assert plan['source_text_receipt']['source_image_input'] is False
    assert research['identity_joint_followup']['phase'] == 'not_sent'
    saved = research['identity_joint_initial']['closed_plan']
    assert json.loads(saved['raw_json']) == initial == saved['payload']
    assert 'accepted_architectural_text' not in saved['schema']['properties']


@pytest.mark.asyncio
async def test_restart_before_search_plan_checkpoint_reuses_original_without_new_send_or_body(tmp_path, monkeypatch):
    service, story, active, initial = prepared_plan(tmp_path)
    original_candidates = copy.deepcopy(active)
    calls, acquired = install(service, initial, monkeypatch)
    await identity_discovery.suggest(service, story, '', active)
    before = service._identity_snapshot(story['id'])[1]['identity_joint_initial']['closed_plan']
    fresh = type(service)(service.settings, providers=service.providers)
    snapshot = current_snapshot(fresh, story)
    history, _ = await identity_discovery.prepare_search_plan(fresh, snapshot, '', original_candidates)
    assert calls == ['initial', 'followup'] and acquired == [['99']]
    assert history['search_plan']['payload']['selected_wikipedia_page_ids'] == ['99']
    assert fresh._identity_snapshot(story['id'])[1]['identity_joint_initial']['closed_plan'] == before
    assert '_identity_geometry_result' not in snapshot


@pytest.mark.asyncio
async def test_restart_cached_full_regional_inventory_keeps_identical_joint_input_without_http(tmp_path, monkeypatch):
    from test_regional_catalogue_selection import inventory, offline, response
    service, story, active, initial = prepared_plan(tmp_path, literal_addresses=True)
    story['_identity_search_context'] = {'reverse_address': {'city': 'Город', 'road': 'Тестовая улица'}}
    http_calls = []
    def handler(request):
        http_calls.append(request)
        assert len(http_calls) <= 2, 'A known-unsent wake must use the received catalogue cache only'
        return response(inventory(21, 35, next_page=False) if b'p=2' in request.url.query else inventory())
    offline(monkeypatch, handler)
    original_candidates = copy.deepcopy(active)
    calls, _ = install(service, initial, monkeypatch)
    await identity_discovery.suggest(service, story, '', active)
    assert len(http_calls) == 2 and len(story['_identity_regional_catalogue']['results']) == 35
    fresh = type(service)(service.settings, providers=service.providers)
    snapshot = current_snapshot(fresh, story)
    snapshot.pop('_identity_regional_catalogue')  # Rebuild from actual saved publisher bytes.
    await identity_discovery.prepare_search_plan(fresh, snapshot, '', original_candidates)
    assert len(http_calls) == 2 and calls == ['initial', 'followup']
    assert len(snapshot['_identity_search_plan_payload']['regional_catalogue']['results']) == 35


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'camera', 'context', 'configuration', 'raw_response', 'schema'])
async def test_original_plan_reuse_requires_identical_source_context_configuration_and_receipt(tmp_path, monkeypatch, change):
    service, story, active, initial = prepared_plan(tmp_path)
    original_candidates = copy.deepcopy(active)
    calls, _ = install(service, initial, monkeypatch)
    await identity_discovery.suggest(service, story, '', active)
    fresh = type(service)(service.settings, providers=service.providers)
    snapshot = current_snapshot(fresh, story)
    transcript = ''
    if change == 'source':
        buffer = io.BytesIO()
        Image.new('RGB', (12, 16), 'navy').save(buffer, format='PNG')
        fresh._source_photo_bytes = lambda _: buffer.getvalue()
    elif change == 'camera':
        snapshot['_camera_hints'] = {'position_basis': 'owner_approx_camera', 'heading_degrees': 22}
    elif change == 'context':
        transcript = 'New author observation changes the exact original input'
    elif change == 'configuration':
        fresh.settings = replace(fresh.settings, gemini_web_search_model='another-configured-model')
    else:
        with fresh.store.tx() as db:
            row = fresh._story_row(db, story['id'])
            research = json.loads(row['research_json'])
            saved = research['identity_joint_initial']['closed_plan']
            if change == 'raw_response':
                saved['raw_json'] = '{}'
            else:
                saved['schema']['required'] = []
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    with pytest.raises(PermanentProviderError, match='identity_joint_initial_closed_plan_unavailable'):
        await identity_discovery.suggest(fresh, snapshot, transcript, original_candidates)
    assert calls == ['initial', 'followup']


@pytest.mark.asyncio
async def test_unknown_followup_still_fences_valid_initial_across_restart(tmp_path, monkeypatch):
    service, story, active, initial = prepared_plan(tmp_path)
    calls, _ = install(service, initial, monkeypatch, phase='unknown')
    with pytest.raises(RetryableProviderError, match='offline_followup_unknown'):
        await identity_discovery.suggest(service, story, '', active)
    assert service._identity_snapshot(story['id'])[1]['identity_joint_initial']['closed_plan']
    fresh = type(service)(service.settings, providers=service.providers)
    with pytest.raises(RetryableProviderError, match='identity_joint_followup_outcome_unknown'):
        await identity_discovery.suggest(fresh, current_snapshot(fresh, story), '', active)
    assert calls == ['initial', 'followup']


@pytest.mark.asyncio
async def test_invalid_geometry_keeps_independent_valid_nominees_without_identity_proof(tmp_path, monkeypatch):
    service, story, active, initial = prepared_plan(tmp_path)
    initial['accepted_geometry'] = geometry_decision()
    initial['accepted_geometry']['candidate_label'] = 2  # The other physical body.
    calls, _ = install(service, initial, monkeypatch)
    await identity_discovery.suggest(service, story, '', active)
    saved = story['_identity_search_plan_payload']
    assert saved['selected_wikipedia_page_ids'] == ['99']
    assert 'accepted_geometry' not in saved and not saved.get('geometry_proof')
    assert saved['rejected_geometry']['decision'] == initial['accepted_geometry']
    assert '_identity_geometry_result' not in story
    assert calls == ['initial', 'followup']


@pytest.mark.asyncio
async def test_sole_invalid_geometry_cannot_authorize_initial_plan_reuse(tmp_path, monkeypatch):
    service, story, active, initial = prepared_plan(tmp_path)
    initial['selected_wikipedia_page_ids'] = []
    initial['subject_article_bindings'] = []
    initial['accepted_geometry'] = geometry_decision()
    initial['accepted_geometry']['candidate_label'] = 2
    calls, _ = install(service, initial, monkeypatch)
    with pytest.raises(RetryableProviderError, match='offline_followup_admission'):
        await identity_discovery.suggest(service, story, '', active)
    assert not service._identity_snapshot(story['id'])[1]['identity_joint_initial'].get('closed_plan')
    assert '_identity_geometry_result' not in story
    assert calls == ['initial', 'followup']
