"""Whole same-source context is readable but never becomes attached support."""
import copy
import hashlib
import json
import sqlite3

import pytest

from street_story.headless_review_context import source_context
from street_story.headless_review_quotes import model_packet, with_quote_catalog
from street_story.service import ConflictError


@pytest.fixture
def db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE live_review_packets(packet_ref TEXT,story_id TEXT,payload_json TEXT);'
        'CREATE TABLE source_versions(source_version_id TEXT,content_sha256 TEXT,normalized_text TEXT,final_url TEXT);')
    own = 'According to the account, the house dates from 1900.'
    full = own + '\n' + 'Unrelated intermediate text. ' * 180 + '\nBibliography: The account author is A. Writer.'
    payload = {'items': [{'id': 'fact1', 'evidence': [{'source_version_id': 'own', 'text': own}]},
        {'id': 'fact2', 'evidence': [{'source_version_id': 'own', 'text': own}]}]}
    db.execute('INSERT INTO live_review_packets VALUES(?,?,?)', ('packet', 'story', json.dumps(payload)))
    for vid, text in [('own', full), ('foreign', 'Another source must not be borrowed.')]:
        db.execute('INSERT INTO source_versions VALUES(?,?,?,?)',
            (vid, hashlib.sha256(text.encode()).hexdigest(), text, 'https://example.org/' + vid))
    yield db
    db.close()


def test_full_own_context_is_read_once_without_attaching_or_truncating_it(db):
    before = db.total_changes
    original = db.execute('SELECT payload_json FROM live_review_packets').fetchone()[0]
    value = source_context(db, 'packet', 'story')
    assert len(value['documents']) == 1 and len(value['bindings']) == 2
    document = value['documents'][0]
    assert document['text'].endswith('Bibliography: The account author is A. Writer.')
    assert len(document['text']) > 5000 and document['complete'] and value['complete']
    assert document['source_version_id'] == 'own' and document['attached_as_support'] is False
    assert db.total_changes == before
    assert db.execute('SELECT payload_json FROM live_review_packets').fetchone()[0] == original
    assert {b['fact'] for b in value['bindings']} == {0, 1}


def test_context_cannot_borrow_a_foreign_story_packet(db):
    with pytest.raises(ConflictError) as error:
        source_context(db, 'packet', 'foreign-story')
    assert error.value.code == 'live_review_packet_unknown'


def test_changed_retained_body_is_not_fabricated_context(db):
    db.execute("UPDATE source_versions SET normalized_text='Changed body' WHERE source_version_id='own'")
    with pytest.raises(ConflictError) as error:
        source_context(db, 'packet', 'story')
    assert error.value.code == 'live_review_source_context_invalid'


def test_missing_snapshot_stays_explicitly_unavailable(db):
    db.execute("DELETE FROM source_versions WHERE source_version_id='own'")
    context = source_context(db, 'packet', 'story')
    assert context['complete'] is False
    assert context['documents'] == [{'source_version_id': 'own', 'available': False,
        'complete': False, 'attached_as_support': False}]
    assert all(b['source_version_id'] == 'own' for b in context['bindings'])


def test_context_does_not_expand_literal_quote_catalog_or_evidence_numbers(db):
    packet = with_quote_catalog({'packet_ref': 'packet', 'items': [
        {'fact': 0, 'evidence': 0, 'offset': 0, 'passage': 'Own attached literal.'}]})
    original = copy.deepcopy(packet)
    packet['own_source_context'] = source_context(db, 'packet', 'story')
    view = model_packet(packet)
    assert packet['items'] == original['items']
    assert packet['quote_catalog'] == original['quote_catalog']
    assert view['facts'][0]['evidence'][0]['slices'] == [{
        'offset': 0, 'passage': 'Own attached literal.', 'quote_ref': original['items'][0]['quote_ref']}]
    assert view['own_source_context']['documents'][0]['attached_as_support'] is False
