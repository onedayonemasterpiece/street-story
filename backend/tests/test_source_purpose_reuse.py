"""Frozen article acquisition is independent of text/media/comparison work."""
import json

import pytest

from street_story.db import Store
from street_story.research_runs import (
    attach_source_version, begin_research_run, chunk_checkpoint, mark_chunk,
    persist_source_version, record_chunk_batch, reusable_source_document, source_coverage,
)
from test_research_runs import create_story


URL = 'https://example.org/gate-gallery'
TEXT = 'A literal public article passage about architecture. ' * 40


def begin(db, run, story, *, goal='History', scope=None, poi='wiki:77'):
    return begin_research_run(db, story_id=story, poi_key=poi, goal=goal, scope=scope,
                              expected_story_revision=1, identity_generation=0, run_id=run, now=10)


def snapshot(db, run, *, read_status='complete', access_scope='story', now=10):
    return persist_source_version(db, run_id=run, requested_url=URL, final_url=URL,
        title='Gallery', content_type='text/html', http_status=200, redirect_chain=[],
        normalized_text=TEXT, read_status=read_status, access_scope=access_scope, now=now)


def finish_text(db, run, document):
    for chunk in document['chunks']:
        record_chunk_batch(db, run_id=run, chunk_id=chunk['chunk_id'], batch_index=0,
            status='completed', raw_fact_count=0, accepted_fact_count=0,
            continuation_needed=False, continuation_reason='', model_name='checked-model',
            prompt_version='live-chunk-findings-v1', now=11,
            payload={'facts': [], 'source_content_valid': True, 'no_claims': True})
        mark_chunk(db, run_id=run, chunk_id=chunk['chunk_id'], status='no_claims',
            observation_count=0, model_name='checked-model', prompt_version='live-chunk-findings-v1', now=11)


def confirm(db, story, candidate='wiki:77'):
    db.execute('UPDATE stories SET research_json=? WHERE id=?',
               (json.dumps({'identity_generation': 0, 'visual_identity': {
                   'status': 'match', 'candidate_id': candidate, 'candidate_name': 'Gate'}}), story))


