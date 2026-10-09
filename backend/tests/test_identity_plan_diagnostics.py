"""Closed invalid joint answers stay inspectable without weakening acceptance."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.identity_plan_diagnostics import (
    RAW_JSON_BYTES, RAW_PREFIX_BYTES, VALIDATION_ERRORS, joint_followup_marker, retain_closed_invalid, validation_details,
)
from street_story.providers import PermanentProviderError
from street_story.service import ConflictError, canonical
from test_observed_address_search_context import observed
from test_structured_identity_first_wave import choice, payload
from test_visual_search_continuation import prepared


SCHEMA = {'type': 'object', 'properties': {'facts': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['facts'], 'additionalProperties': False}


def saved(service, story):
    return service._identity_snapshot(story['id'])[1].get('identity_closed_invalid_plan')


def retain(service, story, value, **kwargs):
    return retain_closed_invalid(service, story, value, SCHEMA,
        code='identity_search_plan_malformed', route='google', **kwargs)


@pytest.mark.parametrize('value,path,validator', [
    ({'facts': 3}, ['facts'], 'type'), ({}, [], 'required'),
    ({'facts': [777]}, ['facts', 0], 'type'),
    ({'facts': [], 'private_note': 'Synthetic private body'}, [], 'additionalProperties'),
])
def test_exact_bounded_rejected_json_and_value_free_logs(tmp_path, caplog, value, path, validator):
    service, _, story, _ = prepared(tmp_path)
    snapshot = service._identity_snapshot(story['id'])[0]
    raw = json.dumps(value, ensure_ascii=False, indent=2)
    caplog.set_level('INFO', logger='uvicorn.error')
    retain(service, snapshot, value, raw_json=raw, provider_response_id='original-response')
    diagnostic = saved(service, story)
    assert diagnostic['raw_json'] == raw
    assert diagnostic['raw_json_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic['raw_json_utf8_bytes'] == len(raw.encode()) and not diagnostic['raw_json_truncated']
    assert diagnostic['provider_response_id'] == 'original-response'
    assert diagnostic['validation_schema_sha256'] == hashlib.sha256(canonical(SCHEMA).encode()).hexdigest()
    detail = diagnostic['validation_errors'][0]
    assert detail['instance_path'] == path and detail['validator'] == validator
    if validator == 'required':
        assert detail['missing_properties'] == ['facts']
    assert 'message' not in detail and 'result' not in diagnostic
    assert validator in caplog.text
    assert 'Synthetic private body' not in caplog.text and 'private_note' not in caplog.text
    assert 'original-response' not in caplog.text


def test_large_multibyte_json_keeps_exact_hash_and_bounded_prefix(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    value = {'facts': 3, 'private_note': 'Ж' * 20000}
    raw = json.dumps(value, ensure_ascii=False)
    retain(service, service._identity_snapshot(story['id'])[0], value, raw_json=raw)
    diagnostic = saved(service, story)
    assert len(raw.encode()) > RAW_JSON_BYTES
    assert 'raw_json' not in diagnostic and diagnostic['raw_json_truncated'] is True
    assert diagnostic['raw_json_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    prefix = diagnostic['raw_json_prefix'].encode()
    assert len(prefix) <= RAW_PREFIX_BYTES and raw.encode().startswith(prefix)
    assert len(canonical(diagnostic).encode()) < RAW_JSON_BYTES


def test_many_errors_are_explicitly_bounded_without_validator_messages(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    retain(service, service._identity_snapshot(story['id'])[0], {'facts': [777] * 40})
    diagnostic = saved(service, story)
    assert diagnostic['validation_errors_truncated'] is True
    assert len(diagnostic['validation_errors']) == VALIDATION_ERRORS
    assert [e['instance_path'] for e in diagnostic['validation_errors']] == [['facts', i] for i in range(16)]


def test_first_invalid_is_immutable_after_restart_and_later_invalid(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    snapshot = service._identity_snapshot(story['id'])[0]
    first = retain(service, snapshot, {'facts': 3}, provider_response_id='first')
    assert retain(service, service._identity_snapshot(story['id'])[0], {}, provider_response_id='later') == first
    assert saved(service, story) == first


@pytest.mark.parametrize('change', ['photo', 'generation', 'revision', 'stop'])
def test_stale_photo_generation_revision_or_stop_cannot_persist(tmp_path, change):
    service, _, story, _ = prepared(tmp_path)
    snapshot = service._identity_snapshot(story['id'])[0]
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'] or '{}')
        if change == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b' * 64, story['id']))
        elif change == 'generation':
            research['identity_generation'] = int(research.get('identity_generation') or 0) + 1
        else:
            research['research_controls'] = {'identity': {'photo_sha256': story['photo_sha256'],
                'identity_generation': int(research.get('identity_generation') or 0),
                'revision': int(change == 'revision'), 'stopped': change == 'stop'}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    with pytest.raises(ConflictError) as error:
        retain(service, snapshot, {})
    assert error.value.code == 'visual_comparison_changed'
    assert saved(service, story) is None


def test_new_revision_retains_its_own_first_invalid(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    first = retain(service, service._identity_snapshot(story['id'])[0], {'facts': 3})
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['research_controls'] = {'identity': {'photo_sha256': story['photo_sha256'],
            'identity_generation': research.get('identity_generation') or 0, 'revision': 2}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    second = retain(service, service._identity_snapshot(story['id'])[0], {})
    assert first['scope']['control_revision'] == 0 and second['scope']['control_revision'] == 2
    assert saved(service, story) == second and second != first


class Executor:
    async def execute(self, operation, call):
        return await call('offline-fixture', 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['schema', 'json'])
async def test_first_invalid_persisted_before_same_joint2_repair(tmp_path, malformed):
    service, _, story, _ = prepared(tmp_path)
    calls = []
    valid = payload([choice('address', 'osm:node:1'), choice('address', 'osm:node:3')])
    bad = {k: v for k, v in valid.items() if k != 'first_wave_hypotheses'}
    raw = json.dumps(bad) if malformed == 'schema' else '{broken JSON'
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=raw, response_id='google-original')
        diagnostic = saved(service, story)
        assert diagnostic['raw_json'] == raw and diagnostic['provider_response_id'] == 'google-original'
        assert 'schema_validation' in contents[-1] if malformed == 'schema' else 'json_syntax' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        return SimpleNamespace(text=json.dumps(valid), response_id='google-repair')
    async def forbidden(*args):
        pytest.fail('A corrected joint2 cannot require an independent planner')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    snapshot = {**service._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    await identity_discovery.suggest(service, snapshot, '', [])
    assert len(calls) == 2 and saved(service, story)['provider_response_id'] == 'google-original'
    assert snapshot['_identity_search_plan_route'] == 'google'


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['schema', 'json'])
async def test_invalid_joint2_fail_closed_without_third_google_or_fresh_fallback(tmp_path, malformed):
    service, _, story, _ = prepared(tmp_path)
    calls = []
    raw = '{}' if malformed == 'schema' else '{broken'
    async def generate(*args, **kwargs):
        calls.append('google')
        assert len(calls) <= 2
        return SimpleNamespace(text=raw, response_id=f'google-{len(calls)}')
    async def forbidden(*args):
        pytest.fail('Closed-invalid joint2 cannot authorize a fresh third semantic operation')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['google', 'google'] and saved(service, story)['provider_response_id'] == 'google-1'
    followup = service._identity_snapshot(story['id'])[1]['identity_closed_invalid_followup_plan']
    assert followup['provider_response_id'] == 'google-2' and followup['raw_json'] == raw
    assert followup['joint_stage'] == 'followup'
    assert followup['operation_binding'] == joint_followup_marker(service,
        service._identity_snapshot(story['id'])[0])['binding']
    fresh_service = type(service)(service.settings, providers=service.providers)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(fresh_service, fresh_service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['google', 'google']


@pytest.mark.asyncio
async def test_distinct_closed_followup_json_survives_without_overwriting_original_or_resend(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    raw = ['{}', '{"entity_name":"Second rejected response"}']
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=raw[len(calls)-1], response_id=f'closed-{len(calls)}')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    research = service._identity_snapshot(story['id'])[1]
    first, second = research['identity_closed_invalid_plan'], research['identity_closed_invalid_followup_plan']
    assert first['raw_json'] == raw[0] and second['raw_json'] == raw[1]
    assert first['raw_json_sha256'] != second['raw_json_sha256']
    assert second['raw_json_sha256'] == hashlib.sha256(raw[1].encode()).hexdigest()
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['joint', 'joint']
    assert service._identity_snapshot(story['id'])[1]['identity_closed_invalid_plan'] == first
    assert service._identity_snapshot(story['id'])[1]['identity_closed_invalid_followup_plan'] == second


@pytest.mark.parametrize('change', ['binding', 'unknown'])
def test_followup_diagnostic_requires_original_closed_operation_binding(tmp_path, change):
    from street_story.identity_plan_diagnostics import joint_operation_marker
    service, _, story, _ = prepared(tmp_path)
    snapshot = service._identity_snapshot(story['id'])[0]
    binding = {'input_sha256':'a'*64, 'schema_sha256':hashlib.sha256(canonical(SCHEMA).encode()).hexdigest()}
    joint_operation_marker(service, snapshot, stage='followup', binding=binding,
        phase='unknown' if change == 'unknown' else 'response_closed')
    requested = {**binding, 'input_sha256':'b'*64} if change == 'binding' else binding
    with pytest.raises(ConflictError, match='Исходный закрытый запрос изменился'):
        retain(service, snapshot, {}, joint_stage='followup', operation_binding=requested)
    assert 'identity_closed_invalid_followup_plan' not in service._identity_snapshot(story['id'])[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['', None])
async def test_closed_empty_or_unavailable_text_never_invents_json_body(tmp_path, raw):
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=raw, response_id='closed-empty')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    diagnostic = saved(service, story)
    assert diagnostic['raw_json'] == '' and diagnostic['raw_json_utf8_bytes'] == 0
    assert diagnostic['raw_json_sha256'] == hashlib.sha256(b'').hexdigest()
    assert diagnostic['raw_json_available'] is (raw is not None)
    assert calls == ['joint', 'joint']


@pytest.mark.asyncio
async def test_joint2_retryable_outcome_keeps_original_error_without_key_or_fallback_resend(tmp_path):
    from pydantic import SecretStr
    from street_story.gemini import GeminiExecutor, GeminiKeyPool
    from street_story.providers import RetryableProviderError
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        if len(calls) == 1:
            return SimpleNamespace(text='{}', response_id='first')
        raise RetryableProviderError('original_followup_outcome_unknown', retry_at=service.store.now() + 30)
    async def forbidden(*args):
        pytest.fail('Unknown addressed joint2 cannot become another key/model/planner send')
    pool = GeminiKeyPool(service.store, (SecretStr('fixture-a'), SecretStr('fixture-b')), 'fixture-model')
    service.providers.gemini = SimpleNamespace(executor=GeminiExecutor(pool), _generate=generate, research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(RetryableProviderError, match='original_followup_outcome_unknown'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['google', 'google']
    fresh_service = type(service)(service.settings, providers=service.providers)
    with pytest.raises(RetryableProviderError, match='identity_joint_followup_outcome_unknown'):
        await identity_discovery.suggest(fresh_service, fresh_service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['google', 'google']
    research = fresh_service._identity_snapshot(story['id'])[1]
    marker = research['identity_joint_followup']
    assert marker['phase'] == 'unknown' and marker['scope']['photo_sha256'] == story['photo_sha256']
    assert len(marker['binding']['input_sha256']) == len(marker['binding']['schema_sha256']) == 64


@pytest.mark.asyncio
async def test_interrupted_send_intent_survives_restart_without_another_dispatch(tmp_path):
    import asyncio
    from street_story.providers import RetryableProviderError
    service, _, story, _ = prepared(tmp_path)
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        if len(calls) == 1:
            return SimpleNamespace(text='{}')
        assert joint_followup_marker(service, service._identity_snapshot(story['id'])[0])['phase'] == 'send_intent'
        raise asyncio.CancelledError
    async def forbidden(*args):
        pytest.fail('Interrupted send intent cannot authorize a new planner')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    with pytest.raises(asyncio.CancelledError):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    fresh_service = type(service)(service.settings, providers=service.providers)
    with pytest.raises(RetryableProviderError, match='identity_joint_followup_outcome_unknown'):
        await identity_discovery.suggest(fresh_service, fresh_service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['joint', 'joint']


@pytest.mark.asyncio
async def test_authoritative_unsent_followup_is_not_labelled_unknown(tmp_path):
    from street_story.providers import RetryableProviderError
    service, _, story, _ = prepared(tmp_path)
    calls = []
    error = RetryableProviderError('fixture_admission_denied')
    error.receipt = {'provider_send_state': 'not_sent'}
    async def generate(*args, **kwargs):
        calls.append('joint')
        if len(calls) == 1:
            return SimpleNamespace(text='{}')
        raise error
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    with pytest.raises(RetryableProviderError, match='fixture_admission_denied'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    marker = joint_followup_marker(service, service._identity_snapshot(story['id'])[0])
    assert marker['phase'] == 'not_sent' and marker['code'] == 'identity_joint_followup_not_sent'
    with pytest.raises(PermanentProviderError, match='identity_joint_followup_not_sent'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    assert calls == ['joint', 'joint']


@pytest.mark.asyncio
async def test_invalid_selected_regional_field_cannot_fetch_body_during_contract_repair(tmp_path, monkeypatch):
    from street_story import identity_architectural_context as context
    from test_geometry_identity_plan import geometry_setup, geometry_decision, payload as geometry_payload
    from test_regional_catalogue_selection import selection
    service, snapshot, active = geometry_setup(tmp_path)
    catalogue = {'results': [{'article_id': 'prussia39:sid:34'}]}
    async def prepare(*args, **kwargs):
        return catalogue
    async def forbidden_body(*args, **kwargs):
        pytest.fail('A schema-invalid selected field cannot acquire a body')
    monkeypatch.setattr(context, 'prepare_regional_catalogue', prepare)
    monkeypatch.setattr(context, 'acquire_selected_regional_text', forbidden_body)
    valid = geometry_payload(geometry_decision())
    invalid = {**valid, 'accepted_geometry': {**geometry_decision(), 'decision': 'uncertain'},
        'regional_article_selections': [{**selection(), 'scope': 777}]}
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=json.dumps(invalid if len(calls) == 1 else valid))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    await identity_discovery.suggest(service, snapshot, '', active)
    assert calls == ['joint', 'joint']
    assert snapshot['_identity_geometry_result']['proof_kind'] == 'geometry'


@pytest.mark.asyncio
async def test_invalid_original_readback_uses_its_own_frozen_schema_no_google(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    frozen = {'type': 'object', 'properties': {'legacy_count': {'type': 'integer'}},
        'required': ['legacy_count'], 'additionalProperties': False}
    async def forbidden(*args, **kwargs):
        pytest.fail('Original readback cannot dispatch another Google operation')
    async def readback(*args):
        return {'result': {'legacy_count': 'wrong type'}, 'original_schema': copy.deepcopy(frozen),
            'original_schema_readback': True, 'receipt': {'provider_response_id': 'native-original'}}
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=forbidden)
    service.providers.research = SimpleNamespace(has_identity_search_plan_readback=lambda _: True,
        plan_identity_search=readback)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.suggest(service, service._identity_snapshot(story['id'])[0], '', [])
    diagnostic = saved(service, story)
    assert diagnostic['validation_schema_origin'] == 'original_frozen'
    assert diagnostic['validation_schema_sha256'] == hashlib.sha256(canonical(frozen).encode()).hexdigest()
    assert diagnostic['validation_errors'] == validation_details(frozen, {'legacy_count': 'wrong type'})[0]
    assert diagnostic['provider_response_id'] == 'native-original'
    assert diagnostic['raw_json_origin'] == 'decoded_result'


@pytest.mark.asyncio
async def test_valid_original_readback_not_revalidated_under_new_joint_contract(tmp_path):
    service, _, story, _ = prepared(tmp_path)
    old = {key: value for key, value in payload([]).items() if key != 'first_wave_hypotheses'}
    old['article_queries'] = ['Frozen original query']
    frozen = {'type': 'object', 'properties': {key: {'type': 'array' if isinstance(value, list) else 'string'}
        for key, value in old.items()}, 'required': list(old), 'additionalProperties': False}
    async def readback(*args):
        return {'result': old, 'original_schema': frozen, 'original_schema_readback': True}
    service.providers.research = SimpleNamespace(has_identity_search_plan_readback=lambda _: True,
        plan_identity_search=readback)
    snapshot = service._identity_snapshot(story['id'])[0]
    joint_followup_marker(service, snapshot, binding={'input_sha256': 'a' * 64, 'schema_sha256': 'b' * 64},
        phase='send_intent')
    await identity_discovery.suggest(service, snapshot, '', [])
    assert snapshot['_identity_article_queries'] == ['Frozen original query']
    assert saved(service, story) is None
