import copy
import json

import pytest
from jsonschema import Draft202012Validator

from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import identity_text_discovery_schema
from test_opencode_research import Harness
from test_autonomous_live_research import SCHEMA, binding, client
from test_mvp_research import service


def test_discovery_schema_excludes_image_verdicts_and_repeated_inventory_enums():
    ids = [f'osm:way:{index}' for index in range(400)]
    pointer = {'type': 'string', 'enum': ids}
    joint = {'type': 'object', 'properties': {
        'observed_candidate_ids': {'type': 'array', 'maxItems': 6, 'items': pointer},
        'first_wave_hypotheses': {'type': 'array', 'maxItems': 3, 'items': {
            'type': 'object', 'properties': {'subject_id': pointer,
                'kind': {'type': 'string', 'enum': ['address', 'appearance']}},
            'required': ['subject_id', 'kind'], 'additionalProperties': False}},
        'accepted_geometry': {'type': 'object'}, 'accepted_architectural_text': {'type': 'object'},
        'source_scene_observations': {'type': 'object'}, 'spatial_hypotheses': {'type': 'array'}},
        'required': ['first_wave_hypotheses', 'source_scene_observations']}
    original = copy.deepcopy(joint)
    role = identity_text_discovery_schema(joint)
    validator = Draft202012Validator(role)
    result = {'first_wave_hypotheses': [{'subject_id': ids[-1], 'kind': 'address'}]}
    assert validator.is_valid(result)
    assert not validator.is_valid({**result, 'accepted_geometry': {}})
    assert not validator.is_valid({**result, 'source_scene_observations': {}})
    assert role['required'] == ['first_wave_hypotheses']
    assert len(json.dumps(role)) < 1200
    assert 'osm:way:' not in json.dumps(role)
    assert joint == original  # Joint visual host schema retains exact enums.


@pytest.mark.asyncio
async def test_all_oversized_planner_routes_close_unchanged_unit_without_quota_or_resend(tmp_path):
    from types import SimpleNamespace
    from street_story.errors import PermanentProviderError
    from street_story.research_adapter import ProductResearchAdapter
    from test_research_control import fixture
    svc, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = svc
    calls = []
    provider = SimpleNamespace(endpoint='http://existing-controlled-native',
        provider_id='opencode', model_id='qualified-controlled-text', directory=None)
    async def refuse(prompt, current, schema):
        calls.append(current)
        receipt = {'binding': dict(current), 'phase': 'failed',
            'provider_id': provider.provider_id, 'model_id': provider.model_id,
            'provider_send_state': 'not_sent', 'error_code': 'research_input_too_large',
            'frozen_prompt': prompt, 'frozen_schema': schema}
        await adapter.checkpoint(current, receipt)
        raise ResearchUnavailable('research_input_too_large', receipt)
    provider.plan_identity_search = refuse
    adapter._fact_pool_routes = lambda: [{'provider_id': provider.provider_id,
        'model_id': provider.model_id, 'endpoint': provider.endpoint, 'client': provider,
        'qualified': True, 'available': True}]
    story = {'id': sid, 'photo_sha256': photo}
    for _ in range(2):
        with pytest.raises(PermanentProviderError, match='identity_search_plan_input_oversize'):
            await adapter.plan_identity_search(story, 'Unchanged oversized operation', {'type': 'object'})
    assert len(calls) == 1
    assert svc.store.cache_get('research-quota-health:opencode:qualified-controlled-text') is None
    with svc.store.connection() as db:
        rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts'))
    assert len(rows) == 1
    assert json.loads(rows[0][0])['provider_send_state'] == 'not_sent'


