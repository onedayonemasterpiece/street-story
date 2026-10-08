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
        for key in ('height', 'building:levels', 'roof:levels'):
            if tags.get(key) is not None:
                kind[key] = _short(tags[key], 30)
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


def compact_scene_manifest(manifest):
    """Neutral map labels retain all exact IDs and measured contour summaries."""
    from .identity_scene import lean_scene_manifest
    return lean_scene_manifest(manifest)


def wikipedia_metadata_context(pages, *, intro_limit=70):
    """All metadata choices, no raw article bodies or downloaded images."""
    return {'columns': ['page_id', 'title', 'wikidata_id', 'latitude_longitude', 'intro',
        'has_reference_image', 'mapped_osm_ids', 'discovery'],
        'rows': [[str(page['pageid']), _short(page.get('title'), 150),
            (page.get('pageprops') or {}).get('wikibase_item') or page.get('wikidata_id'),
            [_number(page.get('lat'), 6), _number(page.get('lon'), 6)],
            _short(page.get('extract'), intro_limit), bool(page.get('image_url') or page.get('thumbnail_url')),
            [item['candidate_id'] for item in page.get('mapped_wikipedia_sources') or []],
            page.get('discovery', 'wikipedia_nearby_metadata')]
            for page in pages if isinstance(page, dict) and page.get('pageid')],
        'policy': 'Coordinates and links belong to the article or exact mapped object only. '
            'A nearby park, institution, complex or district article is not automatically the SOURCE building. '
            'Choose up to three physically applicable pages; image URLs will be fetched only after selection.'}