def test_more_wording_reuses_same_checked_scope_without_refetch(tmp_path):
    store = Store(tmp_path / 'reuse.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'first', story, goal='Find architectural facts', scope='architecture')
        document = snapshot(db, 'first')
        finish_text(db, 'first', document)
        begin(db, 'more', story, goal='Find even more interesting facts please', scope='architecture')
        reused = reusable_source_document(db, run_id='more', url=URL, now=12)
        assert reused['source_version_id'] == document['source_version_id']
        assert chunk_checkpoint(db, 'more', document['chunks'][0]['chunk_id'])['terminal']
        assert source_coverage(db, ['wiki:77'])[URL][0]['scope'] == 'architecture'
        assert db.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM poi_research_observations').fetchone()[0] == 0


def test_new_model_supplied_aspect_reuses_bytes_but_not_text_extraction(tmp_path):
    store = Store(tmp_path / 'scope.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'first', story, scope='architecture')
        document = snapshot(db, 'first')
        finish_text(db, 'first', document)
        begin(db, 'events', story, scope='recent cultural events')
        reused = reusable_source_document(db, run_id='events', url=URL, now=12)
        assert reused['normalized_text'] == TEXT
        assert not chunk_checkpoint(db, 'events', document['chunks'][0]['chunk_id'])['terminal']
        assert db.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0] == 1


def test_same_snapshot_keeps_original_partition_for_consumers_with_different_window_sizes(tmp_path):
    store = Store(tmp_path / 'partition.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'first', story, scope='architecture')
        document = persist_source_version(db, run_id='first', requested_url=URL, final_url=URL,
            title='Gallery', content_type='text/html', http_status=200, redirect_chain=[],
            normalized_text=TEXT, read_status='complete', target_chars=500, overlap_chars=100, now=10)
        finish_text(db, 'first', document)
        begin(db, 'second', story, scope='architecture')
        repeated = persist_source_version(db, run_id='second', requested_url=URL, final_url=URL,
            title='Gallery', content_type='text/html', http_status=200, redirect_chain=[],
            normalized_text=TEXT, read_status='complete', target_chars=2000, overlap_chars=200, now=12)
        assert [c['chunk_id'] for c in repeated['chunks']] == [c['chunk_id'] for c in document['chunks']]
        assert db.execute('SELECT COUNT(*) FROM source_chunks').fetchone()[0] == len(document['chunks'])
        assert all(chunk_checkpoint(db, 'second', c['chunk_id'])['terminal'] for c in document['chunks'])
        assert source_coverage(db, ['wiki:77'])[URL][0]['completed']


def test_more_resumes_checked_partial_checkpoint_without_repeating_saved_prefix(tmp_path):
    store = Store(tmp_path / 'resume.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'partial', story, goal='Read architectural facts', scope='architecture')
        document = snapshot(db, 'partial')
        chunk = document['chunks'][0]
        record_chunk_batch(db, run_id='partial', chunk_id=chunk['chunk_id'], batch_index=0,
            status='continuation', raw_fact_count=0, accepted_fact_count=0,
            continuation_needed=True, continuation_reason='Remaining article passages',
            model_name='checked-model', prompt_version='live-chunk-findings-v1', now=11,
            payload={'facts': [], 'source_content_valid': True, 'no_claims': True})
        mark_chunk(db, run_id='partial', chunk_id=chunk['chunk_id'], status='deferred',
            observation_count=0, model_name='checked-model', prompt_version='live-chunk-findings-v1', now=11)
        begin(db, 'more', story, goal='More facts from unread parts', scope='architecture')
        reused = reusable_source_document(db, run_id='more', url=URL, now=12)
        assert reused['source_version_id'] == document['source_version_id']
        checkpoint = chunk_checkpoint(db, 'more', chunk['chunk_id'])
        assert not checkpoint['terminal'] and checkpoint['next_batch_index'] == 1
        assert db.execute('SELECT reuse_kind FROM research_chunk_runs WHERE run_id=? AND chunk_id=?',
                          ('more', chunk['chunk_id'])).fetchone()[0] == 'resumed_partial'
        assert not source_coverage(db, ['wiki:77'])[URL][0]['completed']


def test_partial_snapshot_is_not_promoted_by_identical_complete_bytes(tmp_path):
    store = Store(tmp_path / 'partial.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'partial', story)
        partial = snapshot(db, 'partial', read_status='partial_text_limit')
        finish_text(db, 'partial', partial)
        begin(db, 'reader', story)
        reused = reusable_source_document(db, run_id='reader', url=URL, now=11)
        assert reused['read_status'] == 'partial_text_limit'
        assert not source_coverage(db, ['wiki:77'])[URL][0]['completed']
        begin(db, 'complete', story)
        complete = snapshot(db, 'complete', now=12)
        assert partial['source_version_id'] != complete['source_version_id']
        assert db.execute('SELECT read_status FROM source_versions WHERE source_version_id=?',
                          (partial['source_version_id'],)).fetchone()[0] == 'partial_text_limit'


@pytest.mark.parametrize('access_scope,confirmed,same_poi,expected', [
    ('story', True, True, False), ('public', False, True, False),
    ('public', True, False, False), ('public', True, True, True),
])
def test_cross_story_snapshot_needs_explicit_public_and_confirmed_physical_subject(
    tmp_path, access_scope, confirmed, same_poi, expected,
):
    store = Store(tmp_path / 'access.sqlite3')
    a, b = create_story(store, 'story_a'), create_story(store, 'story_b')
    with store.tx() as db:
        begin(db, 'donor', a)
        begin(db, 'receiver', b, poi='wiki:77' if same_poi else 'wiki:88')
        if confirmed:
            confirm(db, a)
            confirm(db, b, 'wiki:77' if same_poi else 'wiki:88')
        document = snapshot(db, 'donor', access_scope=access_scope)
        reused = reusable_source_document(db, run_id='receiver', url=URL, now=11)
        assert bool(reused) is expected
        if expected:
            assert reused['source_version_id'] == document['source_version_id']
        assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0


@pytest.mark.parametrize('guard', ['stale', 'cancelled', 'wrong_url', 'expired', 'rebind'])
def test_snapshot_attachment_rejects_stale_cancelled_wrong_or_expired_binding(tmp_path, guard):
    store = Store(tmp_path / 'guards.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        begin(db, 'donor', story)
        document = snapshot(db, 'donor')
        begin(db, 'target', story)
        if guard == 'stale':
            db.execute('UPDATE stories SET research_json=\'{"identity_generation":1}\' WHERE id=?', (story,))
        elif guard == 'cancelled':
            db.execute("UPDATE research_runs SET state='cancelled' WHERE run_id='target'")
        elif guard == 'rebind':
            snapshot(db, 'target', read_status='partial')
        if guard == 'expired':
            result = reusable_source_document(db, run_id='target', url=URL, now=86411)
        else:
            result = attach_source_version(db, run_id='target', source_version_id=document['source_version_id'],
                requested_url=URL + '/invented' if guard == 'wrong_url' else URL, now=12)
        assert result is None
