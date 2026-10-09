"""The same joint2 must revisit declared alternatives; priors are not evidence."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context, identity_discovery
from street_story.identity_proof import freeze_architectural_text_proof
from street_story.providers import PermanentProviderError
from test_architectural_text_identity import text_inputs
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload


def prior_for(primary='osm:way:2', alternative='osm:way:3'):
    initial = payload(geometry_decision())
    initial['accepted_geometry']['candidate_id'] = primary
    initial['accepted_geometry']['rejected_alternatives'][0]['candidate_id'] = alternative
    initial['source_scene_observations'] = {'observed': ['A bay and the street return are visible.'],
        'inferred': ['This could be the principal facade.'], 'unknown': ['The far return is cropped.']}
    return initial


def test_conditional_prior_keeps_source_and_negative_claims_but_requires_only_received_nominations():
    initial = prior_for(alternative='osm:way:999')
    initial['observed_candidate_ids'] = ['osm:way:2', 'osm:way:3']
    initial['spatial_hypotheses'] = [{'candidate_id': 'osm:way:3', 'support_status': 'plausible',
        'basis': ['Visible return may lie on this footprint.']}]
    original = copy.deepcopy(initial)
    prior = identity_discovery._conditional_text_prior(initial, ['osm:way:2', 'osm:way:3'])
    assert prior['input_kind'] == 'model_hypothesis_not_evidence'
    assert prior['candidate_ids'] == ['osm:way:2', 'osm:way:3']
    assert prior['accepted_geometry']['rejected_alternatives'][0]['candidate_id'] == 'osm:way:999'
    assert prior['source_scene_observations'] == initial['source_scene_observations']
    prior['source_scene_observations']['observed'].append('Changed local copy')
    assert initial == original


@pytest.mark.parametrize('malformed', [None, {}, {'accepted_geometry': None},
    {'accepted_geometry': {'rejected_alternatives': 3}}, {'observed_candidate_ids': 3}])
def test_malformed_prior_does_not_replace_existing_validation(malformed):
    prior = identity_discovery._conditional_text_prior(malformed, [])
    assert prior is None or prior['candidate_ids'] == []


@pytest.mark.parametrize('resolution', ['missing', 'comparative', 'uncertain', 'new_subject'])
def test_text_freeze_requires_reassessment_of_actual_prior_alternatives(resolution):
    story, candidates, decision, receipt = text_inputs(candidate_id='osm:way:2')
    neighbor = {**copy.deepcopy(candidates[0]), 'candidate_id': 'osm:way:3'}
    candidates.append(neighbor)
    story['_identity_observed_candidates'] = candidates
    receipt['conditional_initial_decision'] = identity_discovery._conditional_text_prior(
        prior_for(), ['osm:way:2', 'osm:way:3'])
    if resolution == 'comparative':
        decision['material_alternatives'] = [{'candidate_id': 'osm:way:3',
            'reason': 'The neighbor places its return beside the bay; SOURCE shows the return behind it.'}]
    elif resolution == 'uncertain':
        decision.update(decision='uncertain', material_alternatives_resolved=False)
    elif resolution == 'new_subject':
        decision['candidate_id'] = 'osm:way:3'
        decision['article_bindings'][0]['candidate_id'] = 'osm:way:3'
        decision['material_alternatives'] = [{'candidate_id': 'osm:way:2',
            'reason': 'On reconsideration the first subject has the reverse return ordering.'}]
    proof = freeze_architectural_text_proof(story, decision, receipt, candidates)
    assert (proof is not None) is (resolution in {'comparative', 'new_subject'})


@pytest.mark.asyncio
@pytest.mark.parametrize('comparative', [False, True])
async def test_same_second_joint_carries_prior_and_rejects_empty_alternative_claim(tmp_path, monkeypatch, comparative):
    service, story, active = geometry_setup(tmp_path)
    initial = prior_for()
    initial['accepted_geometry']['decision'] = 'uncertain'
    initial['regional_lookup'] = {'route': 'address', 'candidate_ids': ['osm:way:2'],
        'reason': 'Actual architectural text may distinguish the return and bay.'}
    _story, _catalog, decision, receipt = text_inputs(candidate_id='osm:way:2')
    if comparative:
        decision['material_alternatives'] = [{'candidate_id': 'osm:way:3',
            'reason': 'SOURCE shows the return behind the bay; this alternative puts the return beside it.'}]
    calls, reads = [], []

    async def acquire(*args):
        assert len(calls) == 1
        reads.append('selected text')
        return receipt['articles'], {'status': 'completed'}

    monkeypatch.setattr(identity_architectural_context, 'acquire_regional_text', acquire)

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert len(calls) == 2 and reads == ['selected text']
        assert 'Conditional initial model decision' in contents[-1]
        assert 'model_hypothesis_not_evidence' in contents[-1]
        assert initial['accepted_geometry']['rejected_alternatives'][0]['reason'] in contents[-1]
        assert initial['source_scene_observations']['unknown'][0] in contents[-1]
        assert 'Shared generic elements or the sole available article do not resolve alternatives.' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        return SimpleNamespace(text=json.dumps({**initial, 'accepted_architectural_text': decision}))

    async def forbidden(*args, **kwargs):
        pytest.fail('No third model, REF or replacement text search is authorized')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    if comparative:
        await identity_discovery.prepare_search_plan(service, story, '', active)
        proof = story['_identity_geometry_result']['architectural_text_proof']
        assert proof['source_text_receipt']['conditional_initial_decision']['candidate_ids'] == ['osm:way:2', 'osm:way:3']
    else:
        with pytest.raises(PermanentProviderError, match='identity_architectural_text_proof_invalid'):
            await identity_discovery.prepare_search_plan(service, story, '', active)
        assert '_identity_geometry_result' not in story
        with pytest.raises(PermanentProviderError, match='identity_architectural_text_proof_invalid'):
            await identity_discovery.suggest(service, story, '', active)
    assert len(calls) == 2 and reads == ['selected text']
