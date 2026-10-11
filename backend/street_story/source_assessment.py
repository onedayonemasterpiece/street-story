"""One-pass semantic declarations; host checks addressing and consistency only."""
from copy import deepcopy

LEGACY_CONTRACT = 'source-reading-assessment-v2'
CONTRACT = 'source-reading-assessment-v3'
CONTRACTS = {LEGACY_CONTRACT, CONTRACT}
FIELDS = {
    'claims': {'type': 'array', 'minItems': 1, 'maxItems': 8,
        'description': 'All independently selectable propositions, not copies of a compound sentence. '
            'Separate dimensions, namesakes and construction events even for the same subject.',
        'items': {'type': 'string', 'minLength': 1, 'maxLength': 1200}},
    'own_evidence_values': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {
        'type': 'object', 'properties': {
            'property': {'type': 'string', 'minLength': 1, 'maxLength': 100,
                'description': 'One property, such as diameter or height; never a combined category such as sizes.'},
            'value': {'type': 'string', 'minLength': 1, 'maxLength': 500,
                'description': 'One own value of this property. Enumerate competing values separately, '
                    'including a differing infobox value, without inventing stages to reconcile them.'},
            'passage_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'integer'}},
            'quotes': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': {'type': 'string', 'minLength': 1, 'maxLength': 900}}},
        'required': ['property', 'value', 'passage_ids', 'quotes'], 'additionalProperties': False}},
    'own_value_conflicts': {'type': 'array', 'maxItems': 12,
        'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
}
CHECKS = (
    'For source-reading-assessment-v3, FIRST enumerate own_evidence_values for each asserted property '
    'from ALL own frozen passages, including every competing infobox/prose value. Copy exact own '
    'passage_ids and literal quotes for each value. Then declare own_value_conflicts; empty only '
    'when the source itself establishes consistent scope. Do not invent foundation/completion '
    'distinctions. Unresolved competing values require possible_conflict or insufficient. '
    'Then enumerate independently selectable propositions in claims BEFORE choosing verdict/atomic. '
    'Each dimension, person, role or distinct event is separate. Building count, levels and entrance '
    'count are independently selectable properties, not one structural fact. A historical event '
    'and a subsequent condition are separate claims. supported requires exactly one '
    'claim equal to text and no own_value_conflicts. Normalize separate new facts in this SAME pass. '
    'Known inventory is prior assertions for comparison, never substitute evidence or a semantic '
    'oracle: reassess each reused claim against all own values. A changed/narrowed claim must use '
    'existing_fact_id="". Do not copy a compound or disputed known claim as supported. '
    'Resolve sentence subjects and pronoun antecedents locally: after a source switches buildings, '
    'its following architects/management statements cannot silently return to the page-title building. '
    'If the antecedent remains ambiguous, withhold that claim. '
    'An account attributed to an author does not establish that author as the architect or designer. '
    'Preserve attribution as attribution, not a role inferred from a prior claim. An undated '
    'currently/now statement or an explicitly outdated source does not establish present-day use '
    'or condition. Retrieval time is not publication/event time; preserve the dated historical '
    'statement or withhold the unsupported current assertion. '
)


def source_first_input(capsule):
    """Order the unchanged evidence before the old matching index on the wire."""
    if capsule.get('semantic_contract') != CONTRACT:
        return capsule
    keys = ('sources', 'semantic_contract', 'context', 'known_fact_inventory')
    return {**{key: capsule[key] for key in keys if key in capsule},
            **{key: value for key, value in capsule.items() if key not in keys}}


def response_schema(legacy_schema, contract):
    if contract != CONTRACT:
        return legacy_schema
    schema = deepcopy(legacy_schema)
    row = schema['properties']['facts']['items']
    # Present the evidence examination before the conclusion in the actual
    # response contract, matching the instruction to enumerate before verdict.
    order = ('own_evidence_values', 'own_value_conflicts', 'claims')
    row['properties'] = {**{key: deepcopy(FIELDS[key]) for key in order}, **row['properties']}
    row['required'] = [*order, *row['required']]
    return schema


def row_errors(row, passages=None):
    """No host inference of atomicity, chronology, identity or semantic support."""
    if row['verdict'] == 'supported' and (
            row['claims'] != [row['text']] or row['own_value_conflicts']):
        return [{'path': ['claims'], 'validator': 'declared_assessment_inconsistent'}]
    own = set(row['passage_ids'])
    for value in row['own_evidence_values']:
        if any(pid not in own for pid in value['passage_ids']):
            return [{'path': ['own_evidence_values'], 'validator': 'own_passage_membership'}]
        if passages is not None:
            from .review_packets import literal_basis_quote
            texts = [passages[pid] for pid in value['passage_ids'] if pid in passages]
            if len(texts) != len(value['passage_ids']) or any(
                    not any(' '.join(literal_basis_quote(quote, texts).split()) in ' '.join(text.split())
                            for text in texts) for quote in value['quotes']):
                return [{'path': ['own_evidence_values'], 'validator': 'own_literal_quote'}]
    return []


def public_fact(row):
    # `claims` is a public evidence-group field with a different shape. Private
    # declarations remain in immutable raw receipts, not another acceptance writer.
    return {key: value for key, value in row.items() if key not in FIELDS}
