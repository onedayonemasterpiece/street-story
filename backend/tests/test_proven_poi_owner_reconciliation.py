"""Exact physical aliases reconcile registry ownership, not fact/evidence rows."""
import copy
import json

import pytest

from street_story.poi_external import ingest_poi_evidence
from street_story.poi_memory import ensure_poi_identity, memory_keys, persist_research_memory, prior_facts
from street_story.service import canonical
from test_identity_lifecycle import create, make_service
from test_poi_external import event


OSM = 'osm:way:123'
WIKI = 'wiki:77'
COMMONS = 'commonscat:gate'
WIKI_URL = 'https://ru.wikipedia.org/wiki/Physical_gate'
SOURCE_URL = 'https://archive.example/gate'


def fixture(tmp_path):
    service, _gemini = make_service(tmp_path)
    story = create(service)
    wiki = {'candidate_id': WIKI, 'name': 'Gate', 'url': WIKI_URL, 'wikidata': 'Q123'}
    osm = {'candidate_id': OSM, 'name': 'Gate', 'osm_id': OSM, 'wikidata': 'Q123',
           'wikipedia_url': WIKI_URL, 'url': 'https://www.openstreetmap.org/way/123'}
    identity = {'status': 'match', 'visual_reference_verified': True, 'candidate_id': OSM,
                'candidate_name': 'Gate', 'candidate_url': osm['url'], 'candidates': [osm, wiki]}
    with service.store.tx() as db:
        wiki_identity = {'candidate_id': WIKI, 'candidate_name': 'Gate', 'candidates': [wiki]}
        older = ensure_poi_identity(db, wiki_identity, now=1)
        db.execute("INSERT INTO poi_aliases VALUES(?,'street_story_candidate',?,?,1)", (older, COMMONS, COMMONS))
        newer = ensure_poi_identity(db, {'candidate_id': OSM, 'candidate_name': 'Gate',
            'candidates': [{'candidate_id': OSM, 'name': 'Gate', 'osm_id': OSM}]}, now=2)
        for n in range(12):
            key = WIKI if n < 6 else COMMONS
            text = f'Controlled public historical claim {n}.'
            source = {'url': SOURCE_URL, 'supports': [{'kind': 'verified_page_span',
                      'source_url': SOURCE_URL, 'text': text}]}
            persist_research_memory(db, {'candidate_id': key, 'candidate_name': 'Gate'},
                [{'fact_id': f'claim-{n}', 'claim_key': f'history-{n}', 'text': text, 'confidence': .95,
                  'sources': [source]}], [source], 'History', 3 + n)
        # A different candidate-key history already exists at the OSM owner.
        persist_research_memory(db, {'candidate_id': OSM, 'candidate_name': 'Gate'},
            [{'fact_id': 'osm-memory', 'claim_key': 'osm-detail', 'text': 'An earlier OSM-specific claim.',
              'confidence': .95, 'sources': [{'url': SOURCE_URL, 'supports': [{'kind': 'verified_page_span',
                  'source_url': SOURCE_URL, 'text': 'An earlier OSM-specific claim.'}]}]}], [], 'Details', 20)
        db.execute("UPDATE poi_research_assertions SET eligibility='eligible',review_status='eligible'")
        research = {'visual_identity': identity, 'poi_id': None, 'publication_concept': 'Owner concept',
                    'identity_generation': 0}
        db.execute("UPDATE stories SET state='identity_ready',research_json=?,draft_text=? WHERE id=?",
                   (canonical(research), 'Preserved owner draft', story['id']))
        owner_source = {'url': SOURCE_URL, 'supports': [{'kind': 'verified_page_span',
                        'source_url': SOURCE_URL, 'text': 'Owner-selected existing claim.'}]}
        db.execute('INSERT INTO facts VALUES(?,?,?,?,?,?,?)', (story['id'], 'owner-selected',
            'Owner-selected existing claim.', .95, 1, 1, canonical([owner_source])))
    assert older != newer
    return service, story['id'], identity, older, newer


