"""An unsent regional TEXT admission gets one bounded retry outside key leases."""
import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from street_story import identity_discovery
from street_story.gemini import GeminiExecutor, GeminiKeyPool, GeminiPolicy
from street_story.identity_plan_diagnostics import joint_followup_marker
from street_story.providers import PermanentProviderError, RetryableProviderError
from street_story.quota import SharedQuotaDenied
from street_story.service import ConflictError, canonical
from test_architectural_text_identity import TEXT, text_inputs
from test_closed_initial_plan_reuse import current_snapshot, prepared_plan
from test_geometry_identity_plan import geometry_decision
from test_regional_catalogue_selection import html, inventory, offline, response, selection


def denial(delay=47.462, *, state='not_sent'):
    error = SharedQuotaDenied(delay)
    if state:
        error.provider_send_state = state
    return error


def setup(tmp_path, monkeypatch, error, *, wake=None, repeated=False, geometry=False):
    service, story, active, initial = prepared_plan(tmp_path, literal_addresses=True)
    service.settings = replace(service.settings, gemini_web_search_tertiary_model='fixture-model')
    story.update(latitude=54.7, longitude=20.5)
    story['_identity_search_context'] = {'reverse_address': {'city': 'Город', 'road': 'Тестовая улица'}}
    initial['regional_article_selections'] = [selection()]
    initial['accepted_geometry'] = geometry_decision()
    if not geometry:
        initial['accepted_geometry']['rejected_alternatives'][0]['candidate_id'] = 'osm:way:999'
    clock = [service.store.now()]
    monkeypatch.setattr(service.store, 'now', lambda: clock[0])
    policy = GeminiPolicy(call_timeout=.1, attempt_timeout=1)
    pool = GeminiKeyPool(service.store, (SecretStr('fixture-a'), SecretStr('fixture-b')), 'fixture-model',
        policy=policy, clock=lambda: clock[0])
    executor = GeminiExecutor(pool)
    requests, bodies, sdk_sends, waits = [], [], [], []
    original_sleep = asyncio.sleep

    async def handler(request):
        if request.url.path == '/sight/database.php':
            return response(inventory(34, 34, 1, next_page=False))
        assert request.url.path == '/sight/index.php' and b'sid=34' in request.url.query
        assert sum(pool._in_flight.values()) == 0, 'Initial SDK key must be released before article transport'
        bodies.append(request)
        # Ordinary article transport runs after the initial SDK lease is released.
        await original_sleep(.15)
        return response(html(f'<td style="text-align:justify">{TEXT}</td>'))
    offline(monkeypatch, handler)
    _, _, text_decision, _ = text_inputs(candidate_id='osm:way:2')
    text_decision = copy.deepcopy(text_decision)
    text_decision['article_bindings'][0]['article_id'] = 'prussia39:sid:34'
    text_decision['correspondences'][0]['article_id'] = 'prussia39:sid:34'

    async def generate(key, timeout, contents, config, **kwargs):
        assert sum(pool._in_flight.values()) == 1
        assert 0 < timeout <= policy.attempt_timeout
        requests.append((contents, config.model_dump(mode='json', exclude_none=True), kwargs))
        if len(requests) == 1:
            sdk_sends.append('initial')
            return SimpleNamespace(text=json.dumps(initial), response_id='original-closed')
        assert TEXT in contents[-1] and len(bodies) == 1
        if len(requests) == 2 or repeated:
            raise error  # Proven admission denial: no SDK send.
        sdk_sends.append('useful-text')
        return SimpleNamespace(text=json.dumps({**initial, 'accepted_architectural_text': text_decision}),
            response_id='useful-text-closed')

    async def wait(delay):
        assert sum(pool._in_flight.values()) == 0, 'No key lease may cover Retry-After'
        marker = joint_followup_marker(service, story)
        assert marker['phase'] == 'not_sent' and marker['admission_retry']['retry_count'] == 0
        assert marker['prepared_request']['prompt'] == requests[-1][0][-1]
        assert marker['prepared_request']['config'] == requests[-1][1]
        waits.append(delay)
        clock[0] += delay
        if wake:
            wake(service, story, clock)
    monkeypatch.setattr(identity_discovery.asyncio, 'sleep', wait)
    async def forbidden(*args, **kwargs):
        pytest.fail('A pending useful joint cannot dispatch another planner/REF/third judge')
    quota = object()
    service.providers.gemini = SimpleNamespace(executor=executor, _generate=generate,
        research_routes=[('fixture-model', pool, quota, executor)])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    return service, story, active, requests, bodies, sdk_sends, waits, pool


