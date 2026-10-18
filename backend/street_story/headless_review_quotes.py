"""Literal addressing for frozen source-backed reviews; no semantic verdicts."""
from copy import deepcopy
import hashlib

from .service import ConflictError

PREFIX = '@own_quote:'
OWN_REFS_CONTRACT = 'own_quote_refs_v2'


def _entry(packet, item):
    passage = item['passage']
    digest = hashlib.sha256(passage.encode('utf-8')).hexdigest()
    label = f"{PREFIX}{packet['packet_ref']}:{item['fact']}:{item['evidence']}:{item['offset']}:{digest[:16]}"
    return label, {'fact': item['fact'], 'evidence': item['evidence'],
                   'offset': item['offset'], 'passage_sha256': digest}


def with_quote_ref(packet, item):
    """Address an unchanged slice before the existing response-size check."""
    label, _ = _entry(packet, item)
    return {**item, 'quote_ref': label}


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


def remember_live_page(session, packet):
    """Reuse literal addressing for slices issued in this interactive session."""
    addressed = with_quote_catalog(packet)
    received = session.state.setdefault('review_quote_packets', {}).setdefault(packet['packet_ref'],
        {'packet_ref': packet['packet_ref'], 'items': [], 'quote_catalog': {}})
    for item in addressed['items']:
        label = item['quote_ref']
        if label not in received['quote_catalog']:
            received['items'].append(item)
            received['quote_catalog'][label] = addressed['quote_catalog'][label]
    # The model needs the inline address, not a duplicate address catalog.
    return {key: value for key, value in addressed.items() if key != 'quote_catalog'}


def resolve_live_rows(session, args):
    """Resolve only the used own labels, without requiring unrelated pages."""
    decisions = args.get('decisions')
    visible = session.state.get('review_active_read_snapshot')
    if not isinstance(decisions, list):
        return decisions
    uses_labels = any(isinstance(row, dict) and isinstance(row.get('basis_quotes'), list)
        and any(isinstance(q, str) and q.startswith(PREFIX) for q in row['basis_quotes'])
        for row in decisions)
    if not uses_labels and visible is None:
        return decisions  # Existing literal API/headless contract.
    ref = args.get('packet_ref')
    received = session.state.get('review_quote_packets', {}).get(ref)
    if not received and visible is None:
        raise ConflictError('live_fact_review_evidence_invalid',
                            'Read this packet in the current session before choosing its quote_ref labels.')
    received = received or {'packet_ref': ref, 'items': [], 'quote_catalog': {}}
    request = args
    if visible is not None:
        # The shared host sends responses after all calls in one batch. A
        # positive basis must have been issued before this provider batch.
        labels = visible.get(ref, ())
        received = {**received,
            'items': [item for item in received['items'] if item['quote_ref'] in labels],
            'quote_catalog': {q: entry for q, entry in received['quote_catalog'].items() if q in labels}}
        request = deepcopy(args)
        for row in request['decisions']:
            if isinstance(row, dict) and isinstance(row.get('basis_quotes'), list) and any(
                    not isinstance(q, str) or not q.startswith(PREFIX) for q in row['basis_quotes']):
                row['_row_error'] = 'live_fact_review_evidence_invalid'
    return resolve_rows(received, request)['decisions']


