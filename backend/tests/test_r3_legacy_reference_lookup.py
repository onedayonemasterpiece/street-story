import base64
import hashlib
import json
import sqlite3

import pytest

from street_story.poi_memory import candidate_reference_images


ARTICLE = 'https://history.example/article'
IMAGE = 'https://history.example/gate.jpg'


def legacy_reference_store(*, image_url=IMAGE, final_url=ARTICLE, valid_hash=True):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE poi_aliases(namespace TEXT,normalized_value TEXT,poi_id TEXT);
      CREATE TABLE stories(id TEXT,research_json TEXT,updated_at REAL);
      CREATE TABLE cache(key TEXT,value_json TEXT);
    ''')
    research = {'visual_identity': {'status': 'match', 'visual_reference_verified': True,
        'candidate_id': 'osm:way:1', 'candidate_name': 'Physical POI name',
        'reference_evidence': [{'candidate_id': 'web:example', 'subject_candidate_id': 'osm:way:1',
            'article_url': ARTICLE, 'image_url': IMAGE, 'model_image_sha256': 'b' * 64}]}}
    body = f'<h1>Original article H1</h1><article><img src="{image_url}"></article>'.encode()
    entry = {'body': base64.b64encode(body).decode(), 'final_url': final_url,
        'sha256': hashlib.sha256(body).hexdigest() if valid_hash else '0' * 64}
    db.execute('INSERT INTO stories VALUES(?,?,?)', ('story_original', json.dumps(research), 1))
    db.execute('INSERT INTO cache VALUES(?,?)',
        ('public-article-acquisition-v1:' + hashlib.sha256(ARTICLE.encode()).hexdigest(), json.dumps(entry)))
    return db, entry


def test_legacy_accepted_reference_recovers_hash_and_original_title_without_mutating_history():
    db, entry = legacy_reference_store()
    try:
        original = db.execute('SELECT research_json FROM stories').fetchone()[0]
        changes = db.total_changes
        refs = candidate_reference_images(db, [{'candidate_id': 'osm:way:1'}])
        assert len(refs) == 1
        ref = refs[0]
        assert ref['name'] == 'Original article H1'
        assert ref['article_media'][0]['article_source_sha256'] == entry['sha256']
        assert ref['reference_image_urls'] == [IMAGE]
        assert ref['reference_reuse']['subject_candidate_id'] == 'osm:way:1'
        assert ref['reference_reuse']['story_id'] == 'story_original'
        assert ref['article_media'][0]['model_image_sha256'] == 'b' * 64
        assert db.total_changes == changes
        assert db.execute('SELECT research_json FROM stories').fetchone()[0] == original
        assert 'owner_confirmed' not in ref and 'confidence' not in ref
    finally:
        db.close()


@pytest.mark.parametrize('arguments', [
    {'image_url': 'https://history.example/different-building.jpg'},
    {'final_url': 'https://history.example/another-article'},
    {'valid_hash': False},
])
def test_legacy_reference_without_valid_addressed_descriptor_is_not_reused(arguments):
    db, _entry = legacy_reference_store(**arguments)
    try:
        changes = db.total_changes
        assert candidate_reference_images(db, [{'candidate_id': 'osm:way:1'}]) == []
        assert db.total_changes == changes
    finally:
        db.close()
