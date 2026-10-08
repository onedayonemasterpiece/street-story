"""Mechanical, evidence-bound article subject resolution; names are never identity keys."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .identity_candidate_policy import candidate_identity_eligible


def article_candidate(candidate: dict[str, Any]) -> bool:
    return str(candidate.get('candidate_id') or '').startswith('web:')


def documented_physical_subject(candidate: dict[str, Any], candidates: Mapping[str, dict[str, Any]]) -> str | None:
    """Read the host-validated official location receipt, never infer a building.

    An institution housed in a building is distinct from that building. The
    receipt relates their physical subjects without creating an entity alias.
    """
    evidence = candidate.get('physical_subject_evidence')
    subject_id = candidate.get('physical_subject_candidate_id')
    subject = candidates.get(subject_id) if isinstance(subject_id, str) else None
    if not isinstance(evidence, dict) or not subject:
        return None
    try:
        source = urlparse(str(evidence.get('source_url') or ''))
    except ValueError:
        return None
    article_hash = evidence.get('source_sha256')
    quote = evidence.get('source_quote')
    if (candidate.get('identity_role') != 'institution_at_physical_subject'
            or candidate.get('identity_eligible') is not False
            or evidence.get('proof') != 'host_reviewed_official_location'
            or evidence.get('relation') != 'institution_housed_in_physical_object'
            or evidence.get('institution_candidate_id') != candidate.get('candidate_id')
            or evidence.get('physical_subject_candidate_id') != subject_id
            or subject_id == candidate.get('candidate_id')
            or article_candidate(subject) or not candidate_identity_eligible(subject)
            or source.scheme not in {'http', 'https'} or not source.hostname
            or not isinstance(quote, str) or not quote.strip()
            or not isinstance(article_hash, str) or len(article_hash) != 64
            or any(character not in '0123456789abcdef' for character in article_hash)):
        return None
    return subject_id


def physical_alternative_id(candidate_id: str, candidates: Mapping[str, dict[str, Any]]) -> str | None:
    """Normalize documented hosted venues; unknown venue relationships compete."""
    candidate = candidates.get(candidate_id) or {}
    subject_id = documented_physical_subject(candidate, candidates)
    if subject_id:
        return subject_id
    if (candidate.get('identity_role') == 'institution_at_physical_subject'
            or candidate.get('physical_subject_candidate_id') or candidate.get('physical_subject_evidence')):
        return candidate_id
    return candidate_id if candidate_identity_eligible(candidate) else None


def subject_aliases(candidates: list[dict[str, Any]], *, poi_aliases: Mapping[str, str] | None = None) -> dict[str, set[str]]:
    """Exact entity links or established registry/cluster memberships only.

    ``poi_aliases`` must come from the existing registry's candidate namespace,
    not model output or name aliases. Article addresses do not identify entities.
    """
    parent: dict[str, str] = {}
    by_id = {item.get('candidate_id'): item for item in candidates}
    institutions = {cid for cid, item in by_id.items() if documented_physical_subject(item, by_id)}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def join(left: str, right: str) -> None:
        parent[find(right)] = find(left)

    owners: dict[tuple[str, str], str] = {}
    entrance_buildings: dict[str, set[str]] = {}
    for candidate in candidates:
        mapped = candidate.get('map_object') or {}
        receipt = mapped.get('building_entrances') or {}
        cid = candidate.get('candidate_id') or ''
        if (cid.startswith('osm:way:') and mapped.get('provenance') == 'osm.tags'
                and (mapped.get('tags') or {}).get('building') not in {None, '', 'no'}
                and receipt.get('proof') == 'osm_closed_way_node_membership'
                and receipt.get('source_url') == candidate.get('url') == mapped.get('source_url')):
            for entrance_id in receipt.get('candidate_ids') or []:
                entrance = by_id.get(entrance_id) or {}
                entrance_map = entrance.get('map_object') or {}
                tags = entrance_map.get('tags') or {}
                if (str(entrance_id).startswith('osm:node:') and entrance_map.get('provenance') == 'osm.tags'
                        and tags.get('entrance') not in {None, '', 'no'}
                        and not any(tags.get(key) for key in ('amenity', 'shop', 'office'))):
                    entrance_buildings.setdefault(entrance_id, set()).add(cid)
    for candidate in candidates:
        cid = str(candidate.get('candidate_id') or '')
        if not cid or article_candidate(candidate):
            continue
        find(cid)
        if cid in institutions:
            continue
        keys = [('wikidata', candidate.get('wikidata')), ('osm_id', candidate.get('osm_id')),
                ('wikipedia_url', candidate.get('wikipedia_url'))]
        if cid.startswith('wiki:'):
            keys.append(('wikipedia_url', candidate.get('url')))
        if cid.startswith('osm:'):
            keys.append(('osm_id', cid))
        if poi_aliases and poi_aliases.get(cid):
            keys.append(('registry_poi', poi_aliases[cid]))
        for namespace, raw in keys:
            value = str(raw or '').strip()
            if not value:
                continue
            key = namespace, value
            if key in owners:
                join(cid, owners[key])
            else:
                owners[key] = cid
        if candidate.get('discovery') == 'wikimedia_entity_cluster':
            for alias in candidate.get('alias_candidate_ids') or []:
                if isinstance(alias, str) and alias and not alias.startswith('web:') and alias not in institutions:
                    join(cid, alias)
    for entrance, buildings in entrance_buildings.items():
        if len(buildings) == 1:
            join(next(iter(buildings)), entrance)
    groups: dict[str, set[str]] = {}
    for cid in parent:
        groups.setdefault(find(cid), set()).add(cid)
    return {cid: groups[find(cid)] for cid in parent}


def reference_binding_valid(result: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    """Check the host receipt produced below; never accept this field from a model."""
    binding = result.get('_reference_subject_binding')
    if not isinstance(binding, dict) or binding.get('proof') != 'model_reference_subject_resolution':
        return False
    subject = result.get('candidate_id')
    reference = binding.get('reference_candidate_id')
    by_id = {item.get('candidate_id'): item for item in candidates}
    candidate = by_id.get(subject)
    source = by_id.get(reference)
    return bool(candidate and not article_candidate(candidate) and candidate_identity_eligible(candidate)
                and source and article_candidate(source)
                and binding.get('subject_candidate_id') == subject
                and isinstance(reference, str) and reference.startswith('web:')
                and reference in result.get('_references_sent', [])
                and binding.get('article_url') == source.get('url')
                and binding.get('image_url') in (source.get('reference_image_urls') or [])
                and binding.get('reference_id') in result.get('_reference_ids_sent', []))


def bind_reference_subject(
    result: dict[str, Any], sent_candidates: list[dict[str, Any]], full_shortlist: list[dict[str, Any]],
    reference_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a matched article illustration to an existing eligible hypothesis.

    The model selects a sent reference via ``candidate_id`` and explicitly names
    ``reference_subject_candidate_id`` from the supplied shortlist. Matching a
    page's title alone cannot create a physical POI. This helper does not itself
    assert visual match: the caller still applies the unchanged confidence,
    observations, alternatives and generation/photo acceptance checks.
    """
    # Internal proof is exclusively host-owned, including on unresolved output.
    raw = {key: value for key, value in result.items() if key != '_reference_subject_binding'}
    selected = next((item for item in sent_candidates if item.get('candidate_id') == raw.get('candidate_id')), None)

    def unresolved(reason: str) -> dict[str, Any]:
        return {'status': 'unresolved', 'reason': reason,
                'result': {**raw, 'status': 'uncertain' if raw.get('status') == 'match' else raw.get('status')}}

    if not selected or selected.get('candidate_id') not in raw.get('_references_sent', []):
        return unresolved('reference_not_sent')
    def occupant_in_building(subject):
        mapped = subject.get('map_object') or {}
        tags = mapped.get('tags') or {}
        return (raw.get('source_subject_scope') == 'building' and mapped.get('provenance') == 'osm.tags'
                and str(tags.get('building') or '').casefold() in {'', 'no'}
                and any(tags.get(key) for key in ('amenity', 'shop', 'office')))
    if not article_candidate(selected):
        if occupant_in_building(selected):
            return unresolved('mapped_occupant_is_not_building_subject')
        return {'status': 'not_required', 'candidate': selected, 'result': raw,
                'reference_evidence': reference_evidence}
    subject_id = raw.get('reference_subject_candidate_id')
    candidate = next((item for item in full_shortlist if item.get('candidate_id') == subject_id), None)
    if not candidate or article_candidate(candidate) or not candidate_identity_eligible(candidate):
        return unresolved('subject_not_eligible_shortlist_candidate')
    # This is a typed entity-scope check, not a guess from a venue name. The
    # model identifies SOURCE's subject; the map receipt identifies what its
    # selected ID denotes. A mapped occupant cannot stand for the whole house.
    if occupant_in_building(candidate):
        return unresolved('mapped_occupant_is_not_building_subject')
    cid = selected['candidate_id']
    evidence = next((item for item in reference_evidence
                     if item.get('candidate_id') == cid
                     and item.get('source_url') in (selected.get('reference_image_urls') or [])
                     and item.get('article_url') == selected.get('url')
                     and item.get('reference_id') == selected.get('reference_id')
                     and item.get('reference_id') in raw.get('_reference_ids_sent', [])), None)
    if not evidence:
        return unresolved('reference_provenance_missing')
    binding = {'proof': 'model_reference_subject_resolution', 'reference_candidate_id': cid,
               'subject_candidate_id': subject_id, 'article_url': evidence['article_url'],
               'image_url': evidence['source_url'], 'reference_id': evidence['reference_id']}
    return {'status': 'bound', 'candidate': candidate, 'binding': binding,
            'result': {**raw, 'candidate_id': subject_id, '_reference_subject_binding': binding},
            'reference_evidence': [{**item, 'subject_candidate_id': subject_id}
                                   for item in reference_evidence if item.get('candidate_id') == cid]}
