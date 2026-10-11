"""One-pass semantic declarations; host checks addressing and consistency only."""
from copy import deepcopy

LEGACY_CONTRACT = 'source-reading-assessment-v2'
VALUE_CONTRACT = 'source-reading-assessment-v3'
CONTRACT = 'source-reading-assessment-v4'
CONTRACTS = {LEGACY_CONTRACT, VALUE_CONTRACT, CONTRACT}
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
VALUE_CHECKS = (
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

RELATION_FIELDS = {
    'source_roles': {'type': 'array', 'maxItems': 8, 'description':
        'Read each named person in the own sentence AND its local context/references. '
        'Enumerate actual roles, including authors of accounts or bibliography. A bare '
        'according-to/by phrase does not explicitly identify an architect or other building participant.',
        'items': {'type': 'object', 'properties': {
            'entity': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'role': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'basis': {'enum': ['explicit_role', 'attributed_account', 'bibliographic_credit', 'unresolved']},
            'passage_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'integer'}},
            'quotes': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': {'type': 'string', 'minLength': 1, 'maxLength': 900}}},
            'required': ['entity', 'role', 'basis', 'passage_ids', 'quotes'], 'additionalProperties': False}},
    'propositions': {'type': 'array', 'minItems': 1, 'maxItems': 8,
        'description': 'Factor every assertion in text into subject/property/value propositions. '
            'One independently selectable property per proposition. Never bundle building count '
            'with storeys under structure, or a dated event with a later condition under history.',
        'items': {'type': 'object', 'properties': {
            'subject': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'property': {'type': 'string', 'minLength': 1, 'maxLength': 100},
            'value': {'type': 'string', 'minLength': 1, 'maxLength': 500},
            'text': {'type': 'string', 'minLength': 1, 'maxLength': 1200}},
            'required': ['subject', 'property', 'value', 'text'], 'additionalProperties': False}},
    'participant_role_indices': {'type': 'array', 'maxItems': 8, 'uniqueItems': True,
        'description': 'Indices into source_roles for participant roles ASSERTED by this text. '
            'A supported architect/designer/builder/etc assertion requires explicit_role, '
            'never attributed_account/bibliographic_credit/unresolved. Empty for statements '
            'that do not assign a building participant role.',
        'items': {'type': 'integer', 'minimum': 0}},
}
CHECKS = (
    'For source-reading-assessment-v4, resolve SOURCE relations before making a fact. '
    'FIRST read source_roles using the sentence, local antecedents and ALL frozen reference/bibliography '
    'passages. Distinguish the author of an account from a participant in the building history. '
    'A phrase such as "built in [year], according to/by [person]" can attribute an account; '
    'it does not by itself establish a designer. A matching name credited for a book is evidence '
    'of authorship, not architecture. Without an explicit participant role, omit that role claim '
    'or keep it nonpositive; do not disguise it by copying the ambiguous wording. '
    'NEXT factor text into propositions with one subject/property/value each. Counts of buildings, '
    'storeys and entrances are three properties. Eviction and a later ruined condition are separate. '
    'Emit separate fact rows for these propositions in this SAME operation, even when an old claim '
    'bundles them. supported requires one proposition whose text equals the fact text, and all '
    'own_evidence_values must address that same property. participant_role_indices may support '
    'only an explicitly established source_role. These declarations are your semantic judgments, '
    'not permission to label an unresolved relation explicit. Then examine competing own values '
    'and temporal qualifiers, then compare the normalized claim with prior inventory. '
    + VALUE_CHECKS.replace('For source-reading-assessment-v3, FIRST', 'For own values,')
)


def checks_for_contract(contract):
    return CHECKS if contract == CONTRACT else VALUE_CHECKS if contract == VALUE_CONTRACT else ''


def source_first_input(capsule):
    """Order the unchanged evidence before the old matching index on the wire."""
    if capsule.get('semantic_contract') not in {VALUE_CONTRACT, CONTRACT}:
        return capsule
    keys = ('sources', 'semantic_contract', 'context', 'known_fact_inventory')
    return {**{key: capsule[key] for key in keys if key in capsule},
            **{key: value for key, value in capsule.items() if key not in keys}}


def response_schema(legacy_schema, contract):
    if contract not in {VALUE_CONTRACT, CONTRACT}:
        return legacy_schema
    schema = deepcopy(legacy_schema)
    row = schema['properties']['facts']['items']
    # Present the evidence examination before the conclusion in the actual
    # response contract, matching the instruction to enumerate before verdict.
    order = ('own_evidence_values', 'own_value_conflicts', 'claims')
    fields = FIELDS
    if contract == CONTRACT:
        order = (*RELATION_FIELDS, *order)
        fields = {**RELATION_FIELDS, **FIELDS}
    row['properties'] = {**{key: deepcopy(fields[key]) for key in order}, **row['properties']}
    row['required'] = [*order, *row['required']]
    return schema


def row_errors(row, passages=None):
    """No host inference of atomicity, chronology, identity or semantic support."""
    if row['verdict'] == 'supported' and (
            row['claims'] != [row['text']] or row['own_value_conflicts']):
        return [{'path': ['claims'], 'validator': 'declared_assessment_inconsistent'}]
    own = set(row['passage_ids'])
    if 'propositions' in row:
        propositions = row['propositions']
        if row['verdict'] == 'supported' and (len(propositions) != 1
                or propositions[0]['text'] != row['text']
                or any(value['property'] != propositions[0]['property'] for value in row['own_evidence_values'])):
            return [{'path': ['propositions'], 'validator': 'declared_assessment_inconsistent'}]
        roles = row['source_roles']
        for index in row['participant_role_indices']:
            if index >= len(roles) or (row['verdict'] == 'supported' and roles[index]['basis'] != 'explicit_role'):
                return [{'path': ['participant_role_indices'], 'validator': 'declared_role_inconsistent'}]
    for value in [*row['own_evidence_values'], *row.get('source_roles', [])]:
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
    return {key: value for key, value in row.items() if key not in {*FIELDS, *RELATION_FIELDS}}
