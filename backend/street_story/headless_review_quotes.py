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
