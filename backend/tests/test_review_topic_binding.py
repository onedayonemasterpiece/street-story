"""An omitted review reference may reuse only this session's exposed scope."""
import json

import pytest

from test_facts_research_finish import QUOTES, fallback, findings
from street_story.service import ConflictError


@pytest.fixture
async def review(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    try:
        chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
        await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-binding',
                                             'args': findings(chunk, QUOTES)})
        session.capability = 'review'
        with svc.store.connection() as db:
            fact_id = db.execute('SELECT assertion_id FROM fact_assertions ORDER BY assertion_id LIMIT 1').fetchone()[0]
        yield svc, adapter, session, run_id, fact_id
    finally:
        await reader.search_http.aclose()


def unchanged_state(svc):
    with svc.store.connection() as db:
        return {table: [tuple(r) for r in db.execute(query)] for table, query in {
            'packets': 'SELECT * FROM live_review_packets ORDER BY packet_ref',
            'runs': 'SELECT * FROM research_runs ORDER BY run_id',
            'facts': 'SELECT * FROM fact_assertions ORDER BY assertion_id',
            'jobs': 'SELECT * FROM jobs ORDER BY id',
        }.items()}


async def read_topic(adapter, session):
    return await adapter.execute_tool(session, {'name': 'read_topic', 'args': {}})


@pytest.mark.asyncio
async def test_omitted_run_uses_exact_exposed_run_and_real_packet_for_pagination(review):
    svc, adapter, session, run_id, fact_id = review
    # Historical runs may coexist; omission must not search for a sole active run.
    with svc.store.tx() as db:
        row = dict(db.execute('SELECT * FROM research_runs WHERE run_id=?', (run_id,)).fetchone())
        row.update(run_id='historical-run', created_at=row['created_at'] - 100, state='partial')
        db.execute(f"INSERT INTO research_runs({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    topic = await read_topic(adapter, session)
    assert topic['research_run']['run_id'] == run_id
    before = unchanged_state(svc)
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'fact_ids': [fact_id]}})
    assert packet['run_id'] == run_id and packet['total_facts'] == 1
    assert packet['items'][0]['saved_verdict'] is None
    page = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'packet_ref': packet['packet_ref'], 'cursor': 0}})
    assert page['packet_ref'] == packet['packet_ref']
    after = unchanged_state(svc)
    assert len(after['packets']) == len(before['packets']) + 1
    assert after['facts'] == before['facts'] and after['jobs'] == before['jobs']
    assert len(after['runs']) == len(before['runs'])


@pytest.mark.asyncio
async def test_missing_read_requires_read_topic_and_does_not_create_packet(review):
    svc, adapter, session, _, fact_id = review
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'fact_ids': [fact_id]}})
    assert exc.value.code == 'live_review_run_required'
    assert unchanged_state(svc) == before
    # Another session's initialized context is not this session's read receipt.
    other = type(session)(**vars(session))
    other.state = dict(session.state)
    await read_topic(adapter, session)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(other, {'name': 'get_review_packet', 'args': {'fact_ids': [fact_id]}})
    assert exc.value.code == 'live_review_run_required'
    assert unchanged_state(svc) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['photo', 'generation', 'subject', 'facts_epoch', 'identity_epoch', 'stop', 'latest_run'])
async def test_scope_drift_rejects_before_packet_or_reopen_write(review, drift):
    svc, adapter, session, run_id, fact_id = review
    await read_topic(adapter, session)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        if drift == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('f' * 64, session.resource_id))
        elif drift == 'generation':
            research['identity_generation'] = int(research.get('identity_generation') or 0) + 1
        elif drift == 'subject':
            research['visual_identity']['candidate_id'] = 'different-owner-building'
        elif drift in {'facts_epoch', 'identity_epoch'}:
            purpose = 'facts' if drift == 'facts_epoch' else 'identity'
            control = research.setdefault('research_controls', {}).setdefault(purpose, {})
            control['revision'] = int(control.get('revision') or 0) + 1
        elif drift == 'stop':
            research['fact_research_cancelled'] = True
            research.setdefault('research_controls', {}).pop('facts', None)
        elif drift == 'latest_run':
            run = dict(db.execute('SELECT * FROM research_runs WHERE run_id=?', (run_id,)).fetchone())
            run.update(run_id='new-current-run', created_at=run['created_at'] + 100)
            db.execute(f"INSERT INTO research_runs({','.join(run)}) VALUES({','.join('?' for _ in run)})", tuple(run.values()))
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
        db.execute("UPDATE research_runs SET state='completed' WHERE run_id=?", (run_id,))
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'fact_ids': [fact_id]}})
    assert exc.value.code in {'live_review_run_stale', 'live_research_stopped', 'live_research_control_changed'}
    assert unchanged_state(svc) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('reference,code', [
    ({'run_id': 'foreign-run'}, 'live_research_run_unknown'),
    ({'run_id': ''}, 'live_research_run_unknown'),
    ({'packet_ref': 'invented-packet'}, 'live_review_packet_unknown'),
    ({'packet_ref': ''}, 'live_research_run_unknown'),
    ({'supersedes_packet_ref': 'invented-packet'}, 'live_review_packet_unknown'),
])
async def test_explicit_bad_reference_is_never_replaced_by_session_binding(review, reference, code):
    svc, adapter, session, _, fact_id = review
    await read_topic(adapter, session)
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'get_review_packet',
            'args': {'fact_ids': [fact_id], **reference}})
    assert exc.value.code == code
    assert unchanged_state(svc) == before


@pytest.mark.asyncio
async def test_run_binding_does_not_authorize_finalize_without_a_real_packet(review):
    svc, adapter, session, _, _ = review
    await read_topic(adapter, session)
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'no-real-packet',
            'args': {'packet_ref': 'invented-packet', 'decisions': [], 'relations_complete': True}})
    assert exc.value.code == 'live_review_packet_unknown'
    assert unchanged_state(svc) == before
