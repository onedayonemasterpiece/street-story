"""Explicit Stop fences background work without discarding publication state."""
import json

import pytest

from street_story.research_control import research_stopped, resume_research, stop_research
from street_story.research_runs import (
    acquire_chunk_lease, begin_research_run, chunk_checkpoint, chunk_lease_owned,
    persist_source_version, record_chunk_batch,
)
from street_story.service import ConflictError
from test_mvp_research import service


def fixture(tmp_path):
    svc, _, story = service(tmp_path)
    sid = story['id']
    with svc.store.tx() as db:
        story = dict(svc._story_row(db, sid))
        research = {'identity_generation': 0, 'visual_identity': {'status': 'match', 'candidate_id': 'wiki:1'},
                    'publication_concept': 'Owner concept', 'visual_search_operation': {
                        'generation': 0, 'photo_sha256': story['photo_sha256'], 'lease_owner': 'old-visual',
                        'lease_until': svc.store.now() + 180, 'queue': ['remaining-ref'],
                        'pending': {'id': 'existing-comparison'}, 'seen_images': ['completed-ref']}}
        db.execute('UPDATE stories SET research_json=?,draft_text=? WHERE id=?',
                   (json.dumps(research), 'Owner draft', sid))
        for kind in ('identity', 'identity_visual', 'research', 'refinement', 'visual', 'publish'):
            svc._enqueue_job(db, sid, kind, 'control-' + kind, {
                'identity_generation': 0, 'photo_sha256': story['photo_sha256']})
        db.execute("UPDATE jobs SET state='running',attempts=3,lease_until=? WHERE kind IN ('research','identity_visual')",
                   (svc.store.now() + 90,))
        svc._enqueue_job(db, sid, 'research', 'control-completed', {'identity_generation': 0})
        db.execute("UPDATE jobs SET state='done' WHERE semantic_key='control-completed'")
        begin_research_run(db, story_id=sid, poi_key='wiki:1', goal='History', scope='history',
                           expected_story_revision=story['revision'], identity_generation=0, run_id='partial-run', now=svc.store.now())
    return svc, sid, story['photo_sha256']


def rows(svc, sid):
    with svc.store.connection() as db:
        return {r['semantic_key']: dict(r) for r in db.execute('SELECT * FROM jobs WHERE story_id=?', (sid,))}


def research(svc, sid):
    with svc.store.connection() as db:
        return json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])


def test_stop_is_scoped_and_preserves_publication_and_visual_queue(tmp_path):
    svc, sid, sha = fixture(tmp_path)
    before = rows(svc, sid)
    stopped = stop_research(svc, sid, purpose='identity', expected_photo_sha256=sha, expected_identity_generation=0)
    after = rows(svc, sid)
    assert stopped['changed'] == ['identity']
    assert after['control-identity']['state'] == after['control-identity_visual']['state'] == 'cancelled'
    assert after['control-identity_visual']['attempts'] == 4
    for key in ('control-research', 'control-refinement', 'control-visual', 'control-publish', 'control-completed'):
        assert after[key] == before[key]
    state = research(svc, sid)
    assert state['publication_concept'] == 'Owner concept'
    assert stopped['story']['draft_text'] == 'Owner draft'
    visual = state['visual_search_operation']
    assert visual['lease_owner'] is None and visual['lease_until'] == 0
    assert visual['queue'] == ['remaining-ref'] and visual['pending']['id'] == 'existing-comparison'
    assert research_stopped(state, 'identity', photo_sha256=sha, identity_generation=0)
    assert not research_stopped(state, 'identity', photo_sha256=sha, identity_generation=1)
    assert not research_stopped(state, 'facts', photo_sha256=sha, identity_generation=0)


def test_stop_and_resume_keep_same_job_ids_and_idempotent_state(tmp_path):
    svc, sid, _ = fixture(tmp_path)
    first = stop_research(svc, sid)
    before = rows(svc, sid)
    assert stop_research(svc, sid)['changed'] == []
    assert rows(svc, sid) == before
    result = resume_research(svc, sid)
    after = rows(svc, sid)
    assert set(after) == set(before)
    assert len(result['resumed_job_ids']) == 4
    assert result['research_control_revision'] > first['research_control_revision']
    for key in ('control-identity', 'control-identity_visual', 'control-research', 'control-refinement'):
        assert after[key]['state'] == 'retry' and after[key]['id'] == before[key]['id']
        assert after[key]['lease_until'] == 0
    assert resume_research(svc, sid)['changed'] == []
    assert rows(svc, sid) == after
    with svc.store.connection() as db:
        assert db.execute("SELECT state FROM research_runs WHERE run_id='partial-run'").fetchone()[0] == 'partial'


@pytest.mark.parametrize('binding_change', ['photo', 'generation'])
def test_resume_does_not_revive_old_scope_after_binding_change(tmp_path, binding_change):
    svc, sid, sha = fixture(tmp_path)
    stop_research(svc, sid)
    with svc.store.tx() as db:
        state = research(svc, sid)
        if binding_change == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b' * 64, sid))
        else:
            state['identity_generation'] = 1
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(state), sid))
    before = rows(svc, sid)
    with pytest.raises(ConflictError):
        resume_research(svc, sid)
    assert rows(svc, sid) == before
    with pytest.raises(ConflictError):
        stop_research(svc, sid, expected_photo_sha256=sha, expected_identity_generation=0)