def compact_planner_packet(packet):
    """Lossless shared literals and exact neutral map-label joins, never ranking."""
    from collections import Counter
    original_packet = packet
    table = (original_packet.get('map_scene') or {}).get('objects') or {}
    columns = table.get('columns') or []
    labels = {}
    if 'label' in columns and 'candidate_id' in columns:
        li, ci = columns.index('label'), columns.index('candidate_id')
        labels = {row[ci]: row[li] for row in table.get('rows') or [] if row[ci]}
    grid_origin = [round(float(packet.get(key) or 0) * 1_000_000) for key in ('capture_lat', 'capture_lon')]
    def tables(value):
        if isinstance(value, list):
            return [tables(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: tables(item) for key, item in value.items()}
        rows, columns = result.get('rows'), result.get('columns')
        if isinstance(rows, list) and rows and isinstance(columns, list) and all(len(row) == len(columns) for row in rows):
            label_columns = [index for index, key in enumerate(columns)
                if key in {'candidate_id', 'subject_id', 'group_key', 'mapped_entry_id'}
                and 'label' not in columns and any(isinstance(row[index], str) and row[index] in labels for row in rows)]
            for row in rows:
                for index in label_columns:
                    value = row[index]
                    if isinstance(value, str) and value in labels:
                        row[index] = labels[value]
                    elif value is not None and not isinstance(value, str):
                        row[index] = {'literal_candidate_value': value}
            if label_columns:
                result['candidate_label_columns'] = label_columns
            coordinate_columns = [index for index, key in enumerate(columns) if key == 'latitude_longitude']
            if coordinate_columns:
                for row in rows:
                    for index in coordinate_columns:
                        point = row[index]
                        if isinstance(point, list) and len(point) == 2 and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in point):
                            row[index] = [round(value*1_000_000) - origin for value, origin in zip(point, grid_origin)]
                result['coordinate_offset_columns'] = coordinate_columns
            defaults = {str(index): rows[0][index] for index in range(len(columns))
                if all(row[index] == rows[0][index] for row in rows)}
            if defaults:
                varying = [index for index in range(len(columns)) if str(index) not in defaults]
                projected = []
                for row in rows:
                    compact = [row[index] for index in varying]
                    while compact and compact[-1] is None:
                        compact.pop()
                    projected.append(compact)
                result.update(rows=projected, variable_columns=varying, column_defaults=defaults)
        return result
    packet = tables(packet)
    counts = Counter()
    composites = Counter()
    composite_values = {}
    def signature(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    def count(value):
        if isinstance(value, str):
            counts[value] += 1
        elif isinstance(value, list):
            if value:
                key = signature(value)
                composites[key] += 1
                composite_values[key] = value
            for item in value:
                count(item)
        elif isinstance(value, dict):
            if value:
                key = signature(value)
                composites[key] += 1
                composite_values[key] = value
            for item in value.values():
                count(item)
    count(packet)
    literals = [value for value, amount in counts.items() if value not in labels
        and amount >= 2 and (len(value.encode()) + 2 - 5) * amount > len(value.encode()) + 3]
    indexes = {value: index for index, value in enumerate(literals)}
    composite_indexes = {}
    for key, amount in composites.items():
        if amount >= 2 and (len(key.encode()) - 5) * amount > len(key.encode()) + 1:
            composite_indexes[key] = len(literals)
            literals.append(composite_values[key])
    def encode(value, *, preserve_id=False):
        if isinstance(value, str):
            if value in labels and not preserve_id:
                return '@' + str(labels[value])
            if value in indexes:
                return '$' + str(indexes[value])
            return '=' + value if value.startswith(('@', '$', '=')) else value
        if isinstance(value, list):
            index = composite_indexes.get(signature(value))
            if index is not None:
                return '$' + str(index)
            return [encode(item, preserve_id=preserve_id) for item in value]
        if isinstance(value, dict):
            index = composite_indexes.get(signature(value))
            if index is not None:
                return '$' + str(index)
            return {key: encode(item, preserve_id=preserve_id or key == 'objects') for key, item in value.items()}
        return value
    encoded = encode(packet)
    used = set()
    def references(value):
        if isinstance(value, str) and value.startswith('$') and value[1:].isdigit():
            used.add(int(value[1:]))
        elif isinstance(value, list):
            for item in value:
                references(item)
        elif isinstance(value, dict):
            for item in value.values():
                references(item)
    references(encoded)
    remap = {old: new for new, old in enumerate(sorted(used))}
    def renumber(value):
        if isinstance(value, str) and value.startswith('$') and value[1:].isdigit():
            return '$' + str(remap[int(value[1:])])
        if isinstance(value, list):
            return [renumber(item) for item in value]
        if isinstance(value, dict):
            return {key: renumber(item) for key, item in value.items()}
        return value
    return {'encoding': 'lossless-literals-and-map-labels-v1',
        'join_policy': 'A string @N means the exact candidate_id at map_scene.objects label N; '
            'a string $N means literals[N]. A string starting = escapes its remaining literal text. Decode these references in every table/value. '
            'Table rows follow variable_columns indices when present; other columns use column_defaults. '
            'Missing trailing row cells mean null. These are exact lossless joins/defaults, not omitted objects. '
            'In candidate_label_columns, integer values mean exact map labels; literal_candidate_value wraps original nonstring values. '
            'In coordinate_offset_columns, numeric pairs are offsets from coordinate_grid_origin_microdegrees '
            'in millionths of a degree; add the origin and divide by 1000000 to recover rounded observed lat/lon. '
            'Output actual exact IDs and literal strings required by the response schema, never these references. '
            'Missing/null values remain unknown; references do not bind SOURCE or rank hypotheses.',
        'coordinate_grid_origin_microdegrees': grid_origin,
        'literals': [literals[index] for index in sorted(used)], **renumber(encoded)}


def expand_planner_packet(packet):
    """Offline round-trip check of the packet supplied to the model."""
    import copy
    literals = packet.get('literals') or []
    grid_origin = packet.get('coordinate_grid_origin_microdegrees') or [0, 0]
    labels = {}
    def tables(value):
        if isinstance(value, list):
            return [tables(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: tables(item) for key, item in value.items()}
        if 'variable_columns' in result:
            columns = result['columns']
            defaults = result.pop('column_defaults')
            varying = result.pop('variable_columns')
            rows = []
            for compact in result['rows']:
                row = [defaults.get(str(index)) for index in range(len(columns))]
                for offset, index in enumerate(varying):
                    row[index] = compact[offset] if offset < len(compact) else None
                rows.append(row)
            result['rows'] = rows
        for index in result.pop('candidate_label_columns', []):
            for row in result['rows']:
                value = row[index]
                if isinstance(value, int) and not isinstance(value, bool):
                    row[index] = labels[value]
                elif isinstance(value, dict) and 'literal_candidate_value' in value:
                    row[index] = value['literal_candidate_value']
        if 'coordinate_offset_columns' in result:
            coordinate_columns = result.pop('coordinate_offset_columns')
            for row in result['rows']:
                for index in coordinate_columns:
                    point = row[index]
                    if isinstance(point, list) and len(point) == 2 and all(isinstance(value, int) and not isinstance(value, bool) for value in point):
                        row[index] = [(value + origin)/1_000_000 for value, origin in zip(point, grid_origin)]
        return result
    def decode(value):
        if isinstance(value, str):
            if value.startswith('='):
                return value[1:]
            if value.startswith('$') and value[1:].isdigit():
                return copy.deepcopy(literals[int(value[1:])])
            if value.startswith('@') and value[1:].isdigit():
                return labels[int(value[1:])]
            return value
        if isinstance(value, dict):
            return {key: decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        return value
    table = tables(decode((packet.get('map_scene') or {}).get('objects') or {}))
    columns = table.get('columns') or []
    if 'label' in columns and 'candidate_id' in columns:
        labels = {row[columns.index('label')]: row[columns.index('candidate_id')] for row in table.get('rows') or []}
    return tables(decode({key: value for key, value in packet.items()
        if key not in {'encoding', 'join_policy', 'literals', 'coordinate_grid_origin_microdegrees'}}))


def model_identity_context(story, candidates=(), *, include_observed=True, scene_available=False):
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
    if scene_available:
        # Measured contour geometry is supplied once by MAP's labeled manifest;
        # literal address records/memberships remain in the separate tables.
        table = packet['observed_physical_candidates']
        columns = ['candidate_id', 'name', 'address_city_street_house_number', 'latitude_longitude', 'object_tags', 'identity_eligible']
        indexes = [table['columns'].index(key) for key in columns]
        packet['observed_physical_candidates'] = {**table, 'columns': columns,
            'rows': [[row[index] for index in indexes] for row in table['rows']]}
        name_index, tags_index = columns.index('name'), columns.index('object_tags')
        for row, entry in zip(packet['observed_physical_candidates']['rows'], by_id.values()):
            tags = (entry.get('map_object') or {}).get('tags') or {}
            # Display names generated from addresses, locality or unnamed OSM
            # objects duplicate other tables and are not observed proper names.
            row[name_index] = _short(tags.get('name') or (
                entry.get('name') if entry.get('type') == 'wikipedia' else ''), 100)
            if row[tags_index]:
                row[tags_index] = {key: value for key, value in row[tags_index].items()
                    if key not in {'scope', 'provenance'}} or None
    # Non-scene selectors have their own small packet envelope. Joint packets
    # compact this full leaf losslessly before the actual provider admission.
    if not scene_available and len(json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode()) > 65536:
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


def identity_transport_schema(schema):
    """Send bounded ID strings; validate exact observed membership on the host.

    Repeating the full OSM dictionary in several schema enums can exceed the
    provider's structured-output complexity limit. The complete dictionary and
    strict validation schema remain unchanged in the operation context.
    """
    import copy
    result = copy.deepcopy(schema)
    properties = result['properties']
    if 'observed_candidate_ids' in properties:
        properties['observed_candidate_ids']['items'] = {'type': 'string', 'maxLength': 100}
    if 'spatial_hypotheses' in properties:
        properties['spatial_hypotheses']['items']['properties']['candidate_id'] = {
            'type': 'string', 'maxLength': 100}
    if 'first_wave_hypotheses' in properties:
        properties['first_wave_hypotheses']['items']['properties']['subject_id'] = {
            'type': 'string', 'maxLength': 100}
    return result


def geometry_decision_schema(candidate_ids):
    """Evidence from the existing joint call, rather than a second judge."""
    text = {'type': 'string', 'maxLength': 300}
    # The exact ID dictionary is already in the map packet. The common proof
    # validator dereferences these IDs against all received observed features.
    candidate_id = {'type': 'string', 'maxLength': 100}
    texts = {'type': 'array', 'maxItems': 8, 'items': text}
    feature = {'type': 'object', 'properties': {
        'candidate_id': candidate_id,
        'kind': {'type': 'string', 'enum': ['contour', 'road_axis', 'segment', 'point', 'attribute']},
        'ring_index': {'type': 'integer', 'minimum': 0},
        'line_index': {'type': 'integer', 'minimum': 0},
        'segment_index': {'type': 'integer', 'minimum': 0},
        'key': {'type': 'string', 'maxLength': 60}},
        'required': ['candidate_id', 'kind'], 'additionalProperties': False}
    relation = {'type': 'object', 'properties': {
        'source_observation': text, 'map_features': {'type': 'array', 'maxItems': 6, 'items': feature},
        'correspondence': text},
        'required': ['source_observation', 'map_features', 'correspondence'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'decision': {'type': 'string', 'enum': ['accepted_geometry', 'uncertain']},
        'candidate_id': candidate_id,
        'candidate_label': {'type': 'integer', 'minimum': 1},
        'scope': text,
        'decisive_relations': {'type': 'array', 'maxItems': 8, 'items': relation},
        'rejected_alternatives': {'type': 'array', 'maxItems': 8, 'items': {
            'type': 'object', 'properties': {
                'candidate_id': candidate_id, 'reason': text},
            'required': ['candidate_id', 'reason'], 'additionalProperties': False}},
        'assumptions': texts,
        'bounded_coverage': {'type': 'object', 'properties': {
            'scope': text, 'limitations': texts, 'material_alternatives_resolved': {'type': 'boolean'}},
            'required': ['scope', 'limitations', 'material_alternatives_resolved'], 'additionalProperties': False},
        'camera_pose': {'type': 'object', 'properties': {
            'position_basis': text, 'yaw_basis': text, 'sensitivity': text},
            'required': ['position_basis', 'yaw_basis', 'sensitivity'], 'additionalProperties': False},
        'next_action': {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['none', 'map_detail', 'address_text', 'ready_article',
                'reference_image', 'owner_context']},
            'reason': text, 'target_candidate_ids': {'type': 'array', 'maxItems': 3,
                'uniqueItems': True, 'items': candidate_id}},
            'required': ['kind', 'reason', 'target_candidate_ids'], 'additionalProperties': False}},
        'required': ['decision', 'candidate_id', 'scope', 'decisive_relations', 'rejected_alternatives',
            'assumptions', 'bounded_coverage', 'camera_pose'], 'additionalProperties': False}


def first_wave_schema(catalog):
    # Coverage is checked after grounded Wiki/spatial choices are known.
    return {'type': 'array', 'minItems': 0, 'maxItems': 3,
        'items': {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['address', 'observed_named', 'unmapped_named', 'appearance']},
            'subject_id': {'type': 'string', 'enum': list(dict.fromkeys([
                *(option['subject_id'] for option in catalog['options'].values()), '']))},
            'query': {'type': 'string', 'maxLength': 240},
            'reason': {'type': 'string', 'maxLength': 240}},
            'required': ['kind', 'subject_id', 'query', 'reason'], 'additionalProperties': False}}


