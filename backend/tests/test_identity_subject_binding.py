from __future__ import annotations

import pytest

from street_story.identity_lifecycle import visual_match
from street_story.identity_subject_binding import bind_reference_subject, subject_aliases
from street_story.poi_memory import ensure_poi_identity
from test_identity_lifecycle import make_service


def verdict(candidate_id='wiki:1', **values):
    return {'status': 'match', 'candidate_id': candidate_id, 'confidence': .97,
            'observations': ['Same distinctive tower openings and brick arch.'],
            'alternative_candidate_ids': [], '_references_sent': [candidate_id],
            '_reference_ids_sent': ['ref_article'], **values}


def subject(candidate_id='wiki:1', **values):
    return {'candidate_id': candidate_id, 'name': 'Physical subject',
            'url': f'https://ru.wikipedia.org/wiki/{candidate_id}', **values}


def article():
    return {'candidate_id': 'web:article', 'name': 'A misleading overview title',
            'url': 'https://example.org/article', 'discovery': 'web_article_media',
            'reference_image_urls': ['https://example.org/rear.jpg'], 'reference_id': 'ref_article'}


def image_evidence(**values):
    return {'candidate_id': 'web:article', 'article_url': 'https://example.org/article',
            'source_url': 'https://example.org/rear.jpg', 'reference_id': 'ref_article', **values}


def test_genuine_alternative_outside_sent_reference_batch_blocks_match():
    selected, competitor = subject(), subject('wiki:2', name='Other physical subject')
    raw = verdict(alternative_candidate_ids=['wiki:2'])
    assert not visual_match(raw, [selected], [selected, competitor])


def test_unknown_alternative_is_not_silently_discarded():
    assert not visual_match(verdict(alternative_candidate_ids=['outside-shortlist']), [subject()])


def test_same_name_does_not_merge_competing_objects():
    first, second = subject(), subject('wiki:2')
    assert not visual_match(verdict(alternative_candidate_ids=['wiki:2']), [first], [first, second])


@pytest.mark.parametrize('shared_key,shared_value', [('wikidata', 'Q123'), ('osm_id', 'osm:way:123')])
def test_exact_entity_alias_does_not_create_false_disagreement(shared_key, shared_value):
    wiki = subject(**{shared_key: shared_value})
    osm = subject('osm:way:123', name='An alternate name', **{shared_key: shared_value})
    assert visual_match(verdict(alternative_candidate_ids=['osm:way:123']), [wiki], [wiki, osm])


def test_explicit_wikipedia_link_and_verified_cluster_aliases():
    wiki = subject()
    osm = subject('osm:way:123', wikipedia_url=wiki['url'])
    assert visual_match(verdict(alternative_candidate_ids=['osm:way:123']), [wiki], [wiki, osm])
    cluster = subject(discovery='wikimedia_entity_cluster', alias_candidate_ids=['commons:123'])
    assert visual_match(verdict(alternative_candidate_ids=['commons:123']), [cluster])


def test_unproved_alias_list_is_not_semantic_identity():
    first = subject(alias_candidate_ids=['wiki:2'])
    assert not visual_match(verdict(alternative_candidate_ids=['wiki:2']), [first], [first, subject('wiki:2')])


def test_registry_aliases_use_candidate_ids_not_names_or_web_addresses():
    first, second = subject(), subject('osm:way:123')
    catalog = [first, second, article()]
    groups = subject_aliases(catalog, poi_aliases={'wiki:1': 'poi_existing', 'osm:way:123': 'poi_existing',
                                                'web:article': 'poi_existing'})
    assert groups['wiki:1'] == {'wiki:1', 'osm:way:123'}
    assert 'web:article' not in groups
    assert visual_match(verdict(alternative_candidate_ids=['osm:way:123']), [first], [first, second],
                        poi_aliases={'wiki:1': 'poi_existing', 'osm:way:123': 'poi_existing'})


def test_raw_article_match_cannot_become_canonical_poi():
    assert not visual_match(verdict('web:article'), [article()])
    unresolved = bind_reference_subject(verdict('web:article'), [article()], [subject()], [image_evidence()])
    assert unresolved['status'] == 'unresolved'
    assert unresolved['result']['status'] == 'uncertain'


def test_explicit_article_subject_resolution_preserves_sent_reference_and_provenance():
    candidate = subject()
    raw = verdict('web:article', reference_subject_candidate_id='wiki:1')
    resolved = bind_reference_subject(raw, [article()], [candidate], [image_evidence()])
    assert resolved['status'] == 'bound'
    assert resolved['candidate'] == candidate
    assert resolved['result']['candidate_id'] == 'wiki:1'
    assert resolved['result']['_references_sent'] == ['web:article']
    assert resolved['reference_evidence'][0]['candidate_id'] == 'web:article'
    assert resolved['reference_evidence'][0]['subject_candidate_id'] == 'wiki:1'
    assert visual_match(resolved['result'], [article()], [candidate])


