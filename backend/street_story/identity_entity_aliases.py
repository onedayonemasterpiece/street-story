"""Enrich physical hypotheses from exact public entity links, never name proximity.

An institution housed at a landmark remains a distinct entity. A host-reviewed
public location relation can make it context for physical-object identification;
this is not a canonical POI alias and cannot merge the institution's registry.
"""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from urllib.parse import quote, unquote, urlsplit


def wikipedia_entity_url(raw):
    try:
        parsed = urlsplit(str(raw or ''))
        match = re.fullmatch(r'([a-z][a-z-]{1,11})(?:\.m)?\.wikipedia\.org', parsed.hostname or '')
        if (parsed.scheme != 'https' or not match or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.path.startswith('/wiki/') or parsed.query or parsed.fragment):
            return None
        title = unquote(parsed.path[6:]).replace(' ', '_')
        return f'https://{match[1]}.wikipedia.org/wiki/{quote(title, safe="/")}' if title else None
    except (TypeError, ValueError):
        return None


def _wikipedia_tag(raw):
    language, separator, title = str(raw or '').partition(':')
    if separator and re.fullmatch(r'[a-z][a-z-]{1,11}', language) and title.strip():
        return wikipedia_entity_url(f'https://{language}.wikipedia.org/wiki/{quote(title.strip().replace(" ", "_"), safe="/")}')
    return None


def _website(raw):
    try:
        parsed = urlsplit(str(raw or ''))
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None
        return parsed.hostname.casefold(), parsed.path.rstrip('/')
    except ValueError:
        return None


def enrich_entity_links(candidates, osm, wikipedia, *, institutional_locations=()):
    """Copy catalog, joining metadata only by exact OSM ID or Wiki page ID.

    Rich landmark tags may follow a reverse-geocoding duplicate with no tags.
    ``institutional_locations`` is exclusively host-owned reviewed evidence:
    callers must never forward arbitrary model/tool arguments into this input.
    Each relation carries its official source text/hash/quote and the explicit
    location interpretation. Exact address and entity types are additional
    checks; they do not establish the relationship themselves.
    """
    result = deepcopy(candidates)
    by_id = {item.get('candidate_id'): item for item in result}
    raw_osm = {}
    for raw in [osm.get('reverse') or {}, *(osm.get('nearby') or [])]:
        kind = raw.get('osm_type') or raw.get('type')
        identifier = raw.get('osm_id') or raw.get('id')
        if kind in {'node', 'way', 'relation'} and identifier is not None:
            cid = f'osm:{kind}:{identifier}'
            raw_osm.setdefault(cid, []).append(raw)
    tags_by_id = {}
    for cid, rows in raw_osm.items():
        tags = {}
        conflicts = set()
        for row in rows:
            for key, value in (row.get('tags') or {}).items():
                if not isinstance(value, str) or not value.strip():
                    continue
                if key in tags and tags[key] != value:
                    conflicts.add(key)
                tags[key] = value
        tags_by_id[cid] = {key: value for key, value in tags.items() if key not in conflicts}
        candidate = by_id.get(cid)
        if candidate is None:
            continue
        candidate['osm_id'] = cid
        candidate['entity_link_evidence'] = list(candidate.get('entity_link_evidence') or [])
        if conflicts & {'wikidata', 'wikipedia'}:
            candidate['entity_link_conflicts'] = sorted(conflicts & {'wikidata', 'wikipedia'})
            candidate.pop('wikidata', None)
            candidate.pop('wikipedia_url', None)
            continue
        for field, value, tag in (
                ('wikidata', tags_by_id[cid].get('wikidata'), 'wikidata'),
                ('wikipedia_url', _wikipedia_tag(tags_by_id[cid].get('wikipedia')), 'wikipedia')):
            if not value or (field == 'wikidata' and not re.fullmatch(r'Q[1-9]\d*', value)):
                continue
            current = candidate.get(field)
            if current and current != value:
                candidate.setdefault('entity_link_conflicts', []).append(field)
                candidate.pop(field, None)
                continue
            candidate[field] = value
            candidate['entity_link_evidence'].append({'predicate': 'exact_entity_link', 'field': field,
                'value': value, 'origin': f'osm.tags.{tag}', 'source_url': candidate.get('url'), 'candidate_id': cid})
        if candidate.get('selection_bucket') == 'reverse' and tags_by_id[cid].get('name'):
            candidate.setdefault('display_name', candidate.get('name'))
            candidate['name'] = tags_by_id[cid]['name']
    for page in wikipedia:
        candidate = by_id.get(f"wiki:{page.get('pageid')}")
        if candidate is None:
            continue
        canonical = wikipedia_entity_url(page.get('url') or page.get('fullurl') or candidate.get('url'))
        if canonical:
            candidate['wikipedia_url'] = canonical
        item_id = (page.get('pageprops') or {}).get('wikibase_item') or page.get('wikidata')
        if isinstance(item_id, str) and re.fullmatch(r'Q[1-9]\d*', item_id):
            if candidate.get('wikidata') not in (None, '', item_id):
                candidate.setdefault('entity_link_conflicts', []).append('wikidata')
                candidate.pop('wikidata', None)
                continue
            candidate['wikidata'] = item_id
            candidate.setdefault('entity_link_evidence', []).append({'predicate': 'exact_entity_link',
                'field': 'wikidata', 'value': item_id, 'origin': 'wikipedia.pageprops.wikibase_item',
                'source_url': page.get('url') or page.get('fullurl') or candidate.get('url')})
    for relation in institutional_locations:
        if not isinstance(relation, dict) or relation.get('proof') != 'host_reviewed_official_location':
            continue
        institution = by_id.get(relation.get('institution_candidate_id'))
        subject = by_id.get(relation.get('physical_subject_candidate_id'))
        if (not institution or not subject or institution is subject
                or relation.get('relation') != 'institution_housed_in_physical_object'):
            continue
        institution_tags = tags_by_id.get(institution['candidate_id'], {})
        subject_tags = tags_by_id.get(subject['candidate_id'], {})
        if (institution_tags.get('amenity') not in {'arts_centre', 'theatre', 'library'}
                and institution_tags.get('tourism') not in {'museum', 'gallery'}):
            continue
        if not subject_tags.get('building') or subject_tags.get('building') == 'no':
            continue
        address_keys = ('addr:city', 'addr:street', 'addr:housenumber')
        if any(not institution_tags.get(key) or not subject_tags.get(key)
               or institution_tags[key].casefold() != subject_tags[key].casefold() for key in address_keys):
            continue
        document, excerpt = relation.get('source_text'), relation.get('source_quote')
        if (not isinstance(document, str) or not isinstance(excerpt, str) or not excerpt.strip()
                or excerpt not in document or hashlib.sha256(document.encode()).hexdigest() != relation.get('source_sha256')
                or not _website(institution_tags.get('website'))
                or _website(relation.get('source_url')) != _website(institution_tags.get('website'))):
            continue
        institution.update(identity_role='institution_at_physical_subject', identity_eligible=False,
                           physical_subject_candidate_id=subject['candidate_id'])
        institution['physical_subject_evidence'] = {key: value for key, value in relation.items() if key != 'source_text'}
    return result
