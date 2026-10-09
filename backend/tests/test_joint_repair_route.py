"""A closed malformed joint uses one registered alternative, never another unit."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
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


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [False, True])
async def test_existing_second_joint_uses_own_registered_quota_and_freezes_model_without_resend(tmp_path, lost):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='initial',
        gemini_web_search_tertiary_model='alternative')
    leases, calls = [], []

    class Executor:
        def __init__(self, name):
            self.name = name

        async def execute(self, role, call):
            assert not leases
            leases.append(self.name)
            try:
                return await call('offline-fixture', 5)
            finally:
                leases.remove(self.name)

    old_executor, new_executor = Executor('initial'), Executor('alternative')
    old_quota, new_quota = object(), object()
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
