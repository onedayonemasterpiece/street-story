"""Isolate malformed closed rows without creating semantic verdicts."""
from copy import deepcopy

from jsonschema import Draft202012Validator


def fact_dependencies(row):
    if not isinstance(row, dict):
        return set()
    conflicts = row.get('conflicts_with_existing', [])
    if not isinstance(conflicts, list) or any(not isinstance(v, str) for v in conflicts):
        return None
    existing = row.get('existing_fact_id')
    if existing is not None and not isinstance(existing, str):
        return None
    links = {('existing', v) for v in [existing, *conflicts] if v}
    links.update((field, row[field]) for field in ('claim_key', 'text') if isinstance(row.get(field), str) and row[field])
    return links


def review_rows_valid(raw, schema):
    """A valid root plus at least one valid row permits local row validation.

    Return no replacement answer: the original decisions and conflict edges
    go to the own-evidence validator, which partitions before any commit.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get('decisions'), list):
        return False
    contract = (schema.get('properties') or {}).get('decisions') or {}
    if len(raw['decisions']) > contract.get('maxItems', len(raw['decisions'])):
        return False
    return (Draft202012Validator(schema).is_valid({**raw, 'decisions': []})
        and any(Draft202012Validator(contract['items']).is_valid(row) for row in raw['decisions']))


def partition_rows(rows, validate, dependencies):
    """Apply an existing row validator, retaining its exact scoped failures."""
    from .service import ConflictError
    links = [dependencies(row) for row in rows]
    if any(link is None for link in links):
        # An unaddressable relation cannot establish independent components.
        raise ConflictError('research_row_relations_invalid', 'Relation scope is malformed; no rows applied.')
    valid, failures = {}, {}
    for index, row in enumerate(rows):
        try:
            valid[index] = validate(deepcopy(row))
        except ConflictError as exc:
            failures[index] = exc
    blocked = set(failures)
    bad_links = set().union(*(links[i] for i in blocked)) if blocked else set()
    while True:
        dependent = {i for i in valid if i not in blocked and links[i] & bad_links}
        if not dependent:
            break
        blocked.update(dependent)
        bad_links.update(set().union(*(links[i] for i in dependent)))
    indices = [i for i in valid if i not in blocked]
    if blocked and not indices:
        raise next(iter(failures.values()))
    return [valid[i] for i in indices], {'accepted_indices': indices,
        'rejected_indices': sorted(failures), 'dependent_indices': sorted(blocked - set(failures)),
        'errors': {i: failure.code for i, failure in failures.items()}}


def isolate_rows(raw, schema, *, passage_ids=None):
    if not isinstance(raw, dict) or not isinstance(raw.get('facts'), list):
        return None
    rows = raw['facts']
    if any(isinstance(row, dict) and 'conflicts_with_existing' in row and
           (not isinstance(row['conflicts_with_existing'], list) or
            any(not isinstance(link, str) for link in row['conflicts_with_existing'])) for row in rows):
        return None  # The relation scope cannot be safely partitioned.
    contract = schema['properties']['facts']
    if len(rows) > contract.get('maxItems', len(rows)):
        return None
    root = {**raw, 'facts': []}
    if not Draft202012Validator(schema).is_valid(root):
        return None
    validator = Draft202012Validator(contract['items'])
    errors = {}
    for index, row in enumerate(rows):
        invalid = list(validator.iter_errors(row))
        if invalid:
            errors[index] = [{'path': list(error.path), 'validator': error.validator} for error in invalid]
        elif passage_ids is not None and any(pid not in passage_ids for pid in row.get('passage_ids', [])):
            errors[index] = [{'path': ['passage_ids'], 'validator': 'own_passage_membership'}]

    def dependencies(row):
        if not isinstance(row, dict):
            return set()
        links = {('existing', v) for v in [row.get('existing_fact_id'), *(row.get('conflicts_with_existing') or [])]
            if isinstance(v, str) and v}
        links.update((field, row[field]) for field in ('claim_key', 'text') if isinstance(row.get(field), str) and row[field])
        return links

    blocked = set(errors)
    bad_links = set().union(*(dependencies(rows[i]) for i in blocked)) if blocked else set()
    # Preserve declared relation dependencies transitively; independence is
    # never inferred from a malformed row's omitted or foreign fields.
    while True:
        dependent = {i for i, row in enumerate(rows) if i not in blocked and dependencies(row) & bad_links}
        if not dependent:
            break
        blocked.update(dependent)
        bad_links.update(set().union(*(dependencies(rows[i]) for i in dependent)))
    indices = [i for i in range(len(rows)) if i not in blocked]
    if blocked and not indices:
        return None
    result = deepcopy(raw)
    result['facts'] = [deepcopy(rows[i]) for i in indices]
    return {'result': result, 'raw_result': deepcopy(raw), 'row_isolation': {
        'accepted_indices': indices, 'rejected_indices': sorted(errors),
        'dependent_indices': sorted(blocked - set(errors)), 'errors': errors}}


def isolate_result(envelope, schema, *, passage_ids=None):
    """Compose validation boundaries using original answer row addresses."""
    isolated = isolate_rows(envelope.get('result'), schema, passage_ids=passage_ids)
    if isolated is None:
        return None
    receipt = envelope.get('receipt') or {}
    prior = envelope.get('row_isolation') or receipt.get('row_isolation')
    current = isolated['row_isolation']
    if not current['rejected_indices']:
        return {**envelope, **({'row_isolation': deepcopy(prior),
            'raw_result': deepcopy(envelope.get('raw_result', receipt.get('raw_result')))} if prior else {})}
    if prior:
        indices = prior['accepted_indices']
        current = {'accepted_indices': [indices[i] for i in current['accepted_indices']],
            'rejected_indices': sorted([*prior['rejected_indices'], *[indices[i] for i in current['rejected_indices']]]),
            'dependent_indices': sorted([*prior['dependent_indices'], *[indices[i] for i in current['dependent_indices']]]),
            'errors': {**prior['errors'], **{indices[int(i)]: errors for i, errors in current['errors'].items()}}}
    return {**envelope, 'result': isolated['result'], 'row_isolation': current,
        'raw_result': deepcopy(envelope.get('raw_result', receipt.get('raw_result', isolated['raw_result'])))}
