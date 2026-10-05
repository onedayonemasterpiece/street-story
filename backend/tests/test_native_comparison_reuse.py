"""Exact prior evidence is reusable; shared place names and unknown turns are not."""
import hashlib
import json

import pytest

from street_story.headless_identity import VERDICT_SCHEMA
from street_story.native_vision import MODEL, TRANSPORT, visual_request
from street_story.research_adapter import ProductResearchAdapter, semantic_visual_context
from street_story.service import canonical, ConflictError
from street_story.research_control import stop_research
from test_mvp_research import PHOTO, PHOTO_SHA, service


def setup(tmp_path):
    svc, _, first = service(tmp_path)
    second = svc.create_story(key='second', client_story_id='second', photo_sha256=PHOTO_SHA,
        photo_mime_type='image/jpeg', photo_bytes=PHOTO, voice_protocol='voice-chunks-v2', lat=54.7104, lon=20.4522)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = svc
    context = semantic_visual_context(canonical({'comparison_id': 'lease-1',
        'references': [{'candidate_id': 'web:ref'}], 'physical_candidates': [{'candidate_id': 'osm:way:1'}]}))
    snapshot = b'exact-source-and-reference-pixels'
    unit = hashlib.sha256(snapshot).hexdigest() + hashlib.sha256((context + canonical(VERDICT_SCHEMA)).encode()).hexdigest()
    binding, _ = adapter.attempt(first, 'vision_native', unit)
    _, prompt = visual_request(VERDICT_SCHEMA, json.loads(context))
    result = {'status': 'match', 'candidate_id': 'web:ref', 'confidence': .98,
        'observations': ['Distinctive towers and arch correspond.'], 'alternative_candidate_ids': [],
        'reference_subject_candidate_id': 'osm:way:1', 'reference_subject_observations': ['Visible matching brickwork.']}
    receipt = {'binding': binding, 'phase': 'completed', 'model': MODEL, 'transport': TRANSPORT,
        'provider': 'codex_native', 'profile_verified': True, 'thread_id': 'actual-thread', 'turn_id': 'actual-turn',
        'photo_sha256': PHOTO_SHA, 'model_image_sha256': hashlib.sha256(snapshot).hexdigest(),
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'result': result, 'usage': {'totalTokens': 4321}}
    with svc.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
            (canonical(receipt), binding['attempt_id']))
    current, _ = adapter.attempt(second, 'vision_native', unit)
    return adapter, binding, current, unit, snapshot, context, receipt


def test_exact_comparison_reused_for_another_story_with_zero_new_inference(tmp_path):
    adapter, origin, current, unit, snapshot, context, receipt = setup(tmp_path)
    reused = adapter.reuse_native_verdict(current, unit, snapshot, VERDICT_SCHEMA, context)
    assert reused['result'] == receipt['result']
    assert reused['binding']['story_id'] == current['story_id']
    assert reused['reused_from']['attempt_id'] == origin['attempt_id']
    assert reused['reused_from']['usage'] == {'totalTokens': 4321}
    assert reused['usage'] == {'totalTokens': 0, 'cost': 0}
    assert reused['inference_performed'] is False
    assert 'turn_id' not in reused and 'quota_permission' not in reused
    # The persisted result is addressed by this story's normal attempt key.
    binding, saved = adapter.attempt({'id': current['story_id'], 'photo_sha256': PHOTO_SHA}, 'vision_native', unit)
    assert binding is None and saved == reused


@pytest.mark.parametrize('change', ['snapshot', 'catalog', 'schema', 'prompt', 'model', 'profile',
    'unknown_origin', 'failed_origin', 'missing_turn', 'invalid_result', 'unknown_current'])
def test_changed_inputs_or_unverified_or_unknown_outcomes_cannot_be_reused(tmp_path, change):
    adapter, origin, current, unit, snapshot, context, receipt = setup(tmp_path)
    schema = json.loads(canonical(VERDICT_SCHEMA))
    if change == 'snapshot':
        snapshot += b'changed'
    elif change == 'catalog':
        context = canonical({**json.loads(context), 'new_hypothesis': 'another building'})
    elif change == 'schema':
        schema['properties']['confidence']['minimum'] = .99
    elif change == 'prompt':
        receipt['prompt_sha256'] = 'prior-instructions'
    elif change == 'model':
        receipt['model'] = 'unqualified-model'
    elif change == 'profile':
        receipt['profile_verified'] = False
    elif change in {'unknown_origin', 'failed_origin'}:
        receipt['phase'] = change.split('_')[0]
    elif change == 'missing_turn':
        receipt.pop('turn_id')
    elif change == 'invalid_result':
        receipt['result']['candidate_id'] = 'foreign-reference'
    with adapter.service.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
            (canonical(receipt), origin['attempt_id']))
        if change == 'unknown_current':
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                (canonical({'binding': current, 'phase': 'unknown', 'thread_id': 'pending-thread', 'turn_id': 'pending-turn'}), current['attempt_id']))
    assert adapter.reuse_native_verdict(current, unit, snapshot, schema, context) is None


def test_reuse_obeys_stop_and_does_not_apply_a_late_result(tmp_path):
    adapter, _, current, unit, snapshot, context, _ = setup(tmp_path)
    stop_research(adapter.service, current['story_id'], purpose='identity')
    with pytest.raises(ConflictError, match='остановлено'):
        adapter.reuse_native_verdict(current, unit, snapshot, VERDICT_SCHEMA, context)
