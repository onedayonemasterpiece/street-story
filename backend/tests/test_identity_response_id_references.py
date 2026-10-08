"""Transport joins do not change semantic choices or relax physical binding."""
import copy

from jsonschema import Draft202012Validator

from street_story.identity_source_selection import compact_planner_packet, resolve_identity_response_ids


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


def test_many_joins_keep_total_count_and_explicit_bounded_receipt():
    raw = {'candidate_ids': ['@338'] * 40}
    result, receipt = resolve_identity_response_ids(raw, packet())
    assert result['candidate_ids'] == ['osm:relation:2'] * 40
    assert receipt['resolved_count'] == 40 and len(receipt['resolutions']) == 32
    assert receipt['resolutions_truncated']
