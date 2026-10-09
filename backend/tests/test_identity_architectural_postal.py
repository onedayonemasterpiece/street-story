"""Actual catalogue postal group membership never confers physical identity."""
from street_story.identity_architectural_postal import complex_postal_member
from street_story.identity_architectural_comparison import (
    publisher_address_relation, verified_publisher_physical_scope)


def test_publisher_enumerated_historical_complex_matches_individual_observed_members():
    publisher='Калининградская область, г. Калининград, ул. Комсомольская, 80, 82, 82а, 84, 86, 88, 90'
    street='Комсомольская улица'
    for house in ('80','82','82А','82A','82-82А','84','88','90'):
        assert complex_postal_member(publisher,street,house)
    for house in ('79','83','90А','92','82Б'):
        assert not complex_postal_member(publisher,street,house)


def test_bounded_publisher_postal_range_only_supports_corpus_membership():
    publisher='Калининградская область, г. Калининград, ул. Фрунзе, 51-57'
    assert complex_postal_member(publisher,'улица Фрунзе','53-57')
    assert complex_postal_member(publisher,'улица Фрунзе','53')
    assert not complex_postal_member(publisher,'улица Фрунзе','59')
    assert not complex_postal_member(publisher,'улица Фрунзе','6А')


def test_single_neighbor_address_never_matches_adjacent_corpus():
    assert not complex_postal_member('Советск, ул. Капитана Гастелло, 21',
        'улица Капитана Гастелло','22')
    assert not complex_postal_member('Калининград, ул. Барнаульская, 6',
        'Барнаульская улица','6А')
    assert not complex_postal_member('Город, ул. Барнаульская, 6А',
        'Барнаульская улица','6')
    assert not complex_postal_member('Город, ул. Барнаульская, 6',
        'Барнаульская улица','6')


def test_publisher_group_from_wrong_street_or_prose_date_never_links():
    assert not complex_postal_member('Город, улица Другая, 80,82,84',
        'Комсомольская улица','82')
    assert not complex_postal_member('Построено в 1928 году у улицы Комсомольской',
        'Комсомольская улица','82')
    assert not complex_postal_member('Город, Комсомольская улица, 51-999',
        'Комсомольская улица','53')


def test_multiple_corpora_in_received_publisher_group_are_visible_not_merged():
    url='https://www.prussia39.ru/sight/index.php?sid=2458'
    article={'article_id':'prussia39:sid:2458','url':url,
        'address':'Калининград, ул. Комсомольская, 80, 82, 82а, 84, 86, 88, 90',
        'address_provenance':'publisher_article_metadata_table',
        'raw_body_sha256_verified':True,'input_kind':'acquired_article_text'}
    def body(cid,number):
        return {'candidate_id':cid,'identity_eligible':True,
            'map_object':{'tags':{'building':'yes'}},
            'map_address':{'street':'Комсомольская улица','house_number':number}}
    buildings=[body('osm:way:1','82-82А'),body('osm:way:2','88'),
        body('osm:way:3','90')]
    metadata=[{'candidate_id':b['candidate_id'],'literal_address_entries':[{
        'entry_id':b['candidate_id'],'address':b['map_address'],
        'provenance':'osm_physical_own_address'}]} for b in buildings]
    joined=publisher_address_relation([article],metadata)[0]['physical_links']
    assert {item['candidate_id'] for item in joined if
        item['publisher_complex_postal_membership_entry_ids']} == {
        'osm:way:1','osm:way:2','osm:way:3'}
    assert all(not row['physical_identity_inferred'] for row in joined)
    decision={'decision':'accepted_architectural_text','candidate_id':'osm:way:1',
        'article_bindings':[{'article_id':article['article_id'],
            'candidate_id':'osm:way:1',
            'scope':'Received publisher residential complex, corpus unresolved',
            'binding_basis':'OSM postal member in complex',
            'physical_binding_resolved':True}]}
    actual=verified_publisher_physical_scope(
        {'_identity_observed_candidates':buildings},buildings,
        {'articles':[article]},decision)
    assert actual['supported'] is False
    assert actual['retrieval_link_valid'] is True
    assert actual['reason']=='publisher_complex_postal_membership_requires_corpus_resolution'
    assert set(actual['publisher_complex_possible_physical_corpora']) == {
        'osm:way:1','osm:way:2','osm:way:3'}
