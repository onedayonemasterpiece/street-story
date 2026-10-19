import json
import sqlite3

from street_story.mvp_research import STATELESS_CALL_SETTLED_SECONDS, original_operation_pending


def _db(*receipts):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE research_provider_attempts(attempt_id TEXT, story_id TEXT, receipt_json TEXT, updated_at REAL)')
    for index, (receipt, updated_at) in enumerate(receipts):
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?)',
                   (f'a{index}', 's1', json.dumps(receipt), updated_at))
    return db


def _receipt(**fields):
    return {'phase': 'unknown', 'provider_send_state': 'possibly_sent',
            'binding': {'job_id': 'job_old', 'generation': 2}, **fields}


def test_lost_direct_http_call_stops_blocking_the_topic():
    # 19.10.2026: a Gemini confirmation that timed out stayed 'unknown' and the
    # resume job of the topic waited on it for three days (7 000+ retries).
    now = 10_000.0
    fresh = _db((_receipt(provider_id='google'), now - 60))
    stale = _db((_receipt(provider_id='google'), now - STATELESS_CALL_SETTLED_SECONDS - 1))
    stale_giga = _db((_receipt(provider_id='gigachat'), now - STATELESS_CALL_SETTLED_SECONDS - 1))
    assert original_operation_pending(fresh, 's1', 'job_new', 2, now)
    assert not original_operation_pending(stale, 's1', 'job_new', 2, now)
    assert not original_operation_pending(stale_giga, 's1', 'job_new', 2, now)


def test_addressable_operations_still_block_until_observed():
    now = 10_000.0
    old = now - 10 * STATELESS_CALL_SETTLED_SECONDS
    thread = _db((_receipt(provider_id='openai', thread_id='t1', turn_id='u1'), old))
    session = _db((_receipt(provider_id='google', session_id='s', message_id='m'), old))
    assert original_operation_pending(thread, 's1', 'job_new', 2, now)
    assert original_operation_pending(session, 's1', 'job_new', 2, now)
    # Own job, another generation and settled phases never block.
    assert not original_operation_pending(thread, 's1', 'job_old', 2, now)
    assert not original_operation_pending(thread, 's1', 'job_new', 3, now)
    done = _db((_receipt(provider_id='openai', thread_id='t1', phase='completed'), old))
    assert not original_operation_pending(done, 's1', 'job_new', 2, now)
