"""Saved public article acquisition is useful before a new photo is identified."""
import json

import pytest

from street_story import article_media, identity_discovery
from street_story.poi_memory import candidate_article_sources, ensure_poi_identity
from street_story.service import canonical
from test_article_source_retention import article, images, reject
from test_visual_search_continuation import prepared


def seed(service, count=1):
    identity = {'status': 'match', 'visual_reference_verified': True, 'candidate_id': 'wiki:77',
                'candidate_name': 'Existing gate', 'candidates': [{'candidate_id': 'wiki:77', 'name': 'Existing gate'}]}
    with service.store.tx() as db:
        ensure_poi_identity(db, identity, now=service.store.now())
        for index in range(count):
            db.execute('INSERT INTO poi_research_sources VALUES(?,?,?,?,?,?,?)',
                ('wiki:77', f'https://archive.example/history-{index}', 'Known text source', '[]', 'Earlier research', 1, 2))


def shortlist(service, sid):
    with service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({'identity_generation': 0,
            'visual_identity': {'status': 'uncertain', 'candidates': [{'candidate_id': 'wiki:77', 'name': 'Existing gate'}]}}), sid))


def test_exact_binding_reuses_all_urls_without_name_join_or_global_cap(tmp_path):
    service, _, _, _ = prepared(tmp_path)
    seed(service, 245)
    with service.store.connection() as db:
        sources = candidate_article_sources(db, [{'candidate_id': 'wiki:77'}])
        assert len(sources) == 245
        assert all(source['discovery_provider'] == 'poi_memory' for source in sources)
        assert not candidate_article_sources(db, [{'candidate_id': 'wiki:999', 'name': 'Existing gate'}])
        assert not candidate_article_sources(db, [{'candidate_id': 'wiki:77', 'identity_eligible': False}])


def test_owner_alias_cycle_and_private_urls_fail_closed(tmp_path):
    service, _, _, _ = prepared(tmp_path)
    seed(service)
    with service.store.tx() as db:
        db.execute('INSERT INTO poi_research_sources VALUES(?,?,?,?,?,?,?)',
            ('wiki:77', 'http://127.0.0.1/private', 'Private', '[]', '', 1, 2))
        assert len(candidate_article_sources(db, [{'candidate_id': 'wiki:77'}])) == 1
        owner = db.execute("SELECT poi_id FROM poi_aliases WHERE namespace='street_story_candidate' AND value='wiki:77'").fetchone()[0]
        other = ensure_poi_identity(db, {'candidate_id': 'wiki:78', 'candidate_name': 'Other gate'}, now=1)
        db.execute("INSERT INTO poi_aliases VALUES(?,'poi_id',?,?,1)", (other, owner, owner))
        db.execute("INSERT INTO poi_aliases VALUES(?,'poi_id',?,?,1)", (owner, other, other))
        assert not candidate_article_sources(db, [{'candidate_id': 'wiki:77'}])


@pytest.mark.asyncio
async def test_new_photo_gets_fresh_verdict_and_uses_saved_articles_before_search(tmp_path, monkeypatch):
    service, adapter, story, sessions = prepared(tmp_path)
    seed(service)
    shortlist(service, story['id'])
    fetches = []

    async def fetch(svc, row, batch, excluded, *, receipts):
        source = batch[0]
        fetches.append(source)
        receipts.append({'url': source['url'], 'status': 'completed'})
        return [article(source['url'])]

    async def forbidden(*args, **kwargs):
        pytest.fail('Known acquisition hints must be consumed before spending on search')

    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    images(service)
    session = sessions()
    first = await adapter._compare_place_images(session, {})
    assert first['comparison_id'] and fetches[0]['discovery_provider'] == 'poi_memory'
    _, research = service._identity_snapshot(story['id'])
    assert research['visual_identity']['status'] == 'uncertain' and not research.get('poi_id')
    assert not research['visual_search_operation']['reviewed_reference_ids']
    assert research['visual_search_operation']['sources'][fetches[0]['url']]['status'] == 'completed'
    reject(adapter, session, first)
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['identity_generation'] = 1
        db.execute('UPDATE stories SET photo_sha256=?,research_json=? WHERE id=?',
                   ('different-source-upload-token', canonical(research), story['id']))
    second = await adapter._compare_place_images(sessions(), {})
    assert second['comparison_id'] != first['comparison_id']
    assert len(fetches) == 2  # Media interpretation for photo B, not another search.
    _, research = service._identity_snapshot(story['id'])
    assert research['visual_identity']['status'] == 'uncertain'
    assert not research['visual_search_operation']['reviewed_reference_ids']


@pytest.mark.asyncio
async def test_memory_arriving_during_pending_unit_preserves_pending_pair(tmp_path, monkeypatch):
    service, adapter, story, sessions = prepared(tmp_path)
    shortlist(service, story['id'])
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['visual_identity']['candidates'][0]['reference_image_urls'] = ['https://archive.example/lead.jpg']
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    images(service)
    session = sessions()
    first = await adapter._compare_place_images(session, {})
    seed(service, 245)
    second = await adapter._compare_place_images(session, {})
    assert second['comparison_id'] == first['comparison_id']
    _, research = service._identity_snapshot(story['id'])
    assert len(research['visual_search_operation']['sources']) == 245
    assert not research['visual_search_operation']['reviewed_reference_ids']