@pytest.mark.asyncio
async def test_actual_unsent_47_second_delay_releases_lease_and_compares_prussia_text_once(tmp_path, monkeypatch):
    service, story, active, requests, bodies, sends, waits, pool = setup(tmp_path, monkeypatch, denial())
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert waits == [47.462] and len(bodies) == 1 and sends == ['initial', 'useful-text']
    assert len(requests) == 3 and sum(pool._in_flight.values()) == 0
    assert requests[1] == requests[2]  # Same bytes/prompt/config/model/controller, ordinary re-admission.
    result = story['_identity_geometry_result']
    assert result['proof_kind'] == 'architectural_text' and result['candidate_id'] == 'osm:way:2'
    assert result['visual_reference_verified'] is False and result['_references_sent'] == []
    marker = joint_followup_marker(service, story)
    assert marker['phase'] == 'response_closed' and marker['admission_retry']['retry_count'] == 1
    frozen = marker['prepared_request']
    assert hashlib.sha256(canonical({k: v for k, v in frozen.items() if k != 'sha256'}).encode()).hexdigest() == frozen['sha256']
    budget = service._identity_snapshot(story['id'])[1]['research_budget']
    assert len(budget['work_units']['planner_calls']) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('delay', [0, 61, float('nan'), float('inf'), 'missing'])
async def test_unbounded_or_invalid_retry_delay_preserves_closed_initial_without_wait(tmp_path, monkeypatch, delay):
    service, story, active, requests, _, sends, waits, _ = setup(tmp_path, monkeypatch, denial(delay))
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(requests) == 2 and sends == ['initial'] and not waits
    assert '_identity_geometry_result' not in story
    assert story['_identity_search_plan_payload']['source_text_receipt']['provider_send_state'] == 'not_sent'


@pytest.mark.asyncio
async def test_only_one_unsent_retry_then_restart_reuses_original_without_send_or_acquisition(tmp_path, monkeypatch):
    service, story, active, requests, bodies, sends, waits, _ = setup(tmp_path, monkeypatch, denial(), repeated=True)
    original_active = copy.deepcopy(active)
    await identity_discovery.suggest(service, story, '', active)
    assert len(requests) == 3 and sends == ['initial'] and waits == [47.462]
    assert joint_followup_marker(service, story)['admission_retry']['retry_count'] == 1
    fresh = type(service)(service.settings, providers=service.providers)
    fresh.store.now = service.store.now
    await identity_discovery.prepare_search_plan(fresh, current_snapshot(fresh, story), '', original_active)
    assert len(requests) == 3 and len(bodies) == 1 and waits == [47.462]


@pytest.mark.asyncio
async def test_restart_during_admission_wait_does_not_resume_optional_retry(tmp_path, monkeypatch):
    def interrupt(*args):
        raise asyncio.CancelledError
    service, story, active, requests, bodies, sends, waits, pool = setup(
        tmp_path, monkeypatch, denial(), wake=interrupt)
    original_active = copy.deepcopy(active)
    with pytest.raises(asyncio.CancelledError):
        await identity_discovery.suggest(service, story, '', active)
    marker = joint_followup_marker(service, story)
    assert marker['phase'] == 'not_sent' and marker['admission_retry']['retry_count'] == 0
    assert sum(pool._in_flight.values()) == 0
    fresh = type(service)(service.settings, providers=service.providers)
    fresh.store.now = service.store.now
    await identity_discovery.prepare_search_plan(fresh, current_snapshot(fresh, story), '', original_active)
    assert len(requests) == 2 and len(bodies) == 1 and sends == ['initial'] and waits == [47.462]


