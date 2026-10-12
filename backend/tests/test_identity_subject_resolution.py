"""The original joint operation owns its final subject; the host binds it only."""
import copy

import pytest
from jsonschema import Draft202012Validator

from street_story.identity_proof import freeze_architectural_text_proof, freeze_geometry_proof
from street_story.identity_subject_resolution import (
    CONTRACT, bind_subject_resolution, resolution_schema, resolution_supports, with_subject_resolution)
from test_architectural_text_identity import text_inputs
from test_geometry_subject_articles import geometry_identity


def conclusion(kind='G', candidate_id='osm:way:7'):
    return {'decision': 'accepted', 'basis_kind': kind, 'candidate_id': candidate_id,
        'unresolved_counterevidence': [],
        'reason': 'The other component concerns an unrelated article; the selected independent basis resolves SOURCE.'}


@pytest.mark.parametrize('change', [
    {'unresolved_counterevidence': ['The selected SOURCE body still has an unresolved physical alternative.']},
    {'basis_kind': 'none'}, {'candidate_id': ''}, {'reason': ''},
])
def test_positive_final_subject_cannot_declare_unresolved_counterevidence(change):
    assert Draft202012Validator(resolution_schema()).is_valid(conclusion())
    assert not Draft202012Validator(resolution_schema()).is_valid({**conclusion(), **change})


def test_final_uncertainty_preserves_model_counterevidence_without_selecting_a_body():
    value = {'decision': 'uncertain', 'basis_kind': 'none', 'candidate_id': '',
        'unresolved_counterevidence': ['SOURCE bay shape differs from the proposed individual body.'],
        'reason': 'This is a scoped contradiction, rather than absent support for an unrelated article.'}
    assert Draft202012Validator(resolution_schema()).is_valid(value)
    assert not Draft202012Validator(resolution_schema()).is_valid({**value, 'candidate_id': 'osm:way:7'})
    receipt = {'subject_resolution_contract': CONTRACT}
    bound = bind_subject_resolution(receipt, {'subject_resolution': value})
    assert bound['subject_resolution'] == value
    assert receipt == {'subject_resolution_contract': CONTRACT}
    assert not resolution_supports(bound, {'candidate_id': 'osm:way:7'}, 'G')


@pytest.mark.parametrize('proof_kind', ['G', 'T'])
@pytest.mark.parametrize('final', ['own_basis', 'other_basis', 'wrong_body', 'uncertain', 'missing', 'blank_reason', 'unknown_contract'])
def test_only_matching_final_basis_can_freeze_existing_component(proof_kind, final):
    story, candidates, text, text_receipt = text_inputs()
    original = geometry_identity()
    decision, receipt = ((original['geometry_proof']['decision'], original['geometry_proof']['source_map_receipt'])
        if proof_kind == 'G' else (text, text_receipt))
    freeze = freeze_geometry_proof if proof_kind == 'G' else freeze_architectural_text_proof
    baseline = freeze(story, decision, receipt, candidates)
    assert baseline is not None
    before = copy.deepcopy((story, decision, receipt))
    result = conclusion(proof_kind)
    if final == 'other_basis':
        result['basis_kind'] = 'T' if proof_kind == 'G' else 'G'
    elif final == 'wrong_body':
        result['candidate_id'] = 'osm:way:8'
    elif final == 'uncertain':
        result.update(decision='uncertain', basis_kind='none', candidate_id='',
            unresolved_counterevidence=['Relevant SOURCE counterevidence remains unresolved.'])
    elif final == 'blank_reason':
        result['reason'] = '   '
    marked = {**receipt, 'subject_resolution_contract': CONTRACT if final != 'unknown_contract' else 'unknown-contract'}
    payload = {} if final == 'missing' else {'subject_resolution': result}
    proof = freeze(story, decision, bind_subject_resolution(marked, payload), candidates)
    assert (proof is not None) is (final == 'own_basis')
    if proof is not None:
        key = 'source_map_receipt' if proof_kind == 'G' else 'source_text_receipt'
        assert proof[key]['subject_resolution'] == result
        assert proof['decision'] == decision
    # Frozen pre-contract turns keep their exact receipt, hash and original admission.
    assert freeze(story, decision, bind_subject_resolution(receipt, payload), candidates) == baseline
    assert (story, decision, receipt) == before


def test_final_contract_is_required_only_in_its_new_frozen_request():
    original = {'type': 'object', 'properties': {'accepted_geometry': {'type': 'object'}},
        'required': ['accepted_geometry']}
    new = with_subject_resolution(original)
    assert new['required'] == ['accepted_geometry', 'subject_resolution']
    assert original['required'] == ['accepted_geometry']
    assert not Draft202012Validator(new).is_valid({'accepted_geometry': {}})
    assert bind_subject_resolution({'turn_id': 'frozen-unknown'}, {}) == {'turn_id': 'frozen-unknown'}
