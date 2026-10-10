"""A closed unusable T response fences inference, not acquired REF work."""
import json

import pytest

from street_story import identity_discovery
from street_story.identity_plan_diagnostics import joint_followup_marker, retain_closed_invalid
from street_story.providers import PermanentProviderError
from test_visual_search_continuation import prepared


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['identity_architectural_comparison_invalid', 'identity_architectural_text_uncertain'])
async def test_closed_text_keeps_sources_and_defers_reference_without_third_inference(tmp_path, monkeypatch, code):
    service, _, story, _ = prepared(tmp_path)
    source = {'url': 'https://example.com/own-article', 'title': 'Acquired article'}
    identity_discovery._retain_article_discovery(service, story, [source])
    binding = {'input_sha256': 'a' * 64, 'schema_sha256': 'b' * 64}
    joint_followup_marker(service, story, binding=binding, phase='send_intent')
    joint_followup_marker(service, story, binding=binding, phase='response_closed')
    diagnostic = retain_closed_invalid(service, story, None, {'type': 'object'},
        code=code, route='fixture', raw_json='{"limitations": [unquoted invalid text]}',
        joint_stage='followup', operation_binding=binding)
    joint_followup_marker(service, story, binding=binding, phase='response_closed', code=code)
    calls = []
    async def unusable(*args):
        calls.append('closed-readback')
        raise PermanentProviderError(code)
    monkeypatch.setattr(identity_discovery, 'suggest', unusable)
    candidates = [{'candidate_id': 'osm:way:1', 'name': 'Observed body'}]
    current = service._identity_snapshot(story['id'])[0]
    history, _ = await identity_discovery.prepare_search_plan(service, current, '', candidates)
    assert history['sources'] == [source]
    assert history['search_plan']['payload']['diagnostic_sha256'] == diagnostic['raw_json_sha256']
    assert current['_identity_accepted_result']['status'] == 'uncertain'
    assert current['_identity_accepted_result']['_comparison_deferred']
    assert '_identity_geometry_result' not in current
    assert history.get('queries') == {}
    fresh = service._identity_snapshot(story['id'])[0]
    await identity_discovery.prepare_search_plan(service, fresh, '', candidates)
    assert calls == ['closed-readback']
    assert fresh['_identity_accepted_result']['status'] == 'uncertain'
    result, retained_candidates = await identity_discovery.recover(service, fresh, '', candidates, set())
    assert result['status'] == 'uncertain' and result['_comparison_deferred']
    assert retained_candidates == candidates and calls == ['closed-readback']
    retained = json.loads(service._identity_snapshot(story['id'])[0]['research_json'])
    assert retained['identity_closed_invalid_followup_plan'] == diagnostic
    assert retained['identity_joint_followup']['phase'] == 'response_closed'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['unknown', 'send_intent', 'closed_failure'])
async def test_unproven_closed_text_failure_does_not_bypass_original_outcome(tmp_path, monkeypatch, phase):
    service, _, story, _ = prepared(tmp_path)
    binding = {'input_sha256': 'a' * 64}
    joint_followup_marker(service, story, binding=binding, phase='send_intent')
    if phase != 'send_intent':
        joint_followup_marker(service, story, binding=binding, phase=phase)
    async def failed(*args):
        raise PermanentProviderError('identity_architectural_comparison_invalid')
    monkeypatch.setattr(identity_discovery, 'suggest', failed)
    with pytest.raises(PermanentProviderError, match='identity_architectural_comparison_invalid'):
        await identity_discovery.prepare_search_plan(service, story, '', [])
    current = json.loads(service._identity_snapshot(story['id'])[0]['research_json'])
    assert not (current.get('identity_article_discovery') or {}).get('search_plan')