def response_schema(packet, public_schema):
    """Constrain fresh private answers without changing the interactive tool."""
    schema = deepcopy(public_schema)
    decisions = schema['properties']['decisions']['items']['properties']
    if packet.get('items'):
        numbers = sorted({item['fact'] for item in packet['items']})
        decisions['fact'] = {'type': 'integer', 'enum': numbers}
        conflicts = schema['properties']['conflicts']
        for side in ('left', 'right'):
            conflicts['items']['properties'][side] = {'type': 'integer', 'enum': numbers,
                'description': 'Exact local fact number from THIS packet, never an eligible inventory index.'}
        if len(numbers) < 2:
            conflicts['maxItems'] = 0
        existing = sorted({row['fact_id'] for row in packet.get('nearby_existing_claims', [])})
        if existing:
            decisions['equivalent_to_existing']['enum'] = existing
            decisions['conflicts_with_existing']['items']['enum'] = existing
        else:
            decisions.pop('equivalent_to_existing', None)
            decisions['conflicts_with_existing']['maxItems'] = 0
    if 'equivalent_to' in decisions and 'items' in packet:
        decisions['equivalent_to'] = {
            'type': ['integer', 'null'],
            'enum': [*sorted({item['fact'] for item in packet['items']}), None],
            'description': 'Optional canonical fact number from THIS packet. Omit or use null for no equivalence; never -1. A canonical may reference itself.'}
    if 'quote_catalog' in packet:
        quotes = decisions['basis_quotes']
        quotes['items'] = {'type': 'string', 'minLength': 1, 'maxLength': 900}
        quotes['description'] = ('Exact frozen quote_ref labels or short literal own-evidence quotations. Choose this fact\'s selected '
                                 'evidence; empty only for unsupported decisions. Labels establish '
                                 'literal addressing, not semantic support.')
    if packet.get('evidence_addressing') == OWN_REFS_CONTRACT:
        scopes = {}
        for item in packet['items']:
            scopes.setdefault(item['fact'], []).append(item['quote_ref'])
        schema['properties']['decisions']['items']['allOf'] = [{'oneOf': [
            {'properties': {'fact': {'const': number},
                'basis_quotes': {'items': {'type': 'string', 'enum': refs}}},
             'required': ['fact']} for number, refs in sorted(scopes.items())]}]
        decisions['basis_quotes']['description'] = ('Choose only received quote_ref labels for THIS fact. '
            'Each label already names its exact own evidence; do not return a second evidence index. '
            'A candidate or paraphrase is not a literal source address.')
        decisions.pop('evidence')
        schema['properties']['decisions']['items']['required'].remove('evidence')
        schema['properties']['decisions']['items']['additionalProperties'] = False
    if packet.get('verifier_presentation') == 'one_assertion_all_own_slices_v1':
        from .headless_review_repairs import REPLACEMENTS_SCHEMA
        decisions['repair_facts'] = deepcopy(REPLACEMENTS_SCHEMA)
        if packet.get('evidence_addressing') == OWN_REFS_CONTRACT:
            replacement = decisions['repair_facts']['items']
            replacement['properties'].pop('evidence')
            replacement['required'].remove('evidence')
            replacement['properties']['quote_refs'] = {'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': {'type': 'string', 'enum': list(packet['quote_catalog'])}}
            replacement['required'].append('quote_refs')
        peers = [row['fact_id'] for row in packet.get('pending_relation_context', [])]
        if peers:
            decisions['related_pending_ids'] = {'type': 'array', 'maxItems': 12,
                'items': {'type': 'string', 'enum': peers}}
        decisions['own_evidence_values'] = {'type': 'array', 'maxItems': 32, 'items': {
            'type': 'object', 'properties': {'property': {'type': 'string'}, 'value': {'type': 'string'},
                'quote_refs': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'enum': list(packet['quote_catalog'])}}},
            'required': ['property', 'value', 'quote_refs'], 'additionalProperties': False}}
        decisions['own_value_conflicts'] = {'type': 'array', 'maxItems': 12, 'items': {'type': 'string', 'maxLength': 500}}
        if packet.get('evidence_addressing') == OWN_REFS_CONTRACT:
            for variant, (_, labels) in zip(schema['properties']['decisions']['items']['allOf'][0]['oneOf'], sorted(scopes.items())):
                refs = {'type': 'array', 'items': {'type': 'string', 'enum': labels}}
                variant['properties']['repair_facts'] = {'items': {'properties': {'quote_refs': refs}}}
                variant['properties']['own_evidence_values'] = {'items': {'properties': {'quote_refs': refs}}}
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
        if decision.get('related_pending_ids') and decision.get('verdict') == 'supported':
            raise ConflictError('live_review_related_group_required',
                'A declared pending relation needs joint own-evidence review before support.')
        decision.pop('related_pending_ids', None)
        decision.pop('repair_facts', None)
        if decision.get('own_value_conflicts') and decision.get('verdict') == 'supported':
            raise ConflictError('live_fact_review_evidence_invalid',
                                'The model declared unresolved own-value conflicts; supported is inconsistent.')
        decision.pop('own_evidence_values', None)
        decision.pop('own_value_conflicts', None)
    return value


def resolve_rows(packet, args):
    """Keep a bad own-quote/check row addressable for product partitioning."""
    # Validate the immutable catalog even when the answer omitted all rows.
    resolve_quotes(packet, {**args, 'decisions': []})
    value = deepcopy(args)
    decisions = value.get('decisions')
    if not isinstance(decisions, list):
        return value
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            continue  # The product row validator rejects it.
        try:
            resolved = resolve_quotes(packet, {**args, 'decisions': [decision]})
            resolved = public_result(packet, resolved)
            decisions[index] = resolved['decisions'][0]
        except ConflictError as exc:
            decisions[index] = {**decision, '_row_error': exc.code}
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


def resolve_quotes(packet, args, *, include_repairs=False):
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
        if packet.get('evidence_addressing') == OWN_REFS_CONTRACT:
            def own_indices(refs):
                if not isinstance(refs, list):
                    raise ConflictError('live_fact_review_evidence_invalid', 'Use received own quote references.')
                indices = []
                for ref in refs:
                    entry = catalog.get(ref) if isinstance(ref, str) else None
                    if not isinstance(entry, dict) or ref not in literals or entry['fact'] != decision.get('fact'):
                        raise ConflictError('live_fact_review_evidence_invalid', 'Quote reference belongs to another or unknown assertion.')
                    if entry['evidence'] not in indices:
                        indices.append(entry['evidence'])
                return indices
            indices = own_indices(decision.get('basis_quotes'))
            # No repair of conflicting redundant pointers, including bypassed
            # private responses. Only the issued reference supplies this address.
            if 'evidence' in decision and decision['evidence'] != indices:
                raise ConflictError('live_fact_review_evidence_invalid', 'Redundant evidence selection conflicts with own references.')
            decision['evidence'] = indices
            for child in decision.get('repair_facts', []) if include_repairs else []:
                if not isinstance(child, dict):
                    raise ConflictError('live_fact_review_evidence_invalid', 'Malformed own-reference replacement.')
                refs = child.pop('quote_refs', None)
                indices = own_indices(refs)
                if 'evidence' in child and child['evidence'] != indices:
                    raise ConflictError('live_fact_review_evidence_invalid', 'Replacement evidence conflicts with own references.')
                child['evidence'] = indices
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
