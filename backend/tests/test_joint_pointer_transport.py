"""Explicit frozen label pointers cross the same joint boundary in every role."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context, identity_discovery
from street_story.identity_source_selection import resolve_identity_response_ids
from test_architectural_text_identity import text_inputs
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload


def test_reported_label_mismatch_feedback_preserves_invalid_id_without_assigning_label_subject():
    decision = geometry_decision()
    decision['candidate_id'] = 'osm:way:432'
    decision['candidate_label'] = 432
    decision['decisive_relations'][0]['map_features'][0]['candidate_id'] = 'osm:way:432'
    original = copy.deepcopy(decision)
    manifest = {'objects': {'columns': ['label', 'candidate_id'],
        'rows': [[432, 'osm:way:987654'], [381, 'osm:way:123456'], [3, 'osm:way:3'], [9, 'osm:way:9']]}}
    issues = identity_discovery._geometry_binding_issues(decision, manifest)
    assert issues['candidate_label_binding']['received_label_candidate_id'] == 'osm:way:987654'
    assert issues['candidate_label_binding']['reported_candidate_id'] == 'osm:way:432'
    assert issues['unreceived_primary_id'] == 'osm:way:432'
    assert decision == original
    assert resolve_identity_response_ids({'accepted_geometry': decision}, {'map_scene': manifest}) == (
        {'accepted_geometry': original}, None)
    explicit = {**decision, 'candidate_id': '@432'}
    resolved, _ = resolve_identity_response_ids({'accepted_geometry': explicit}, {'map_scene': manifest})
    assert resolved['accepted_geometry']['candidate_id'] == 'osm:way:987654'
    # Explicit label resolution does not change another invalid pointer or
    # infer that this object's geometry/article/physical scope matches SOURCE.
    assert resolved['accepted_geometry']['decisive_relations'][0]['map_features'][0]['candidate_id'] == 'osm:way:432'
    assert identity_discovery._geometry_binding_issues(resolved['accepted_geometry'], manifest)['unreceived_feature_ids'] == ['osm:way:432']


@pytest.mark.asyncio
async def test_plain_map_aliases_nominate_selected_text_and_freeze_after_one_same_source_followup(tmp_path, monkeypatch):
    service, story, active = geometry_setup(tmp_path)
    uncertain = geometry_decision()
    uncertain.update(decision='uncertain', candidate_id='@1')
    uncertain['rejected_alternatives'][0]['candidate_id'] = '@2'
    uncertain['decisive_relations'][0]['map_features'][0]['candidate_id'] = '@1'
    uncertain['decisive_relations'][0]['map_features'][1]['candidate_id'] = '@3'
    initial = payload(uncertain)
    initial.update(observed_candidate_ids=['@1'], regional_lookup={'route': 'address',
        'candidate_ids': ['@1'], 'reason': 'The received body text may distinguish the bay and return.'},
        spatial_hypotheses=[{'candidate_id': '@1', 'support_status': 'plausible',
            'basis': ['Visible approach is compatible.'], 'counterevidence': [],
            'assumptions': ['The shown side is the principal facade.'], 'next_action': 'Read selected text.'}])
    _fixture, _catalog, text_decision, receipt = text_inputs(candidate_id='osm:way:2')
    text_decision.update(candidate_id='@1', material_alternatives=[{'candidate_id': '@2',
        'reason': 'The SOURCE/text bay is ahead of the return; this neighboring footprint reverses that order.'}])
    text_decision['article_bindings'][0]['candidate_id'] = '@1'
    calls, acquired = [], []

    async def acquire(svc, snapshot, candidates, request):
        assert len(calls) == 1
        assert request['candidate_ids'] == ['osm:way:2']
        acquired.append(request)
        return receipt['articles'], {'status': 'completed'}

    monkeypatch.setattr(identity_architectural_context, 'acquire_regional_text', acquire)

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert 'Every physical pointer field uses the same namespace' in config.system_instruction
        assert 'never write osm:way:N' in config.system_instruction
        assert 'Exact received ID or @N' in config.system_instruction
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert len(calls) == 2 and len(acquired) == 1
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        assert receipt['articles'][0]['text'] in contents[-1]
        return SimpleNamespace(text=json.dumps({**initial, 'accepted_architectural_text': text_decision}))

    async def forbidden(*args, **kwargs):
        pytest.fail('Explicit aliases need no third model, new selector or reference image')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(calls) == 2 and len(acquired) == 1
    proof = story['_identity_geometry_result']['architectural_text_proof']
    assert proof['candidate_id'] == 'osm:way:2'
    assert proof['decision']['material_alternatives'][0]['candidate_id'] == 'osm:way:3'
    assert history['search_plan']['payload']['spatial_hypotheses'][0]['candidate_id'] == 'osm:way:2'
    assert history['search_plan']['payload']['observed_candidate_ids'] == ['osm:way:2']