@pytest.mark.parametrize('tag', ['amenity', 'shop', 'office'])
def test_whole_building_photo_cannot_be_bound_to_a_mapped_occupant(tag):
    candidate = subject('osm:node:1', map_object={'provenance': 'osm.tags', 'tags': {tag: 'example'}})
    raw = verdict('web:article', reference_subject_candidate_id=candidate['candidate_id'], source_subject_scope='building')
    resolved = bind_reference_subject(raw, [article()], [candidate], [image_evidence()])
    assert resolved['reason'] == 'mapped_occupant_is_not_building_subject'
    assert resolved['result']['status'] == 'uncertain'
    direct = verdict(candidate['candidate_id'], source_subject_scope='building')
    assert bind_reference_subject(direct, [candidate], [candidate], [])['reason'] == 'mapped_occupant_is_not_building_subject'
    # The organization remains a valid hypothesis when it is the actual
    # subject, and a separately mapped building keeps its own identity.
    raw['source_subject_scope'] = 'occupant'
    assert bind_reference_subject(raw, [article()], [candidate], [image_evidence()])['status'] == 'bound'
    raw['source_subject_scope'] = 'building'
    candidate['map_object']['tags']['building'] = 'yes'
    assert bind_reference_subject(raw, [article()], [candidate], [image_evidence()])['status'] == 'bound'


@pytest.mark.parametrize('tags', [{'landuse': 'residential', 'place': 'neighbourhood'},
                                {'landuse': 'industrial'}, {'place': 'suburb'}])
def test_render_can_identify_building_without_substituting_the_whole_mapped_area(tags):
    area = subject('osm:way:area', map_object={'provenance': 'osm.tags', 'tags': dict(tags)})
    building = subject('osm:relation:house', map_object={'provenance': 'nominatim.reverse',
                       'category': 'building', 'type': 'apartments'})
    raw = verdict('web:article', reference_subject_candidate_id=area['candidate_id'],
                  source_subject_scope='building', search_feedback={'reference_kind': 'diagram'})
    catalog = [area, building]
    resolved = bind_reference_subject(raw, [article()], catalog, [image_evidence()])
    assert resolved['reason'] == 'mapped_area_is_not_building_subject'
    assert not visual_match(resolved['result'], [article()], catalog)
    raw['reference_subject_candidate_id'] = building['candidate_id']
    resolved = bind_reference_subject(raw, [article()], catalog, [image_evidence()])
    assert resolved['status'] == 'bound'
    assert visual_match(resolved['result'], [article()], catalog)
    # A territory remains usable when SOURCE actually depicts that territory.
    raw.update(reference_subject_candidate_id=area['candidate_id'], source_subject_scope='other_physical_object')
    assert bind_reference_subject(raw, [article()], catalog, [image_evidence()])['status'] == 'bound'


@pytest.mark.parametrize('candidate_id', ['not-in-shortlist', 'web:article', 'wiki:container'])
def test_article_binding_requires_eligible_existing_physical_subject(candidate_id):
    raw = verdict('web:article', reference_subject_candidate_id=candidate_id)
    resolved = bind_reference_subject(raw, [article()], [subject(), article(), subject('wiki:container', identity_eligible=False)],
                                      [image_evidence()])
    assert resolved['status'] == 'unresolved'
    assert resolved['result']['status'] == 'uncertain'


@pytest.mark.parametrize('alteration', [{'candidate_id': 'web:other'}, {'article_url': 'https://example.org/other'},
                                      {'source_url': 'https://example.org/banner.jpg'}, {'reference_id': 'not_sent'}])
def test_article_binding_requires_exact_actual_sent_image_receipt(alteration):
    resolved = bind_reference_subject(verdict('web:article', reference_subject_candidate_id='wiki:1'),
                                      [article()], [subject()], [image_evidence(**alteration)])
    assert resolved['status'] == 'unresolved'


def test_binding_cannot_hide_competitor_or_lower_acceptance_threshold():
    raw = verdict('web:article', reference_subject_candidate_id='wiki:1', alternative_candidate_ids=['wiki:2'])
    resolved = bind_reference_subject(raw, [article()], [subject(), subject('wiki:2')], [image_evidence()])
    assert not visual_match(resolved['result'], [article()], [subject(), subject('wiki:2')])
    raw = verdict('web:article', reference_subject_candidate_id='wiki:1', confidence=.89)
    resolved = bind_reference_subject(raw, [article()], [subject()], [image_evidence()])
    assert not visual_match(resolved['result'], [article()], [subject()])


