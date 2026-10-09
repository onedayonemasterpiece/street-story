"""A closed malformed joint uses one registered alternative, never another unit."""
import json
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


@pytest.mark.parametrize('scene_available,preferred', [(True, 'alternative'), (False, 'initial')])
def test_joint_visual_role_prefers_configured_registered_model_without_changing_its_tuple(scene_available, preferred):
    first = ('initial', object(), object(), object())
    alternative = ('alternative', object(), object(), object())
    settings = SimpleNamespace(gemini_web_search_model='initial', gemini_web_search_tertiary_model='alternative')
    gemini = SimpleNamespace(web_search_routes=[first, alternative], research_routes=[first])
    routes = identity_discovery._joint_initial_routes(settings, gemini, scene_available=scene_available)
    assert len(routes) == (1 if scene_available else 2) and routes[0][0] == preferred
    assert routes[0] is (alternative if scene_available else first)
    assert identity_discovery._joint_initial_routes(settings, SimpleNamespace(), scene_available=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('registered', [False, True])
async def test_unavailable_visual_role_preserves_independent_text_search_without_lightweight_spatial_send(tmp_path, registered):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    calls = []
    retry_at = service.store.now() + 77023

    class Unavailable:
        async def execute(self, role, call):
            calls.append('visual_admission')
            raise GeminiUnavailable(retry_at, 'fixture_rpd_not_sent')

    class Forbidden:
        async def execute(self, *args, **kwargs):
            pytest.fail('SOURCE/MAP must not silently fall back to a lightweight text model')

    async def generate(*args, **kwargs):
        pytest.fail('No SDK invocation after unavailable visual-role admission')

    async def independent(snapshot, prompt, schema):
        calls.append('independent_text')
        assert 'SOURCE and MAP images are unavailable' in prompt
        assert 'osm:way:2' in prompt and 'osm:way:3' in prompt
        return {'result': {key: value for key, value in payload(geometry_decision()).items()
            if key != 'accepted_geometry'}}

    service.providers.gemini = SimpleNamespace(executor=Forbidden(), _generate=generate,
        web_search_routes=[('initial', object(), object(), Forbidden()),
            *([('alternative', object(), object(), Unavailable())] if registered else [])],
        research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=independent)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == (['visual_admission', 'independent_text'] if registered else ['independent_text'])
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
