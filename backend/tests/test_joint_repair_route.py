"""A closed malformed joint uses one registered alternative, never another unit."""
import json
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.gemini import GeminiUnavailable
from street_story.providers import RetryableProviderError
from test_geometry_identity_plan import geometry_decision, geometry_setup, payload


@pytest.mark.parametrize('issues,registered,preferred,expected', [
    ({'schema_validation': {}}, True, 'alternative', 'alternative'),
    ({}, True, 'alternative', 'initial'),
    ({'schema_validation': {}}, False, 'alternative', 'initial'),
    ({'schema_validation': {}}, True, 'initial', 'initial'),
])
def test_route_change_requires_closed_contract_issues_and_registered_tuple(issues, registered, preferred, expected):
    old_quota, old_executor, new_quota, new_executor = (object() for _ in range(4))
    gemini = SimpleNamespace(web_search_routes=[('alternative', object(), new_quota, new_executor)] if registered else [])
    result = identity_discovery._closed_invalid_followup_route(
        SimpleNamespace(gemini_web_search_tertiary_model=preferred), gemini, issues,
        'initial', old_quota, old_executor)
    assert result == ((expected, new_quota, new_executor) if expected == 'alternative'
        else (expected, old_quota, old_executor))


@pytest.mark.parametrize('unavailable', [False, True])
def test_source_text_comparison_prefers_registered_reasoning_without_reopening_initial(unavailable):
    initial = ('initial', object(), object(), object())
    reasoning = ('reasoning', object(), object(), object())
    selected = identity_discovery._closed_invalid_followup_route(
        SimpleNamespace(gemini_web_search_tertiary_model='reasoning'),
        SimpleNamespace(web_search_routes=[initial, reasoning]), {},
        initial[0], initial[2], initial[3], architectural_comparison=True,
        unavailable_models={'reasoning'} if unavailable else set())
    route = initial if unavailable else reasoning
    assert selected == (route[0], route[2], route[3])


