"""Private literal addressing for frozen headless reviews; no semantic verdicts."""
from copy import deepcopy
import hashlib

from .service import ConflictError

PREFIX = '@own_quote:'


def _entry(packet, item):
    passage = item['passage']
    digest = hashlib.sha256(passage.encode('utf-8')).hexdigest()
    label = f"{PREFIX}{packet['packet_ref']}:{item['fact']}:{item['evidence']}:{item['offset']}:{digest[:16]}"
    return label, {'fact': item['fact'], 'evidence': item['evidence'],
                   'offset': item['offset'], 'passage_sha256': digest}


def with_quote_catalog(packet):
    """Label every already bounded literal slice without duplicating source text."""
    packet = deepcopy(packet)
    catalog = {}
    for item in packet['items']:
        if not isinstance(item['passage'], str) or not 1 <= len(item['passage']) <= 900:
            raise ConflictError('live_fact_review_evidence_invalid', 'Invalid frozen literal slice.')
        label, entry = _entry(packet, item)
        item['quote_ref'] = label
        catalog[label] = entry
    packet['quote_catalog'] = catalog
    return packet


def response_schema(packet, public_schema):
    """Constrain fresh private answers without changing the interactive tool."""
    schema = deepcopy(public_schema)
    decisions = schema['properties']['decisions']['items']['properties']
    if packet.get('items'):
        numbers = sorted({item['fact'] for item in packet['items']})
        decisions['fact'] = {'type': 'integer', 'enum': numbers}
    if 'equivalent_to' in decisions and 'items' in packet:
        decisions['equivalent_to'] = {
            'type': ['integer', 'null'],
            'enum': [*sorted({item['fact'] for item in packet['items']}), None],
            'description': 'Optional canonical fact number from THIS packet. Omit or use null for no equivalence; never -1. A canonical may reference itself.'}
    if 'quote_catalog' in packet:
        quotes = decisions['basis_quotes']
        quotes['items'] = {'type': 'string', 'enum': list(packet['quote_catalog'])}
        quotes['description'] = ('Exact frozen quote_ref labels only. Choose this fact\'s selected '
                                 'evidence; empty only for unsupported decisions. Labels establish '
                                 'literal addressing, not semantic support.')
    if packet.get('verifier_presentation') == 'one_assertion_all_own_slices_v1':
        decisions['own_evidence_values'] = {'type': 'array', 'maxItems': 32, 'items': {
            'type': 'object', 'properties': {'property': {'type': 'string'}, 'value': {'type': 'string'},
                'quote_refs': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'enum': list(packet['quote_catalog'])}}},
            'required': ['property', 'value', 'quote_refs'], 'additionalProperties': False}}
        decisions['own_value_conflicts'] = {'type': 'array', 'maxItems': 12, 'items': {'type': 'string', 'maxLength': 500}}
        schema['properties']['decisions']['items'].setdefault('required', []).extend(
            ['own_evidence_values', 'own_value_conflicts'])
        # Advisory early stopping cannot discard independent own-source
        # verdicts. sufficient_basis validates addressing before any stop.
        schema['properties']['research_sufficient'] = {'description': 'Optional boolean early-stop advice.'}
        schema['properties']['research_sufficient_basis'] = {'description':
            'Optional {candidate_indices:[integer,...], known_fact_ids:[string,...], reason:string}. '
            'Use exact packet candidates and eligible known facts supporting the coverage goal.'}
    return schema


def model_packet(packet):
    """Present each assertion once, retaining every own immutable literal slice.

    Original public packets/quote journals stay unchanged. Grouping only copies
    already addressed numbers; it never discovers equivalence or splits claims.
    """
    groups = {}
    for item in packet['items']:
        group = groups.setdefault(item['fact'], {'fact': item['fact'], 'text': item.get('text', ''), 'evidence': {}})
        evidence = group['evidence'].setdefault(item['evidence'], {
            'evidence': item['evidence'], 'source_url': item.get('source_url'), 'slices': []})
        evidence['slices'].append({key: item[key] for key in
            ('offset', 'passage', 'passage_complete', 'quote_ref') if key in item})
    facts = [{**group, 'evidence': list(group['evidence'].values())} for group in groups.values()]
    return {**{key: value for key, value in packet.items() if key not in {'items', 'quote_catalog'}},
            'facts': facts, 'presentation': 'one_assertion_all_own_slices_v1'}


def public_result(packet, args):
    """Keep private model reasoning durable; public commit uses its original schema."""
    if packet.get('verifier_presentation') != 'one_assertion_all_own_slices_v1':
        return args
    value = deepcopy(args)
    value.pop('research_sufficient', None)
    value.pop('research_sufficient_basis', None)
    for decision in value.get('decisions', []):
        if decision.get('own_value_conflicts') and decision.get('verdict') == 'supported':
            raise ConflictError('live_fact_review_evidence_invalid',
                                'The model declared unresolved own-value conflicts; supported is inconsistent.')
        decision.pop('own_evidence_values', None)
        decision.pop('own_value_conflicts', None)
    return value


def sufficient_basis(packet, args, original_items):
    """Resolve the same review operation's goal decision, without deciding meaning."""
    basis = args.get('research_sufficient_basis')
    if args.get('research_sufficient') is not True or not isinstance(basis, dict):
        return []
    indices, known_ids = basis.get('candidate_indices'), basis.get('known_fact_ids')
    known = {row['fact_id']: row['text'] for row in packet.get('eligible_facts', [])}
    if (not isinstance(indices, list) or not isinstance(known_ids, list)
            or any(type(i) is not int or not 0 <= i < len(original_items) for i in indices)
            or any(not isinstance(fid, str) or fid not in known for fid in known_ids)):
        return []
    return ([(original_items[i]['id'], original_items[i]['text']) for i in indices]
            + [(fid, known[fid]) for fid in known_ids])


def resolve_quotes(packet, args):
    """Resolve only explicit labels for this fact's selected immutable evidence."""
    if 'quote_catalog' not in packet:
        return args  # Addressed legacy contracts keep their original literal semantics.
    catalog = packet['quote_catalog']
    if not isinstance(catalog, dict) or args.get('packet_ref') != packet['packet_ref']:
        raise ConflictError('live_fact_review_evidence_invalid', 'Quote catalog belongs to one frozen packet.')
    literals = {}
    for item in packet['items']:
        label, entry = _entry(packet, item)
        if catalog.get(label) != entry or item.get('quote_ref') != label:
            raise ConflictError('live_fact_review_evidence_invalid', 'Frozen quote catalog changed.')
        literals[label] = item['passage']
    resolved = deepcopy(args)
    for decision in resolved.get('decisions', []):
        quotes = decision.get('basis_quotes')
        if not isinstance(quotes, list):
            continue  # The ordinary public validator rejects malformed fields.
        for index, quote in enumerate(quotes):
            if not isinstance(quote, str) or not quote.startswith(PREFIX):
                continue
            entry = catalog.get(quote)
            if (not isinstance(entry, dict) or quote not in literals
                    or entry['fact'] != decision.get('fact')
                    or not isinstance(decision.get('evidence'), list)
                    or entry['evidence'] not in decision['evidence']):
                raise ConflictError('live_fact_review_evidence_invalid',
                                    'Quote reference must select this fact\'s own chosen evidence.')
            decision['basis_quotes'][index] = literals[quote]
    return resolved
