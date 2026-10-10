"""Transport joins do not change semantic choices or relax physical binding."""
import copy

from jsonschema import Draft202012Validator

from street_story.identity_source_selection import compact_planner_packet, resolve_identity_response_ids, identity_transport_schema


def packet():
    return compact_planner_packet({'map_scene': {'objects': {
        'columns': ['label', 'candidate_id'], 'rows': [[338, 'osm:relation:2'], [339, 'osm:way:3']]}},
        'article_ids': ['wiki:123456789', 'wiki:123456789', 'wiki:123456789']})


def test_exact_map_and_literal_ids_resolve_without_touching_prose_or_original():
    context = packet()
    wiki = '$' + str(context['literals'].index('wiki:123456789'))
    raw = {'subject_article_bindings': [{'candidate_id': '@338', 'article_id': wiki,
        'scope': '@338', 'binding_basis': 'Price $0 and facade @338 remain exact prose.'}],
        'first_wave_hypotheses': [{'subject_id': '@338', 'query': '@338'}],
        'observed_candidate_ids': ['@338', '@339'], 'quote': '@338', 'measurement': '$0'}
    original = copy.deepcopy(raw)
    result, receipt = resolve_identity_response_ids(raw, context)
    assert raw == original
    assert result['subject_article_bindings'][0] == {**raw['subject_article_bindings'][0],
        'candidate_id': 'osm:relation:2', 'article_id': 'wiki:123456789'}
    assert result['first_wave_hypotheses'][0] == {'subject_id': 'osm:relation:2', 'query': '@338'}
    assert result['observed_candidate_ids'] == ['osm:relation:2', 'osm:way:3']
    assert result['quote'] == raw['quote'] and result['measurement'] == raw['measurement']
    assert receipt['resolved_count'] == 5 and not receipt['resolutions_truncated']
    assert receipt['original_decoded_sha256'] != receipt['resolved_decoded_sha256']


def test_unknown_or_unreceived_pointer_stays_rejected_by_unchanged_host_schema():
    context = packet()
    schema = {'type': 'object', 'properties': {'candidate_id': {'enum': ['osm:relation:2']}}}
    for reference in ('@339', '@999', '$999', '$' + '9' * 10000, '@-338', '@٣٣٨', '=@338', 338):
        result, _ = resolve_identity_response_ids({'candidate_id': reference}, context)
        assert not Draft202012Validator(schema).is_valid(result)
    result, _ = resolve_identity_response_ids({'candidate_id': '@338'}, context)
    assert Draft202012Validator(schema).is_valid(result)


def test_plain_packet_or_duplicate_map_labels_never_invents_a_join():
    raw = {'candidate_id': '@338'}
    assert resolve_identity_response_ids(raw, {}) == (raw, None)
    context = compact_planner_packet({'map_scene': {'objects': {
        'columns': ['label', 'candidate_id'], 'rows': [[338, 'osm:way:2'], [338, 'osm:way:3']]}}})
    assert resolve_identity_response_ids(raw, context) == (raw, None)


def test_plain_received_map_has_same_explicit_namespace_as_compact_map_without_suffix_guessing():
    context = {'map_scene': {'objects': {'columns': ['label', 'candidate_id'],
        'rows': [[338, 'osm:relation:2'], [339, 'osm:way:3']]}}, 'literals': ['osm:way:3']}
    raw = {'candidate_id': '@338', 'address_entry_id': '@339',
        'target_candidate_ids': ['@339'], 'scope': '@338', 'article_id': '$0'}
    resolved, receipt = resolve_identity_response_ids(raw, context)
    assert resolved == {**raw, 'candidate_id': 'osm:relation:2',
        'address_entry_id': 'osm:way:3', 'target_candidate_ids': ['osm:way:3']}
    assert receipt['resolved_count'] == 3
    for value in ('osm:way:338', 'osm:relation:338', '338', 338, '@999'):
        assert resolve_identity_response_ids({'candidate_id': value, 'candidate_label': 338}, context) == (
            {'candidate_id': value, 'candidate_label': 338}, None)


def test_every_pointer_transport_field_describes_same_namespace_and_strict_host_stays_unchanged():
    schema = {'type': 'object', 'properties': {
        'observed_candidate_ids': {'type': 'array', 'items': {'type': 'string', 'enum': ['osm:relation:2']}},
        'first_wave_hypotheses': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'subject_id': {'type': 'string', 'enum': ['osm:relation:2', '']}}}},
        'spatial_hypotheses': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'candidate_id': {'type': 'string', 'enum': ['osm:relation:2']}}}},
        'regional_article_selections': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'candidate_id': {'type': 'string', 'enum': ['osm:relation:2']},
            'article_id': {'type': 'string', 'enum': ['prussia39:sid:7']}}}},
        'regional_lookup': {'type': 'object', 'properties': {
            'candidate_ids': {'type': 'array', 'items': {'type': 'string', 'enum': ['osm:relation:2']}},
            'address_entry_id': {'type': 'string', 'enum': ['osm:way:3']}}},
        'accepted_geometry': {'type': 'object', 'properties': {
            'candidate_id': {'type': 'string'}, 'candidate_label': {'type': 'integer'},
            'map_features': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'candidate_id': {'type': 'string'}}}},
            'next_action': {'type': 'object', 'properties': {
                'target_candidate_ids': {'type': 'array', 'items': {'type': 'string'}}}}}}}}
    original = copy.deepcopy(schema)
    transport = identity_transport_schema(schema, map_label_references=True)
    descriptions = []
    def pointers(node):
        for key, value in (node.get('properties') or {}).items():
            if key in {'candidate_id', 'subject_id', 'address_entry_id'}:
                descriptions.append(value['description'])
            elif key in {'observed_candidate_ids', 'candidate_ids', 'target_candidate_ids'}:
                descriptions.append(value['items']['description'])
            pointers(value)
        if isinstance(node.get('items'), dict):
            pointers(node['items'])
    pointers(transport)
    assert len(descriptions) == 9 and all('Exact received ID or @N' in value for value in descriptions)
    assert schema == original
    assert transport['properties']['regional_article_selections']['items']['properties']['article_id'] == {
        'type': 'string', 'enum': ['prussia39:sid:7']}
    assert 'description' not in transport['properties']['accepted_geometry']['properties']['candidate_label']
    raw = {'first_wave_hypotheses': [{'subject_id': '@338'}],
        'spatial_hypotheses': [{'candidate_id': '@338'}],
        'regional_lookup': {'candidate_ids': ['@338'], 'address_entry_id': '@339'},
        'regional_article_selections': [{'candidate_id': '@338', 'article_id': 'prussia39:sid:7'}]}
    assert Draft202012Validator(transport).is_valid(raw)
    assert not Draft202012Validator(schema).is_valid(raw)
    resolved, _ = resolve_identity_response_ids(raw, packet())
    assert Draft202012Validator(schema).is_valid(resolved)


def test_many_joins_keep_total_count_and_explicit_bounded_receipt():
    raw = {'candidate_ids': ['@338'] * 40}
    result, receipt = resolve_identity_response_ids(raw, packet())
    assert result['candidate_ids'] == ['osm:relation:2'] * 40
    assert receipt['resolved_count'] == 40 and len(receipt['resolutions']) == 32
    assert receipt['resolutions_truncated']