def immutable_snapshot(db):
    return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('poi_research_assertions', 'poi_research_observations', 'poi_research_sources',
                          'poi_claims', 'poi_claim_evidence', 'poi_external_events', 'poi_conflicts')}


def aliases(db):
    return [tuple(row) for row in db.execute('SELECT * FROM poi_aliases ORDER BY namespace,normalized_value')]


def test_existing_two_owners_reuse_both_key_histories_without_copying_memory(tmp_path):
    service, sid, identity, older, newer = fixture(tmp_path)
    with service.store.tx() as db:
        before = immutable_snapshot(db)
        assert ensure_poi_identity(db, identity, now=30) == older
        assert immutable_snapshot(db) == before
        assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 2
        assert {row[0] for row in db.execute('SELECT id FROM pois')} == {older, newer}
        for key in (OSM, WIKI, COMMONS):
            assert set(memory_keys(db, {'candidate_id': key})) == {OSM, WIKI, COMMONS}
            assert {fact['fact_id'] for fact in prior_facts(db, {'candidate_id': key}, sid)} == {
                'osm-memory', *(f'claim-{n}' for n in range(12))}
        # A second binding from Wiki and then OSM is stable and idempotent.
        rebound = {**identity, 'candidate_id': WIKI, 'candidate_name': 'Gate', 'candidate_url': WIKI_URL}
        assert ensure_poi_identity(db, rebound, now=31) == older
        assert ensure_poi_identity(db, identity, now=32) == older


def test_normal_story_read_reconciles_bound_osm_and_adds_prior_memory_preserving_owner_content(tmp_path):
    service, sid, _identity, older, _newer = fixture(tmp_path)
    # No manual ensure/hydrate call: the ordinary product read repairs its missing
    # canonical owner even though the selected candidate was already registered.
    result = service.story(sid)
    assert result['draft_text'] == 'Preserved owner draft'
    assert len(result['facts']) == 14  # owner selection, 12 Wiki/Commons, OSM history
    assert {fact['fact_id'] for fact in result['facts'] if fact['selected']} == {'owner-selected'}
    with service.store.connection() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert research['poi_id'] == older
        assert research['publication_concept'] == 'Owner concept'
        assert research['visual_identity']['candidate_id'] == OSM
    revision = result['revision']
    assert service.story(sid)['revision'] == revision


@pytest.mark.parametrize('change', ['same_name', 'unproved_alias_list', 'uncertain', 'third_exact_owner', 'owner_cycle'])
def test_unproved_owner_conflicts_do_not_merge_or_expose_other_memory(tmp_path, change):
    service, _sid, identity, older, newer = fixture(tmp_path)
    identity = copy.deepcopy(identity)
    with service.store.tx() as db:
        if change in {'same_name', 'unproved_alias_list'}:
            identity['candidates'][0].pop('wikidata')
            identity['candidates'][0].pop('wikipedia_url')
            if change == 'unproved_alias_list':
                identity['candidates'][0]['alias_candidate_ids'] = [WIKI]
        elif change == 'uncertain':
            identity['status'] = 'uncertain'
        elif change == 'third_exact_owner':
            third = ensure_poi_identity(db, {'candidate_id': 'wiki:other', 'candidate_name': 'Gate',
                'candidates': [{'candidate_id': 'wiki:other', 'wikidata': 'Q999'}]}, now=0.5)
            assert third not in {older, newer}
            identity['candidates'][0]['wikidata'] = 'Q999'
        elif change == 'owner_cycle':
            db.execute("INSERT INTO poi_aliases VALUES(?,'poi_id',?,?,1)", (older, newer, newer))
            db.execute("INSERT INTO poi_aliases VALUES(?,'poi_id',?,?,1)", (newer, older, older))
        before = aliases(db), immutable_snapshot(db)
        result = ensure_poi_identity(db, identity, now=30)
        if change in {'same_name', 'unproved_alias_list'}:
            assert result == newer
        else:
            assert result is None
        assert not any(key in memory_keys(db, identity) for key in (WIKI, COMMONS))
        if result is None:
            assert (aliases(db), immutable_snapshot(db)) == before
        else:
            assert immutable_snapshot(db) == before[1]


