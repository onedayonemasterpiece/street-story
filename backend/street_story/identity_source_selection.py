"""Observed-URL selection, never source or physical-identity invention."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .opencode_research import SEARCH_SCHEMA, selected_search_sources


IDENTITY_SOURCE_POLICY = (
    'The target is the present-day physical building in SOURCE. A street/address/name is only a search hypothesis. '
    'Use supplied observed locality, complete address anchors and exact building/entrance memberships to compare '
    'plausible alternatives. Preserve city, street type and house-number suffix/range in every query. '
    'Prefer concrete records and modern exterior photographs of those address hypotheses. '
    'A building reported demolished/destroyed or visible only in archival photographs cannot provide a modern '
    'reference for the surviving SOURCE building; reject it as an identity lead unless the source explicitly '
    'documents the current physical replacement/restoration and shows its modern exterior. '
    'Reject same-named streets in another locality, general architecture/district summaries, and broad landmark '
    'lists when they do not show a plausible current physical subject. '
    'Do not promote an entrance, tenant, address label, article title or historical name to building identity. '
    'A housing/repair record with the exact address may be a useful lead; require accessible modern exterior '
    'evidence before comparing it. Empty selection is valid. These preferences are not identity proof. '
    'Use SOURCE appearance to choose a specific physical facade/corpus; a map, logo, bridge or archival city '
    'scene is useful only when it depicts the actual kind of subject visible in SOURCE. A rendering may be '
    'useful when its physical geometry applies; decide this semantically, not by file type. '
)


def _short(value, limit=120):
    return str(value or '')[:limit]


def _number(value, digits=1):
    try:
        import math
        number = float(value)
        return round(number, digits) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def compact_candidate_catalog(candidates):
    """All exact IDs and geometry summaries; full map objects stay durable.

    Column tables avoid repeating schema/provenance and raw OSM node arrays per
    candidate. Nomination dereferences the original object, not these summaries.
    """
    rows = []
    for entry in candidates:
        if not isinstance(entry, dict) or not entry.get('candidate_id'):
            continue
        address = entry.get('map_address') or {}
        coordinates = entry.get('map_coordinates') or entry.get('center') or {}
        tags = (entry.get('map_object') or {}).get('tags') or {}
        interval = entry.get('footprint_bearing_interval') or {}
        alignment = entry.get('camera_alignment') or {}
        # Never copy arbitrary OSM tags (some contain long descriptions), raw
        # node/geometry arrays, search excerpts, URLs or image inventories.
        kind = {key: _short(tags[key], 60) for key in
            ('building', 'entrance', 'amenity', 'shop', 'office', 'historic') if tags.get(key)}
        mapped = entry.get('map_object') or {}
        classification = {key: _short(mapped[key], 100) for key in
            ('category', 'class', 'type', 'addresstype') if mapped.get(key)}
        if classification:
            kind.update(classification)
            kind.update({key: _short(mapped[key], 80) for key in ('scope', 'provenance') if mapped.get(key)})
        camera = {key: alignment[key] for key in
            ('status', 'bearing_degrees', 'relative_bearing_degrees', 'within_fov', 'heading_difference_degrees')
            if isinstance(alignment, dict) and key in alignment and isinstance(alignment[key], (str, int, float, bool))}
        if isinstance(alignment, str):
            camera['status'] = alignment
        if entry.get('camera_direction_difference_deg') is not None:
            camera['heading_difference_degrees'] = _number(entry['camera_direction_difference_deg'])
        camera = {key: _short(value, 60) if isinstance(value, str) else value for key, value in camera.items()}
        literal_address = [_short(address.get(key)) for key in ('city', 'street', 'house_number')]
        point = [_number(coordinates.get('latitude', coordinates.get('lat')), 6),
            _number(coordinates.get('longitude', coordinates.get('lon')), 6)]
        bearings = [_number(interval.get(key)) for key in ('start_degrees', 'end_degrees', 'angular_span_degrees')]
        rows.append([entry['candidate_id'], _short(entry.get('name'), 100),
            literal_address if any(literal_address) else None,
            point if any(value is not None for value in point) else None,
            _number(entry.get('distance_m')), _number(entry.get('boundary_distance_m')),
            bearings if any(value is not None for value in bearings) else None,
            camera or None, kind or None, entry.get('identity_eligible') is not False])
    return {'columns': ['candidate_id', 'name', 'address_city_street_house_number', 'latitude_longitude',
        'distance_m', 'boundary_distance_m', 'bearing_start_end_span_degrees', 'camera_alignment',
        'object_tags', 'identity_eligible'], 'rows': rows,
        'geometry_policy': 'All supplied exact IDs are retained. Rounded coordinates/bearings are search hints. '
            'Full original geometry and provenance remain durable and are retrieved by nominated ID; no SOURCE binding.'}


def model_identity_context(story, candidates=(), *, include_observed=True):
    """Bounded semantic packet without raw OSM or repeated address objects."""
    research = json.loads((story or {}).get('research_json') or '{}')
    observed = (story or {}).get('_identity_observed_candidates') or (
        research.get('visual_identity') or {}).get('observed_candidates') or []
    active = (research.get('visual_identity') or {}).get('candidates') or []
    nearby = ((story or {}).get('_identity_search_context') or {}).get('nearby') or []
    by_id = {item['candidate_id']: item for item in [*nearby, *active, *observed, *candidates]
        if isinstance(item, dict) and item.get('candidate_id')}
    addresses = observed_address_context(story, [*nearby, *active, *candidates])
    if not include_observed:
        # Source selection cannot nominate physical IDs. Keep active hypotheses
        # and exact address-linked buildings; distant unaddressed catalog rows
        # stay available to the planner rather than repeating in every selector.
        needed = {item.get('candidate_id') for item in [*active, *candidates]}
        needed.update(item['physical_candidate_id'] for item in addresses['building_address_memberships'])
        by_id = {key: value for key, value in by_id.items() if key in needed}
    reverse = (((story or {}).get('_identity_search_context') or {}).get('reverse_address') or
        ((research.get('osm') or {}).get('reverse') or {}).get('address') or {})
    packet = {'observed_localities': [_short(value) for value in addresses['observed_localities']],
        'reverse_address': {key: _short(reverse[key]) for key in
            ('city', 'town', 'village', 'state', 'country', 'road', 'house_number') if reverse.get(key)},
        'nearby_context': [{**({'candidate_id': item['candidate_id']} if item.get('candidate_id') else {}),
            **({'road_name': _short(item['road_name'])} if item.get('road_name') else {}),
            **({'observed_name': _short((item.get('tags') or {}).get('name'))} if (item.get('tags') or {}).get('name') else {}),
            'distance_m': _number(item.get('distance_m'))} for item in nearby[:20]],
        'address_anchors': {'columns': ['mapped_entry_id', 'city', 'street', 'house_number', 'distance_m', 'entry_kind', 'latitude_longitude'],
            'rows': [[item['mapped_entry_id'], *[_short(item['address'].get(key)) for key in
                ('city', 'street', 'house_number')], _number(item['distance_m']), item['entry_kind'],
                [_number((item.get('map_coordinates') or {}).get(key), 6) for key in ('latitude', 'longitude')]]
                for item in addresses['address_anchors']]},
        'building_address_memberships': [{'physical_candidate_id': item['physical_candidate_id'],
            'address_entry_ids': [anchor['mapped_entry_id'] for anchor in item['address_entries']],
            'proof': 'osm_closed_way_node_membership'} for item in addresses['building_address_memberships']],
        'observed_physical_candidates': compact_candidate_catalog(list(by_id.values())),
        'policy': addresses['policy']}
    # Fail explicitly instead of silently losing candidates. Radius/pool bounds
    # normally keep this well below the envelope; all exact IDs remain present.
    if len(json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode()) > 65536:
        from .providers import RetryableProviderError
        raise RetryableProviderError('identity_semantic_packet_too_large')
    return packet


def first_wave_catalog(story, candidates=()):
    """Literal search subjects and coverage groups, never SOURCE identities."""
    from .identity_candidate_policy import candidate_identity_eligible
    research = json.loads((story or {}).get('research_json') or '{}')
    visual = research.get('visual_identity') or {}
    context = (story or {}).get('_identity_search_context') or {}
    observed = (story or {}).get('_identity_observed_candidates') or visual.get('observed_candidates') or []
    entries = {item['candidate_id']: item for item in [*(context.get('nearby') or []),
        *(visual.get('candidates') or []), *observed, *candidates]
        if isinstance(item, dict) and item.get('candidate_id')}
    physical_ids = {item.get('candidate_id') for item in [*(visual.get('candidates') or []), *observed, *candidates]
        if isinstance(item, dict)}
    addresses = observed_address_context(story, list(entries.values()))
    memberships = {}
    for building in addresses['building_address_memberships']:
        cid = building['physical_candidate_id']
        if cid in entries and not candidate_identity_eligible(entries[cid]):
            continue
        for anchor in building['address_entries']:
            memberships.setdefault(anchor['mapped_entry_id'], set()).add(cid)
    reverse = context.get('reverse_address') or ((research.get('osm') or {}).get('reverse') or {}).get('address') or {}
    locality = next((str(reverse[key]).strip() for key in ('city', 'town', 'village') if reverse.get(key)), '')
    options = {}
    for anchor in addresses['address_anchors']:
        cid = anchor['mapped_entry_id']
        linked = memberships.get(cid) or set()
        # Ambiguous/missing membership remains an entry hypothesis. A direct
        # building address and an exact entrance membership share one group.
        group = (next(iter(linked)) if len(linked) == 1 else cid
            if anchor['entry_kind'] == 'building' and candidate_identity_eligible(entries.get(cid, {}))
            else 'address-entry:' + cid)
        address = anchor['address']
        city = str(address.get('city') or '').strip()
        literal = ' '.join(str(value).strip() for value in
            (city or locality, address['street'], address['house_number']) if str(value).strip())
        options[('address', cid)] = {'kind': 'address', 'subject_id': cid, 'group_key': group,
            'literal_query': literal, 'address': dict(address),
            'locality_context': locality if not city else '', 'scope': 'search_hypothesis_only'}
    for cid, entry in entries.items():
        tags = (entry.get('map_object') or {}).get('tags') or {}
        if cid not in physical_ids or not candidate_identity_eligible(entry) or tags.get('entrance'):
            continue
        # A host-generated unnamed-building/address display label is not a
        # literal named object. Preserve only observed tags or article titles.
        name = tags.get('name') or (entry.get('name') if entry.get('type') == 'wikipedia' else '')
        if not name:
            continue
        city = (entry.get('map_address') or {}).get('city') or locality
        linked = memberships.get(cid) or set()
        group = next(iter(linked)) if len(linked) == 1 else cid
        occupant = (str(tags.get('building') or '').casefold() in {'', 'no'}
            and any(tags.get(key) for key in ('amenity', 'shop', 'office')))
        options[('observed_named', cid)] = {'kind': 'observed_named', 'subject_id': cid,
            'group_key': '' if occupant else group,
            'coverage_scope': 'mapped_occupant_context' if occupant else 'observed_subject_hypothesis',
            'literal_query': ' '.join(str(value).strip() for value in (name, city) if value),
            'scope': 'search_hypothesis_only'}
    groups = {option['group_key'] for option in options.values() if option['group_key']}
    return {'options': options, 'required_grounded_count': min(2, len(groups)), 'locality_context': locality}


def first_wave_schema(catalog):
    return {'type': 'array', 'minItems': catalog['required_grounded_count'], 'maxItems': 3,
        'items': {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['address', 'observed_named', 'unmapped_named', 'appearance']},
            'subject_id': {'type': 'string', 'enum': list(dict.fromkeys([
                *(option['subject_id'] for option in catalog['options'].values()), '']))},
            'query': {'type': 'string', 'maxLength': 240},
            'reason': {'type': 'string', 'maxLength': 240}},
            'required': ['kind', 'subject_id', 'query', 'reason'], 'additionalProperties': False}}


def render_first_wave(catalog, hypotheses):
    """Dereference model choices; enforce coverage without interpreting prose."""
    from .providers import RetryableProviderError
    rendered, groups, queries = [], set(), set()
    for hypothesis in hypotheses:
        kind, subject = hypothesis['kind'], hypothesis['subject_id']
        option = catalog['options'].get((kind, subject))
        if kind in {'address', 'observed_named'}:
            if not option:
                raise RetryableProviderError('identity_first_wave_unobserved_subject')
            group = option['group_key']
            if group and group in groups:
                raise RetryableProviderError('identity_first_wave_duplicate_group')
            if group:
                groups.add(group)
            query = option['literal_query']
            if not query or len(query) > 240:
                raise RetryableProviderError('identity_first_wave_literal_too_long')
        else:
            if subject or not hypothesis['query'].strip():
                raise RetryableProviderError('identity_first_wave_unmapped_subject_invalid')
            query = ' '.join(hypothesis['query'].split())
            group = ''  # Unmapped names/appearance cannot inflate mapped coverage.
        normalized = ' '.join(query.split()).casefold()
        if normalized in queries:
            raise RetryableProviderError('identity_first_wave_duplicate_query')
        queries.add(normalized)
        rendered.append({**hypothesis, 'query': query, 'group_key': group,
            **({'literal_subject': {key: value for key, value in option.items()
                if key in {'address', 'locality_context', 'scope', 'coverage_scope'}}} if option else {})})
    if len(groups) < catalog['required_grounded_count']:
        raise RetryableProviderError('identity_first_wave_coverage_incomplete')
    # Put the required coverage into the earliest transport slots. This is a
    # stable partition of model selections, not a ranking of candidate truth.
    return [item for item in rendered if item['group_key']] + [item for item in rendered if not item['group_key']]


def observed_address_context(story, candidates=()):
    """Literal anchors and exact OSM memberships, never inferred house addresses."""
    research = json.loads((story or {}).get('research_json') or '{}')
    observed = (story or {}).get('_identity_observed_candidates') or (
        research.get('visual_identity') or {}).get('observed_candidates') or []
    entries = {item['candidate_id']: item for item in [*observed, *candidates]
        if isinstance(item, dict) and item.get('candidate_id')}
    for item in (research.get('osm') or {}).get('observed_pool') or []:
        if item.get('type') not in {'node', 'way', 'relation'} or item.get('id') is None:
            continue
        cid = f"osm:{item['type']}:{item['id']}"
        tags = item.get('tags') or {}
        literal_address = {key: tags[value] for key, value in
            [('city', 'addr:city'), ('street', 'addr:street'), ('house_number', 'addr:housenumber')]
            if tags.get(value)}
        if cid in entries:
            # City tags can be omitted by the compact map DTO. Restore only
            # literal tags of this same observed ID, never another entry's city.
            entries[cid] = {**entries[cid], 'map_address': {
                **literal_address, **(entries[cid].get('map_address') or {})}}
            continue
        entries[cid] = {'candidate_id': cid, 'distance_m': item.get('distance_m'),
            'map_address': literal_address, 'map_object': {'tags': tags}}
    anchors = []
    for cid, entry in entries.items():
        address = entry.get('map_address') or {}
        if not address.get('street') or not address.get('house_number'):
            continue
        tags = (entry.get('map_object') or {}).get('tags') or {}
        anchors.append({'mapped_entry_id': cid, 'address': dict(address),
            'map_coordinates': entry.get('map_coordinates'), 'distance_m': entry.get('distance_m'),
            'entry_kind': 'entrance' if tags.get('entrance') else 'building' if tags.get('building') else 'mapped_entry',
            'scope': 'mapped_entry_only'})
    def distance(anchor):
        try:
            return float(anchor.get('distance_m'))
        except (TypeError, ValueError):
            return float('inf')
    anchors.sort(key=lambda anchor: (distance(anchor), anchor['mapped_entry_id']))
    by_id = {anchor['mapped_entry_id']: anchor for anchor in anchors}
    buildings = []
    for cid, entry in entries.items():
        membership = (entry.get('map_object') or {}).get('building_entrances') or {}
        if membership.get('proof') != 'osm_closed_way_node_membership':
            continue
        linked = [by_id[key] for key in membership.get('candidate_ids') or [] if key in by_id]
        if linked:
            buildings.append({'physical_candidate_id': cid, 'address_entries': linked,
                'membership_proof': dict(membership), 'boundary_distance_m': entry.get('boundary_distance_m'),
                'footprint_bearing_interval': entry.get('footprint_bearing_interval'),
                'camera_alignment': entry.get('camera_alignment')})
    localities = list(dict.fromkeys([*regional_source_profile(story, candidates)['observed_localities'],
        *(str(anchor['address'][key]).strip() for anchor in anchors
            for key in ('city', 'town', 'village', 'state', 'country') if anchor['address'].get(key))]))
    return {'observed_localities': localities,
        'address_anchors': anchors, 'building_address_memberships': buildings,
        'policy': 'Distances order observations only. Membership joins these mapped entries to this observed '
            'building footprint; it does not identify SOURCE or imply one combined house-number range.'}


def regional_source_profile(story, candidates=()):
    """Source preferences from observed map locality, never photo answers."""
    context = (story or {}).get('_identity_search_context') or {}
    research = json.loads((story or {}).get('research_json') or '{}')
    addresses = [context.get('reverse_address') or {},
                 ((research.get('osm') or {}).get('reverse') or {}).get('address') or {}]
    addresses.extend(item.get('map_address') or {} for item in
                     [*(context.get('nearby') or []), *candidates])
    localities = list(dict.fromkeys(str(address[key]).strip() for address in addresses
        if isinstance(address, dict) for key in ('city', 'town', 'village', 'state', 'region', 'country')
        if address.get(key)))
    profile = {'observed_localities': localities,
        'preferences': ['official physical-object or operator pages with address evidence',
                        'municipal heritage registers', 'housing records and documented repairs'],
        'policy': 'Preferences are discovery hints, not truth or an allowlist; preserve subject and date distinctions.'}
    # This is a regional catalog, not a building/name/address lookup. Match only
    # locality fields already observed in map records; unknown geography stays unknown.
    if any('калининград' in value.casefold() or 'kaliningrad' in value.casefold() for value in localities):
        profile['regional_sources'] = [
            {'domain': 'prussia39.ru', 'use': 'local history, aliases and modern reference photographs'},
            {'domain': 'fkr39.ru', 'use': 'building repair records and dated reports'},
            {'domain': 'visit-kaliningrad.ru', 'use': 'object pages and photographs'},
            {'domain': 'gov39.ru', 'use': 'heritage registers and official documents'},
            {'domain': 'klgd.ru', 'use': 'municipal records'},
            {'domain': 'kgd.ru', 'use': 'dated local reporting'},
            {'domain': 'newkaliningrad.ru', 'use': 'dated local reporting'}]
    return profile


def indexed_selection_schema(count):
    return {'type': 'object', 'properties': {
        'summary': {'type': 'string', 'maxLength': 2000},
        'selected_sources': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'source_index': {'type': 'integer', 'enum': list(range(count))},
            'reason': {'type': 'string', 'maxLength': 400}},
            'required': ['source_index', 'reason'], 'additionalProperties': False}}},
        'required': ['summary', 'selected_sources'], 'additionalProperties': False}


def indexed_model_selection(observed, payload):
    # Only the model chooses pages. Code dereferences its inventory indices,
    # preserving exact observed URL bytes rather than asking it to copy URLs.
    if not Draft202012Validator(indexed_selection_schema(len(observed))).is_valid(payload):
        return [], {'status': 'selection_unavailable', 'code': 'malformed_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0}
    return model_selection(observed, {'summary': payload['summary'], 'selected_sources': [
        {'url': observed[item['source_index']]['url'], 'reason': item['reason']}
        for item in payload['selected_sources']]})


def model_selection(observed, payload):
    if (not Draft202012Validator(SEARCH_SCHEMA).is_valid(payload)
            or any(not item['reason'].strip() for item in payload['selected_sources'])):
        return [], {'status': 'selection_unavailable', 'code': 'malformed_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0}
    chosen, rejected = selected_search_sources(observed, payload)
    if rejected and not chosen:
        return [], {'status': 'selection_unavailable', 'code': 'unobserved_source_selection',
                    'discovered_count': len(observed), 'selected_count': 0, 'unobserved_count': rejected}
    return chosen, {'status': 'model_selected', 'discovered_count': len(observed),
                    'selected_count': len(chosen), 'unobserved_count': rejected}


def response_selection(observed, text):
    text = str(text or '').strip()
    if text.startswith('```json') and text.endswith('```'):
        text = text[7:-3].strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    return model_selection(observed, payload)