def test_article_binding_preserves_proven_alias_not_false_disagreement():
    wiki = subject(wikidata='Q123')
    osm = subject('osm:way:123', wikidata='Q123')
    resolved = bind_reference_subject(verdict('web:article', reference_subject_candidate_id='wiki:1',
                                             alternative_candidate_ids=['osm:way:123']),
                                      [article()], [wiki, osm], [image_evidence()])
    assert visual_match(resolved['result'], [article()], [wiki, osm])


def test_fake_internal_binding_is_removed_and_unsent_reference_not_accepted():
    raw = verdict('web:article', reference_subject_candidate_id='wiki:1', _references_sent=[],
                  _reference_subject_binding={'proof': 'model_reference_subject_resolution'})
    resolved = bind_reference_subject(raw, [article()], [subject()], [image_evidence()])
    assert resolved['reason'] == 'reference_not_sent'
    assert '_reference_subject_binding' not in resolved['result']


def test_existing_non_article_acceptance_is_backward_compatible():
    raw = verdict()
    resolved = bind_reference_subject(raw, [subject()], [subject()], [])
    assert resolved['status'] == 'not_required'
    assert resolved['result'] == raw
    assert visual_match(raw, [subject()])


@pytest.mark.parametrize('values', [
    {'observations': 'A title is not a structured visual observation'},
    {'observations': ['   ']},
    {'observations': [None]},
    {'_references_sent': 'wiki:1'},
    {'_references_sent': [None, 'wiki:1']},
    {'alternative_candidate_ids': 'wiki:2'},
    {'alternative_candidate_ids': [None]},
    {'confidence': True},
    {'candidate_id': {'invented': 'wiki:1'}},
])
def test_malformed_visual_verdict_cannot_confirm_identity(values):
    assert not visual_match(verdict(**values), [subject()])


@pytest.mark.asyncio
async def test_full_shortlist_alias_survives_bounded_reference_batches(tmp_path):
    from street_story.identity_visual import identify_nearest
    from test_identity_lifecycle import create
    service, _ = make_service(tmp_path)
    story = create(service)
    hypotheses = [subject(f'wiki:{i}', distance_m=i, wikidata=f'Q{i}') for i in range(1, 18)]
    hypotheses[-1]['wikidata'] = 'Q1'
    calls = []

    async def compare(snapshot, _transcript, batch, reference_limit):
        calls.append(([item['candidate_id'] for item in batch], reference_limit))
        assert len(snapshot['_identity_shortlist']) == 17
        return verdict(alternative_candidate_ids=['wiki:17'])

    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id': story['id']}, '', hypotheses)
    assert result['status'] == 'match'
    assert calls == [(['wiki:1', 'wiki:2', 'wiki:3', 'wiki:4'], 2)]


@pytest.mark.asyncio
async def test_real_competitor_survives_bounded_reference_batches(tmp_path):
    from street_story.identity_visual import identify_nearest
    from test_identity_lifecycle import create
    service, _ = make_service(tmp_path)
    story = create(service)
    hypotheses = [subject(f'wiki:{i}', distance_m=i) for i in range(1, 18)]
    calls = []

    async def compare(snapshot, _transcript, batch, reference_limit):
        calls.append(len(batch))
        return verdict(batch[0]['candidate_id'], alternative_candidate_ids=['wiki:17'])

    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id': story['id']}, '', hypotheses)
    assert result['status'] == 'uncertain'
    assert calls == [4, 6, 6]


def test_bound_article_reuses_existing_registry_poi_and_never_registers_source_as_entity(tmp_path):
    service, _ = make_service(tmp_path)
    candidate = subject()
    identity = {'status': 'match', 'visual_reference_verified': True, 'candidate_id': 'wiki:1',
                'candidate_name': candidate['name'], 'candidate_url': candidate['url'], 'candidates': [candidate]}
    with service.store.tx() as db:
        existing_poi = ensure_poi_identity(db, identity, now=service.store.now())
        bound = bind_reference_subject(verdict('web:article', reference_subject_candidate_id='wiki:1'),
                                       [article()], [candidate], [image_evidence()])
        assert visual_match(bound['result'], [article()], [candidate])
        accepted = {**identity, 'candidate_id': bound['candidate']['candidate_id'],
                    'reference_evidence': bound['reference_evidence']}
        assert ensure_poi_identity(db, accepted, now=service.store.now()) == existing_poi
        assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM poi_aliases WHERE namespace='street_story_candidate' AND value='web:article'").fetchone()