def test_existing_external_graph_claims_and_aliases_stay_at_old_ids_with_public_family_visibility(tmp_path):
    service, _sid, identity, older, newer = fixture(tmp_path)
    public_wiki = ingest_poi_evidence(service.store, event(external_ids={'wikidata': 'Q123'},
        visibility='public', semantic_key='public-wiki', text='Public Wiki owner claim.'))
    public_osm = ingest_poi_evidence(service.store, event(external_ids={'osm_id': OSM},
        visibility='public', semantic_key='public-osm', text='Public OSM owner claim.'))
    private_osm = ingest_poi_evidence(service.store, event(external_ids={'osm_id': OSM},
        visibility='private', semantic_key='private-osm', text='Private owner claim.'))
    assert public_wiki['poi_id'] == older
    assert public_osm['poi_id'] == private_osm['poi_id'] == newer
    with service.store.tx() as db:
        before = immutable_snapshot(db)
        external_aliases = [tuple(row) for row in db.execute("SELECT * FROM poi_aliases WHERE namespace<>'street_story_candidate' ORDER BY namespace,normalized_value")]
        assert ensure_poi_identity(db, identity, now=40) == older
        assert immutable_snapshot(db) == before
        for alias in external_aliases:
            assert alias in [tuple(row) for row in db.execute('SELECT * FROM poi_aliases')]
        facts = prior_facts(db, identity, 'unused')
        regional = [fact for fact in facts if fact.get('sources', [{}])[0].get('type') == 'regional_knowledge']
        assert {fact['text'] for fact in regional} == {'Public Wiki owner claim.', 'Public OSM owner claim.'}
        assert {fact['poi_id'] for fact in regional} == {older, newer}
        assert all(fact['text'] != 'Private owner claim.' for fact in facts)


def test_equal_created_at_uses_stable_owner_id_independent_of_selected_alias(tmp_path):
    service, _sid, identity, older, newer = fixture(tmp_path)
    with service.store.tx() as db:
        db.execute('UPDATE pois SET created_at=1')
        assert ensure_poi_identity(db, identity, now=30) == min(older, newer)
        assert ensure_poi_identity(db, {**identity, 'candidate_id': WIKI}, now=31) == min(older, newer)


def test_host_geometry_reconciles_exact_physical_aliases_without_reference_receipts(tmp_path):
    from test_geometry_subject_articles import geometry_identity
    service, sid, identity, older, newer = fixture(tmp_path)
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        geometry = geometry_identity(candidate_id=OSM, photo=row['photo_sha256'], generation=0)
        geometry['candidates'] = identity['candidates']
        before = immutable_snapshot(db)
        assert ensure_poi_identity(db, geometry, now=30) == older
        assert immutable_snapshot(db) == before
        assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 2
        assert set(memory_keys(db, geometry)) == {OSM, WIKI, COMMONS}
        assert len(prior_facts(db, geometry, sid)) == 13
        assert geometry['visual_reference_verified'] is False and geometry['reference_evidence'] == []
        assert older != newer


def test_geometry_does_not_alias_neighbor_with_same_name_or_unverified_complex_list(tmp_path):
    from test_geometry_subject_articles import geometry_identity
    service, sid, identity, older, newer = fixture(tmp_path)
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        geometry = geometry_identity(candidate_id=OSM, photo=row['photo_sha256'], generation=0)
        candidates = copy.deepcopy(identity['candidates'])
        candidates[0].pop('wikidata')
        candidates[0].pop('wikipedia_url')
        candidates[0]['alias_candidate_ids'] = [WIKI]
        geometry['candidates'] = candidates
        before = immutable_snapshot(db)
        assert ensure_poi_identity(db, geometry, now=30) == newer
        assert immutable_snapshot(db) == before
        assert memory_keys(db, geometry) == [OSM]
        assert older != newer