def grounded_wave_catalog(catalog, payload, *, ready_wikipedia=False, joint_geometry=False):
    if ready_wikipedia:
        return {**catalog, 'required_grounded_count': 0}
    decision = payload.get('accepted_geometry') or {}
    action = decision.get('next_action') or {}
    hypotheses = payload.get('first_wave_hypotheses') or []
    if (joint_geometry and decision.get('decision') == 'uncertain'
            and action.get('kind') != 'none' and str(action.get('reason') or '').strip()
            and len(hypotheses) == 1):
        hypothesis = hypotheses[0]
        option = catalog['options'].get((hypothesis.get('kind'), hypothesis.get('subject_id')))
        if option and option['group_key'] in (action.get('target_candidate_ids') or []):
            return {**catalog, 'required_grounded_count': min(1, catalog['required_grounded_count'])}
    spatial = payload.get('spatial_hypotheses') or []
    supported = [item for item in spatial if item.get('support_status') == 'spatially_supported'
        and any(isinstance(value, str) and value.strip() for value in item.get('basis') or [])]
    alternatives = [item for item in spatial if item.get('support_status') == 'plausible']
    if len(supported) == 1 and not alternatives:
        selected = supported[0]['candidate_id']
        choices = [catalog['options'].get((item.get('kind'), item.get('subject_id')))
            for item in payload.get('first_wave_hypotheses') or []]
        if any(option and option['group_key'] == selected for option in choices):
            return {**catalog, 'required_grounded_count': min(1, catalog['required_grounded_count'])}
    return catalog


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
