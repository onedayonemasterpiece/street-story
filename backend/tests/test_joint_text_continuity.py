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
    assert 'accepted_geometry' not in prior
    assert prior['geometry_hypotheses']['status'] == 'unconfirmed'
    assert prior['geometry_hypotheses']['alternatives'][0]['candidate_id'] == 'osm:way:999'
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
@pytest.mark.parametrize('malformed_g', [False, True])
async def test_same_second_joint_carries_prior_and_rejects_empty_alternative_claim(tmp_path, monkeypatch, comparative, malformed_g):
    service, story, active = geometry_setup(tmp_path)
    initial = prior_for()
    initial['accepted_geometry']['decision'] = 'uncertain'
    if malformed_g:
        initial['accepted_geometry']['decisive_relations'][0]['map_features'][0]['candidate_id'] = 'unreceived-invalid-id'
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
        assert 'previous_model_hypotheses_not_evidence' in contents[-1]
        assert 'accepted_geometry' not in config.system_instruction
        assert 'unreceived-invalid-id' not in contents[-1]
        assert initial['accepted_geometry']['rejected_alternatives'][0]['reason'] in contents[-1]
        assert initial['source_scene_observations']['unknown'][0] in contents[-1]
        assert 'absence of a neighbor article' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        packet = json.loads(contents[-1].rsplit('\n', 1)[-1])
        hypotheses = packet['previous_model_hypotheses_not_evidence']
        assert 'decision' not in hypotheses['geometry_hypotheses_not_evidence']
        inventory = packet['publisher_and_OSM_literal_records_NOT_prejoined']
        decision['physical_link_evidence'] = [{
            'article_id': receipt['articles'][0]['article_id'], 'candidate_id': decision['candidate_id'],
            'publisher_ref': next(iter(inventory['publisher_refs'])),
            'osm_ref': next(ref for ref, row in inventory['osm_refs'].items()
                if row['candidate_id'] == decision['candidate_id']),
            'relationship': 'same_individual_physical_body', 'subject_scope': 'specific_photographed_OSM_body',
            'architectural_scope_explanation': 'The article describes this individual bay and return configuration.',
            'postal_interpretation': 'The literal records and article distinguish the individual body.'}]
        return SimpleNamespace(text=json.dumps(decision))

    async def forbidden(*args, **kwargs):
        pytest.fail('No third model, REF or replacement text search is authorized')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    if comparative:
        await identity_discovery.prepare_search_plan(service, story, '', active)
        proof = story['_identity_geometry_result']['architectural_text_proof']
        assert proof['source_text_receipt']['conditional_initial_decision']['candidate_ids'] == ['osm:way:2', 'osm:way:3']
    else:
        with pytest.raises(PermanentProviderError, match='identity_architectural_comparison_invalid'):
            await identity_discovery.prepare_search_plan(service, story, '', active)
        assert '_identity_geometry_result' not in story
        with pytest.raises(PermanentProviderError, match='identity_architectural_comparison_invalid'):
            await identity_discovery.suggest(service, story, '', active)
    assert len(calls) == 2 and reads == ['selected text']