@pytest.mark.parametrize('scene_available,preferred', [(True, 'alternative'), (False, 'initial')])
def test_joint_visual_role_prefers_configured_registered_model_without_changing_its_tuple(scene_available, preferred):
    first = ('initial', object(), object(), object())
    alternative = ('alternative', object(), object(), object())
    settings = SimpleNamespace(gemini_web_search_model='initial', gemini_web_search_tertiary_model='alternative')
    gemini = SimpleNamespace(web_search_routes=[first, alternative], research_routes=[first])
    routes = identity_discovery._joint_initial_routes(settings, gemini, scene_available=scene_available)
    assert len(routes) == 2 and routes[0][0] == preferred
    assert routes[0] is (alternative if scene_available else first)
    assert identity_discovery._joint_initial_routes(settings, SimpleNamespace(), scene_available=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('registered', [False, True])
async def test_all_unsent_visual_routes_preserve_independent_text_search(tmp_path, registered):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    calls = []
    retry_at = service.store.now() + 77023

    class Unavailable:
        async def execute(self, role, call):
            calls.append('visual_admission')
            raise GeminiUnavailable(retry_at, 'fixture_rpd_not_sent')

    async def generate(*args, **kwargs):
        pytest.fail('No SDK invocation after unavailable visual-role admission')

    async def independent(snapshot, prompt, schema):
        calls.append('independent_text')
        assert 'SOURCE and MAP images are unavailable' in prompt
        assert 'osm:way:2' in prompt and 'osm:way:3' in prompt
        return {'result': {key: value for key, value in payload(geometry_decision()).items()
            if key != 'accepted_geometry'}}

    service.providers.gemini = SimpleNamespace(executor=Unavailable(), _generate=generate,
        web_search_routes=[('initial', object(), object(), Unavailable()),
            *([('alternative', object(), object(), Unavailable())] if registered else [])],
        research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=independent)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == (['visual_admission', 'visual_admission', 'independent_text'] if registered
                     else ['visual_admission', 'independent_text'])
    assert story['_identity_search_plan_route'] == 'qualified_text_fallback'
    assert '_identity_geometry_result' not in story


@pytest.mark.asyncio
async def test_visual_quota_cause_remains_observable_when_no_independent_planner(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_tertiary_model='alternative')
    retry_at = service.store.now() + 77023

    class Unavailable:
        async def execute(self, role, call):
            raise GeminiUnavailable(retry_at, 'fixture_rpd_not_sent')

    async def forbidden(*args, **kwargs):
        pytest.fail('No SDK call')

    service.providers.gemini = SimpleNamespace(executor=Unavailable(), _generate=forbidden,
        web_search_routes=[('alternative', object(), object(), Unavailable())])
    service.providers.research = None
    with pytest.raises(GeminiUnavailable, match='identity_visual_model_unavailable') as failure:
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert failure.value.retry_at == retry_at
    assert '_identity_geometry_result' not in story


@pytest.mark.asyncio
@pytest.mark.parametrize('reserve', ['google', 'native', 'native_denied'])
@pytest.mark.parametrize('primary_status', [None, 429, 503, 'unknown'])
async def test_unavailable_primary_uses_secondary_pixels_with_same_proof_contract(tmp_path, reserve, primary_status):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    calls, native_receipts = [], []
    quota = object()
    class Denied:
        async def execute(self, operation, call):
            calls.append('primary_unsent' if primary_status is None else
                'primary_unknown' if primary_status == 'unknown' else 'primary_closed_error')
            if primary_status is not None:
                return await call('fixture', 60)
            raise GeminiUnavailable(service.store.now() + 300, 'rpd_not_sent')
    class Allowed:
        async def execute(self, operation, call):
            calls.append('google_secondary')
            return await call('fixture', 60)
    async def generate(key, timeout, contents, config, **kwargs):
        if kwargs['model'] == 'alternative':
            if primary_status == 'unknown':
                raise TimeoutError('Original primary outcome unknown')
            from google.genai.errors import ClientError, ServerError
            error = ServerError if primary_status >= 500 else ClientError
            raise error(primary_status, {'error': {'code': primary_status, 'message': 'Fixture availability failure'}})
        assert kwargs['model'] == 'initial' and kwargs['quota'] is quota
        assert len(contents) == 3 and all(part.inline_data.data for part in contents[:2])
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def native(snapshot, prompt, schema, images, host_context):
        calls.append('native_secondary')
        assert [label for label, _mime, _data in images] == ['SOURCE', 'MAP']
        assert all(data for _label, _mime, data in images)
        assert host_context['source_map_receipt']['model_id'] == 'gpt-6-luna'
        result = payload(geometry_decision())
        native_receipts.append({'phase': 'completed', 'turn_id': 'original-turn'})
        if reserve == 'native_denied':
            native_receipts[-1] = {'phase': 'failed', 'provider_send_state': 'not_sent'}
            raise RetryableProviderError('native_quota_below_reserve')
        return {'result': result, 'receipt': native_receipts[-1], 'host_context': host_context}
    service.providers.gemini = SimpleNamespace(executor=Denied(), _generate=generate,
        web_search_routes=[('alternative', object(), object(), Denied()), ('initial', object(), quota, Allowed())])
    service.providers.research = SimpleNamespace(source_map_available=reserve != 'google',
        plan_source_map=native, source_map_receipt=lambda snapshot: native_receipts[-1] if native_receipts else None)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    first = ('primary_unsent' if primary_status is None else
        'primary_unknown' if primary_status == 'unknown' else 'primary_closed_error')
    assert calls == ([first, 'native_secondary', 'google_secondary'] if reserve == 'native_denied'
        else [first, 'native_secondary' if reserve == 'native' else 'google_secondary'])
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    assert story['_identity_search_plan_payload']['source_map_receipt']['model_id'] == (
        'gpt-6-luna' if reserve == 'native' else 'initial')
    if primary_status == 'unknown':
        from street_story.identity_plan_diagnostics import joint_operation_marker, addressed_joint_models
        marker = joint_operation_marker(service, story, stage='initial')
        original = marker['route_operations']['alternative']
        assert original['phase'] == 'unknown' and original['code'] == 'identity_joint_initial_unknown'
        assert original['scope'] == marker['scope'] and original['binding'] == marker['binding']
        assert 'alternative' in addressed_joint_models(marker)
    elif primary_status is not None:
        from street_story.identity_plan_diagnostics import joint_operation_marker
        marker = joint_operation_marker(service, story, stage='initial')
        assert marker['closed_route_failures'][0]['model_id'] == 'alternative'
        assert marker['closed_route_failures'][0]['status_code'] == primary_status


@pytest.mark.asyncio
@pytest.mark.parametrize('native_phase', ['unknown', 'failed'])
async def test_unavailable_native_readback_does_not_block_remaining_google_route(tmp_path, native_phase):
    from street_story.identity_plan_diagnostics import joint_operation_marker
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    calls, receipts, unknowns = [], [], []
    class Primary:
        async def execute(self, role, call):
            calls.append('primary')
            return await call('fixture', 60)
    class Reserve:
        available = False
        async def execute(self, role, call):
            calls.append('reserve')
            if not self.available:
                raise GeminiUnavailable(service.store.now() + 60, 'fixture_unsent')
            return await call('fixture', 60)
    reserve = Reserve()
    async def generate(key, timeout, contents, config, **kwargs):
        if kwargs['model'] == 'alternative':
            raise TimeoutError('Original Google UNKNOWN')
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def native(current, prompt, schema, images, host_context):
        marker = joint_operation_marker(service, current, stage='initial')
        if not receipts:
            calls.append('native_send')
            receipts.append({'phase': native_phase, 'turn_id': 'original-native-turn'})
        else:
            calls.append('native_readback')
            unknowns.append(copy.deepcopy(marker['route_operations']['gpt-6-luna']))
        raise RetryableProviderError('native_original_pending')
    service.providers.gemini = SimpleNamespace(executor=Primary(), _generate=generate,
        web_search_routes=[('alternative', object(), object(), Primary()),
            ('initial', object(), object(), reserve)])
    service.providers.research = SimpleNamespace(source_map_available=True, plan_source_map=native,
        source_map_receipt=lambda _: receipts[-1] if receipts else None)
    with pytest.raises(GeminiUnavailable):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    before = joint_operation_marker(service, story, stage='initial')
    reserve.available = True
    fresh = type(service)(service.settings, providers=service.providers)
    await identity_discovery.prepare_search_plan(fresh, story, '', active)
    assert calls == ['primary', 'native_send', 'reserve', 'native_readback', 'reserve']
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    after = joint_operation_marker(fresh, story, stage='initial')
    assert after['route_operations']['alternative'] == before['route_operations']['alternative']
    assert unknowns[0]['binding'] == after['route_operations']['gpt-6-luna']['binding']
    assert after['route_operations']['gpt-6-luna']['phase'] == (
        'unknown' if native_phase == 'unknown' else 'closed_failure')


@pytest.mark.asyncio
async def test_restart_skips_model_with_received503_and_reassigns_same_bound_source_map(tmp_path):
    from google.genai.errors import ServerError
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    calls = []
    class Primary:
        async def execute(self, role, call):
            calls.append('failed_model')
            return await call('fixture', 60)
    class Reserve:
        available = False
        async def execute(self, role, call):
            calls.append('reserve')
            if not self.available:
                raise GeminiUnavailable(service.store.now() + 60, 'fixture_unsent')
            return await call('fixture', 60)
    reserve = Reserve()
    async def generate(key, timeout, contents, config, **kwargs):
        if kwargs['model'] == 'alternative':
            raise ServerError(503, {'error': {'code': 503, 'message': 'Fixture unavailable'}})
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    service.providers.gemini = SimpleNamespace(executor=Primary(), _generate=generate,
        web_search_routes=[('alternative', object(), object(), Primary()),
            ('initial', object(), object(), reserve)])
    service.providers.research = None
    with pytest.raises(GeminiUnavailable):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    reserve.available = True
    fresh = type(service)(service.settings, providers=service.providers)
    await identity_discovery.prepare_search_plan(fresh, story, '', active)
    assert calls == ['failed_model', 'reserve', 'reserve']
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [False, True])
async def test_existing_second_joint_uses_own_registered_quota_and_freezes_model_without_resend(tmp_path, monkeypatch, lost):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    leases, calls = [], []

    class Executor:
        def __init__(self, name):
            self.name = name
            self.admissions = 0

        async def execute(self, role, call):
            self.admissions += 1
            assert not leases
            leases.append(self.name)
            try:
                return await call('offline-fixture', 5)
            finally:
                leases.remove(self.name)

    old_executor, new_executor = Executor('initial'), Executor('alternative')
    old_quota, new_quota = object(), object()
    # Isolate the existing closed-contract repair from initial role selection.
    # A legacy initial operation can still require this one addressed repair;
    # unavailability no longer authorizes sending SOURCE/MAP to the text route.
    monkeypatch.setattr(identity_discovery, '_joint_initial_routes', lambda *args, **kwargs:
        [('initial', object(), old_quota, old_executor)])
    bad = geometry_decision()
    bad['rejected_alternatives'][0]['candidate_id'] = 'osm:way:999'

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append((kwargs, contents))
        if len(calls) == 1:
            assert leases == ['initial'] and kwargs['quota'] is old_quota
            return SimpleNamespace(text=json.dumps(payload(bad)))
        assert len(calls) == 2
        assert leases == ['alternative'] and kwargs['model'] == 'alternative'
        assert kwargs['quota'] is new_quota
        assert contents[0].inline_data.data == calls[0][1][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1][1].inline_data.data
        if lost:
            raise TimeoutError('original alternative outcome unknown')
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))

    async def forbidden(*args, **kwargs):
        pytest.fail('No third judge or replacement planner is permitted')

    service.providers.gemini = SimpleNamespace(executor=old_executor, _generate=generate,
        research_routes=[('initial', object(), old_quota, old_executor)],
        web_search_routes=[('alternative', object(), new_quota, new_executor)])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    if lost:
        with pytest.raises(TimeoutError):
            await identity_discovery.prepare_search_plan(service, story, '', active)
        with pytest.raises(RetryableProviderError, match='identity_joint_followup_outcome_unknown'):
            await identity_discovery.prepare_search_plan(service, story, '', active)
    else:
        await identity_discovery.prepare_search_plan(service, story, '', active)
        assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
    receipt = research['identity_joint_followup']
    assert receipt['prepared_request']['model'] == 'alternative'
    assert receipt['phase'] == ('unknown' if lost else 'response_closed')
    assert len(calls) == 2 and not leases