@pytest.mark.asyncio
async def test_native_attested_system_overflow_precedes_admission_and_session_creation():
    h = Harness()
    h.config['agent']['street-story-facts']['prompt'] = 's'*24000
    try:
        with pytest.raises(ResearchUnavailable) as error:
            await h.adapter()._run('facts', 'Small capsule', {'request_id': 'system-overflow'}, {'type': 'object'})
        assert error.value.code == 'research_input_too_large'
        assert error.value.receipt['provider_send_state'] == 'not_sent'
        assert error.value.receipt['input_utf8_bytes'] > 24000
        assert h.admissions == h.sends == []
        assert all(method == 'GET' for method, _, _ in h.requests)
        assert h.checkpoints[-1][1]['error_code'] == 'research_input_too_large'
    finally:
        await h.client.aclose()


@pytest.mark.asyncio
async def test_shared_native_uses_its_existing_attested_agent_prompt(tmp_path):
    from test_shared_devcoveer_research import setup
    h, backend, provider = setup(tmp_path)
    h.config['agent']['plan']['prompt'] = 's'*24000
    try:
        with pytest.raises(ResearchUnavailable, match='research_input_too_large'):
            await provider._run('facts', 'Small capsule', {'request_id': 'shared-system-overflow'}, {'type': 'object'})
        assert h.admissions == h.sends == []
        assert all(method == 'GET' for method, *_ in backend.calls)
    finally:
        await h.client.aclose()


@pytest.mark.asyncio
async def test_shared_native_checks_actual_ascii_encoded_request_bytes(tmp_path):
    from test_shared_devcoveer_research import setup
    h, backend, provider = setup(tmp_path)
    try:
        # The decoded Cyrillic capsule is 8KB UTF-8, but the actual shared
        # backend JSON request escapes each character as six ASCII bytes.
        with pytest.raises(ResearchUnavailable, match='research_input_too_large') as error:
            await provider._run('facts', 'ж'*4000, {'request_id': 'shared-wire-overflow'}, {'type': 'object'})
        assert error.value.receipt['input_utf8_bytes'] > 24000
        assert h.admissions == h.sends == []
        assert backend.calls == []
    finally:
        await h.client.aclose()


@pytest.mark.asyncio
async def test_live_full_schema_setup_overflow_never_opens_host(tmp_path):
    svc, _, story = service(tmp_path)
    provider, calls = client(svc, 'valid')
    large_schema = {**SCHEMA, 'description': 'schema content '*1900}
    with pytest.raises(ResearchUnavailable) as error:
        await provider._run('facts', 'Small capsule', binding(svc, story), large_schema)
    assert error.value.code == 'live_research_unit_oversize'
    assert error.value.receipt['input_utf8_bytes'] > 24000
    assert error.value.receipt['provider_send_state'] == 'not_sent'
    assert calls == []
    with svc.store.connection() as db:
        saved = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0])
    assert saved['input_utf8_bytes'] == error.value.receipt['input_utf8_bytes']
    # UNKNOWN precedes fresh preflight and cannot authorize a new socket.
    with pytest.raises(ResearchUnavailable, match='original_outcome_unknown'):
        await provider._run('facts', 'Small capsule', {**saved['binding'], 'phase': 'unknown'}, large_schema)
    assert calls == []


@pytest.mark.asyncio
async def test_live_receipt_matches_shared_wire_setup_and_trigger(tmp_path):
    from live_interaction.provider import setup_config
    from street_story.live_research import TRIGGER
    svc, _, story = service(tmp_path)
    provider, _ = client(svc, 'valid')
    captured = {}
    original = provider.host_factory
    class CaptureHost(original):
        async def start(self, **kwargs):
            result = await super().start(**kwargs)
            captured.update(self.initialized)
            return result
    provider.host_factory = CaptureHost
    result = await provider._run('facts', 'Сведения из источника', binding(svc, story), SCHEMA)
    setup = setup_config(provider.model_id, captured['context'], configuration=captured['configuration'], search=False)
    trigger = {'clientContent': {'turns': [{'role': 'user', 'parts': [{'text': TRIGGER}]}], 'turnComplete': True}}
    assert result['receipt']['input_utf8_bytes'] == len(json.dumps(setup).encode()) + len(json.dumps(trigger).encode())
