"""Missing address text must not discard received geometry for a nominated body."""
import copy
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context, identity_discovery
from test_geometry_identity_plan import geometry_decision, geometry_setup, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['accepted', 'uncertain', 'foreign_target', 'clarification'])
async def test_empty_address_text_uses_existing_joint2_without_assuming_identity(tmp_path, monkeypatch, outcome):
    svc, story, active = geometry_setup(tmp_path)
    first = geometry_decision()
    first.update(decision='uncertain', candidate_id='', next_action={
        'kind': 'address_text', 'target_candidate_ids': ['osm:way:999' if outcome == 'foreign_target' else 'osm:way:2'],
        'reason': 'Read the nominated individual body address; its overview geometry is insufficient.'})
    initial = payload(first)
    initial['observed_candidate_ids'] = ['osm:way:2']
    if outcome == 'clarification':
        initial['clarification_question'] = 'Which city was this photo taken in?'
    async def empty_lookup(*args, **kwargs):
        return [], {'status': 'completed_empty', 'identity_established': False}
    monkeypatch.setattr(identity_architectural_context, 'acquire_active_regional_text', empty_lookup)
    calls, saved = [], {}
    async def primary(s, prompt, schema, images, host_context):
        calls.append('G')
        saved.update(phase='completed', turn_id='original-G', result=copy.deepcopy(initial),
            frozen_source_map={'host_context': copy.deepcopy(host_context)})
        return {'result': initial, 'receipt': saved, 'host_context': host_context}
    async def followup(s, prompt, schema, images, host_context):
        calls.append('joint2')
        assert 'address_text_without_acquired_body' in prompt
        assert 'osm:way:2' in prompt and 'physical_bodies' in prompt
        decision = geometry_decision()
        if outcome == 'uncertain':
            decision.update(decision='uncertain', candidate_id='', spatial_correspondence=None,
                decisive_relations=[], next_action={'kind': 'none', 'target_candidate_ids': [], 'reason': ''})
            decision['bounded_coverage']['material_alternatives_resolved'] = False
        return {'result': payload(decision), 'receipt': {'phase': 'completed', 'turn_id': 'joint2'},
            'host_context': host_context}
    svc.providers.gemini = SimpleNamespace()
    svc.providers.research = SimpleNamespace(source_map_available=True,
        native_vision=SimpleNamespace(available=True), plan_source_map=primary,
        source_map_receipt=lambda s: saved or None, plan_source_map_followup=followup)
    await identity_discovery.prepare_search_plan(svc, story, '', active)
    assert calls == (['G', 'joint2'] if outcome in {'accepted', 'uncertain'} else ['G'])
    if outcome == 'accepted':
        assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    else:
        assert '_identity_geometry_result' not in story