@pytest.mark.asyncio
async def test_delayed_wake_rechecks_operation_headroom_before_reacquiring_key(tmp_path, monkeypatch):
    def delayed(service, story, clock):
        budget = service._identity_snapshot(story['id'])[1]['research_budget']
        clock[0] = budget['identity_deadline_at'] - .05
    service, story, active, requests, _, sends, waits, pool = setup(
        tmp_path, monkeypatch, denial(), wake=delayed)
    await identity_discovery.suggest(service, story, '', active)
    assert len(requests) == 2 and sends == ['initial'] and waits == [47.462]
    assert sum(pool._in_flight.values()) == 0
    assert joint_followup_marker(service, story)['phase'] == 'not_sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', [None, 'response_closed'])
async def test_unknown_or_closed_provider_failure_never_uses_unsent_retry(tmp_path, monkeypatch, state):
    service, story, active, requests, _, sends, waits, _ = setup(tmp_path, monkeypatch, denial(state=state))
    with pytest.raises(SharedQuotaDenied):
        await identity_discovery.suggest(service, story, '', active)
    assert len(requests) == 2 and sends == ['initial'] and not waits
    marker = joint_followup_marker(service, story)
    assert marker['phase'] == ('unknown' if state is None else 'closed_failure')
    with pytest.raises((RetryableProviderError, PermanentProviderError)):
        await identity_discovery.suggest(service, current_snapshot(service, story), '', active)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_retry_delay_plus_operation_timeout_must_fit_original_upload_deadline(tmp_path, monkeypatch):
    service, story, active, requests, _, sends, waits, _ = setup(tmp_path, monkeypatch, denial())
    with service.store.tx() as db:
        db.execute('UPDATE stories SET created_at=? WHERE id=?', (service.store.now() - 179, story['id']))
    await identity_discovery.suggest(service, story, '', active)
    assert len(requests) == 2 and sends == ['initial'] and not waits
    assert joint_followup_marker(service, story)['phase'] == 'not_sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['photo', 'generation', 'control', 'source_bytes', 'schema'])
async def test_scoped_or_frozen_request_change_during_wait_cannot_dispatch(tmp_path, monkeypatch, change):
    def wake(service, story, clock):
        if change == 'source_bytes':
            service._source_photo_bytes = lambda _: b'changed-original'
        elif change == 'schema':
            # The immutable prepared contract cannot be replaced under its hash.
            with service.store.tx() as db:
                row = service._story_row(db, story['id'])
                research = json.loads(row['research_json'])
                research['identity_joint_followup']['prepared_request']['schema']['required'] = []
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        else:
            with service.store.tx() as db:
                row = service._story_row(db, story['id'])
                research = json.loads(row['research_json'])
                if change == 'photo':
                    db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('f' * 64, story['id']))
                elif change == 'generation':
                    research['identity_generation'] = 1
                else:
                    research['research_controls'] = {'identity': {'photo_sha256': story['photo_sha256'],
                        'identity_generation': 0, 'revision': 1}}
                db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    service, story, active, requests, _, sends, waits, _ = setup(tmp_path, monkeypatch, denial(), wake=wake)
    with pytest.raises((ConflictError, PermanentProviderError, RetryableProviderError)):
        await identity_discovery.suggest(service, story, '', active)
    assert len(requests) == 2 and sends == ['initial'] and waits == [47.462]


@pytest.mark.asyncio
async def test_accepted_initial_geometry_never_acquires_body_or_waits_for_text_admission(tmp_path, monkeypatch):
    service, story, active, requests, bodies, sends, waits, _ = setup(tmp_path, monkeypatch, denial(), geometry=True)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(requests) == 1 and sends == ['initial'] and not bodies and not waits
    assert story['_identity_geometry_result']['proof_kind'] == 'geometry'