def test_joined_requests_are_hidden_from_recovery_until_explicit_resume(tmp_path):
    svc, sid, sha = fixture(tmp_path)
    request = {'identity_generation': 0, 'photo_sha256': sha, 'input_revision': 'history-followup',
               'coverage_goal': 'More history', 'extraction_scope': 'history'}
    with svc.store.tx() as db:
        state = research(svc, sid)
        state['pending_fact_request'] = request
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(state), sid))
    stop_research(svc, sid, purpose='facts')
    state = research(svc, sid)
    assert 'pending_fact_request' not in state
    assert state['research_controls']['facts']['pending_fact_request'] == request
    assert svc.recover_jobs() == 0
    assert len(rows(svc, sid)) == 7
    resume_research(svc, sid, purpose='facts')
    assert research(svc, sid)['pending_fact_request'] == request
    assert len(rows(svc, sid)) == 7


def test_resume_never_reopens_completed_or_independently_cancelled_work(tmp_path):
    svc, sid, _ = fixture(tmp_path)
    with svc.store.tx() as db:
        begin_research_run(db, story_id=sid, poi_key='wiki:1', goal='Done', scope='done',
                           expected_story_revision=1, identity_generation=0, run_id='done-run', now=svc.store.now())
        db.execute("UPDATE research_runs SET state='completed' WHERE run_id='done-run'")
    stop_research(svc, sid, purpose='facts')
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET last_error='identity_changed' WHERE kind='refinement'")
        db.execute("UPDATE research_runs SET status_detail='identity_changed' WHERE run_id='partial-run'")
    resume_research(svc, sid, purpose='facts')
    after = rows(svc, sid)
    assert after['control-refinement']['state'] == 'cancelled'
    assert after['control-completed']['state'] == 'done'
    with svc.store.connection() as db:
        assert db.execute("SELECT state FROM research_runs WHERE run_id='done-run'").fetchone()[0] == 'completed'
        assert db.execute("SELECT state FROM research_runs WHERE run_id='partial-run'").fetchone()[0] == 'cancelled'


def test_stop_resume_preserves_partial_cursor_and_fences_old_source_and_worker(tmp_path):
    svc, sid, _ = fixture(tmp_path)
    with svc.store.tx() as db:
        doc = persist_source_version(db, run_id='partial-run', requested_url='https://example.org/history',
                                     final_url='https://example.org/history', title='History', content_type='text/html',
                                     http_status=200, redirect_chain=[], normalized_text='Documented history. ' * 80,
                                     read_status='complete', now=svc.store.now())
        chunk = doc['chunks'][0]['chunk_id']
        record_chunk_batch(db, run_id='partial-run', chunk_id=chunk, batch_index=0, status='continuation',
                           raw_fact_count=0, accepted_fact_count=0, continuation_needed=True,
                           continuation_reason='Additional passage page', model_name='controlled',
                           prompt_version='live-chunk-findings-v1', now=svc.store.now(),
                           payload={'facts': [], 'source_content_valid': True})
        fence = acquire_chunk_lease(db, run_id='partial-run', chunk_id=chunk, owner='old-extractor', now=svc.store.now())
        cursor = chunk_checkpoint(db, 'partial-run', chunk)
    old_job = rows(svc, sid)['control-research']
    stop_research(svc, sid, purpose='facts')
    resume_research(svc, sid, purpose='facts')
    with svc.store.tx() as db:
        assert chunk_checkpoint(db, 'partial-run', chunk) == cursor
        assert not chunk_lease_owned(db, run_id='partial-run', chunk_id=chunk, owner='old-extractor', fence=fence, now=svc.store.now())
        new_fence = acquire_chunk_lease(db, run_id='partial-run', chunk_id=chunk, owner='new-extractor', now=svc.store.now())
        assert new_fence > fence
        # The root worker must apply this ownership predicate at every finish.
        assert db.execute("UPDATE jobs SET state='done' WHERE id=? AND state='running' AND attempts=?",
                          (old_job['id'], old_job['attempts'])).rowcount == 0


def test_finished_fact_and_owner_selection_survive_stop_resume(tmp_path):
    svc, sid, _ = fixture(tmp_path)
    with svc.store.tx() as db:
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,"
                   "review_status,eligibility,revision_digest,created_at,updated_at) "
                   "VALUES(?,'existing-fact','history-opening','An already saved fact.',1,'eligible','eligible','proof-v1',?,?)",
                   (sid, svc.store.now(), svc.store.now()))
        before = dict(db.execute("SELECT * FROM fact_assertions WHERE story_id=?", (sid,)).fetchone())
    stop_research(svc, sid, purpose='facts')
    resume_research(svc, sid, purpose='facts')
    with svc.store.connection() as db:
        assert dict(db.execute("SELECT * FROM fact_assertions WHERE story_id=?", (sid,)).fetchone()) == before
        assert db.execute('SELECT draft_text FROM stories WHERE id=?', (sid,)).fetchone()[0] == 'Owner draft'


@pytest.mark.parametrize('purpose', ['publication', '', None])
def test_unknown_purpose_fails_without_mutation(tmp_path, purpose):
    svc, sid, _ = fixture(tmp_path)
    before = rows(svc, sid)
    with pytest.raises(ValueError):
        stop_research(svc, sid, purpose=purpose)
    assert rows(svc, sid) == before
