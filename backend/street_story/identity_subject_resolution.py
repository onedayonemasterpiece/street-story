"""One final subject conclusion in the existing joint G/T operation.

Components retain their own evidence contracts. The model reconciles them;
the host only binds the final conclusion to the selected valid component.
"""
from copy import deepcopy

CONTRACT = 'joint-subject-resolution-v1'
COMPONENT_FIELDS = {'accepted_geometry', 'accepted_architectural_text', 'subject_resolution'}
INSTRUCTIONS = (
    'G and T describe their respective evidence, but there is ONE final subject decision in '
    'subject_resolution in this same response. After examining all supplied SOURCE, MAP, text '
    'and publisher photographs, reconcile relevant counterevidence before selecting a basis. '
    'A positive component alone does not settle the final subject. Explain whether the other '
    'component is irrelevant to that individual body, resolved by actual evidence, or still '
    'leaves a material alternative. Choose accepted with basis_kind G or T only when that '
    'component supports the same candidate and no material counterevidence remains unresolved. '
    'T uncertainty or unrelated text does not automatically veto sufficient G; G uncertainty '
    'does not veto sufficient T. Agreement, REF and a second reviewer are not required. '
    'If the subject is unresolved, preserve both components and give subject_resolution '
    'decision uncertain, basis_kind none, empty candidate_id and the unresolved counterevidence. '
    'The host does not choose meaning or override your final decision. '
)


def resolution_schema():
    return {'type': 'object', 'additionalProperties': False, 'properties': {
        'decision': {'type': 'string', 'enum': ['accepted', 'uncertain']},
        'basis_kind': {'type': 'string', 'enum': ['G', 'T', 'none']},
        'candidate_id': {'type': 'string', 'maxLength': 100},
        'unresolved_counterevidence': {'type': 'array', 'maxItems': 8,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 300}},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 600}},
        'required': ['decision', 'basis_kind', 'candidate_id', 'unresolved_counterevidence', 'reason'],
        'allOf': [
            {'if': {'properties': {'decision': {'const': 'accepted'}}}, 'then': {'properties': {
                'basis_kind': {'enum': ['G', 'T']}, 'candidate_id': {'minLength': 1},
                'unresolved_counterevidence': {'maxItems': 0}}}},
            {'if': {'properties': {'decision': {'const': 'uncertain'}}}, 'then': {'properties': {
                'basis_kind': {'const': 'none'}, 'candidate_id': {'const': ''}}}}]}


def with_subject_resolution(schema):
    schema = deepcopy(schema)
    schema['properties']['subject_resolution'] = resolution_schema()
    schema['required'].append('subject_resolution')
    return schema


def bind_subject_resolution(receipt, payload):
    """Copy the actual model conclusion only for a request carrying its marker."""
    if receipt.get('subject_resolution_contract') is None:
        return receipt
    return {**receipt, 'subject_resolution': deepcopy(payload.get('subject_resolution'))}


def resolution_supports(receipt, decision, basis_kind):
    """Frozen legacy requests stay legacy; fresh joint conclusions bind exactly."""
    if receipt.get('subject_resolution_contract') is None:
        return True
    if receipt.get('subject_resolution_contract') != CONTRACT:
        return False
    if not isinstance(decision, dict):
        return False
    from jsonschema import Draft202012Validator
    value = receipt.get('subject_resolution')
    return (Draft202012Validator(resolution_schema()).is_valid(value)
            and value['decision'] == 'accepted' and value['basis_kind'] == basis_kind
            and value['candidate_id'] == decision.get('candidate_id')
            and bool(value['reason'].strip()))
