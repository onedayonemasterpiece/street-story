import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.fact_conflicts import conflict_scan_items, normalize_model_conflict_records, persist_fact_conflicts
from street_story.service import ConflictError
from test_fact_ledger import fact, persist
from test_live_editor import make_service, mark_identity_ready


def journal_fixture(tmp_path):
    service, adapter, session, _ = make_service(tmp_path)
    sid = session.resource_id
    mark_identity_ready(service, sid)
    left = fact('planned', 'В 2009 году только планировали ремонт.', 'https://left.example/history')
    right = fact('completed', 'В 2013 году ремонт завершён.', 'https://right.example/history')
    for item in (left, right):
        item['sources'][0]['supports'] = [{'kind': 'verified_page_span', 'source_version_id': f'v{n}',
            'page_number': n, 'text': item['text'] + ' ' + 'Полная собственная цитата. ' * 35 + str(n)} for n in range(6)]
    persist(service.store, sid, [left, right], 'original-run', 'original-batch')
    with service.store.connection() as db:
        items = conflict_scan_items([{**dict(row), 'sources': json.loads(row['sources_json'])}
            for row in db.execute('SELECT * FROM facts WHERE story_id=?', (sid,))])
    record = normalize_model_conflict_records(items, {'conflicts': [{
        'left_fact_id': items[0]['fact_id'], 'right_fact_id': items[1]['fact_id'],
        'relation': 'source_disagreement', 'suggested_resolution': 'unresolved',
        'confidence': .8, 'rationale': 'Controlled source-scoped declared disagreement.'}]})
    persist_fact_conflicts(service, sid, 'wiki:77', record, detector='controlled-assessment')
    return service, adapter, session, left, right, record[0]['conflict_id']


@pytest.mark.asyncio
async def test_own_conflict_journal_auth_quotes_resolution_and_reopen(tmp_path):
    service, adapter, session, left, right, conflict_id = journal_fixture(tmp_path)
    sid = session.resource_id
    with service.store.connection() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM fact_assertions WHERE story_id=?', (sid,))]
    app = create_app(settings=service.settings, service=service)
    url = f'/v1/stories/{sid}/fact-conflicts'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://product') as client:
        assert (await client.get(url)).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        assert (await client.get(url + '?cursor=0')).status_code == 422
        assert (await client.get('/v1/stories/foreign-story/fact-conflicts')).status_code == 404
        first = (await client.get(url)).json()
        assert first['read_only'] and not first['has_more'] and len(first['items']) == 1
        entry = first['items'][0]
        assert entry['left']['text'] == left['text'] and entry['right']['text'] == right['text']
        for side in ('left', 'right'):
            supports = entry[side]['sources'][0]['supports']
            assert len(supports) == 6 and len(supports[5]['text']) > 500
            assert supports[5]['source_version_id'] == 'v5' and supports[5]['page_number'] == 5
            assert entry[side]['recorded_revision_digest']
        with service.store.connection() as db:
            assert [tuple(row) for row in db.execute('SELECT * FROM fact_assertions WHERE story_id=?', (sid,))] == before
        await adapter.execute_tool(session, {'name': 'resolve_fact_conflict', 'id': 'decision-1', 'args': {
            'conflict_id': conflict_id, 'resolution': 'both_valid',
            'reason': 'A new controlled model scope decision.', 'confidence': .9}})
        reopened = (await client.get(url)).json()['items'][0]
        assert reopened['resolution'] == 'both_valid' and reopened['arbitration_events'][0]['state'] == 'active'
        assert reopened['left']['sources'] == entry['left']['sources'] and reopened['right']['sources'] == entry['right']['sources']
        # Pagination bounds the response, not total visible history.
        with service.store.tx() as db:
            row = dict(db.execute('SELECT * FROM fact_conflicts WHERE story_id=?', (sid,)).fetchone())
            columns = ','.join(row)
            placeholders = ','.join('?' for _ in row)
            for n in range(25):
                db.execute(f'INSERT INTO fact_conflicts({columns}) VALUES({placeholders})',
                    tuple({**row, 'conflict_id': f'historical-{n}'}[key] for key in row))
        first_page = (await client.get(url)).json()
        second_page = (await client.get(url + '?cursor=' + str(first_page['next_cursor']))).json()
        assert first_page['has_more'] and len(first_page['items']) == 20
        assert not second_page['has_more'] and len(second_page['items']) == 6
        assert len({item['conflict_id'] for item in first_page['items'] + second_page['items']}) == 26


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['photo', 'generation', 'stop', 'control_revision', 'missing_scope'])
async def test_delayed_conflict_decision_cannot_cross_source_stop_or_control(tmp_path, boundary):
    service, adapter, session, _, _, conflict_id = journal_fixture(tmp_path)
    sid = session.resource_id
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row['research_json'])
        if boundary == 'photo':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('different-photo', sid))
        elif boundary == 'generation':
            research['identity_generation'] = 1
        elif boundary in {'stop', 'control_revision'}:
            research['research_controls'] = {'facts': {
                'photo_sha256': row['photo_sha256'], 'identity_generation': 0,
                'stopped': boundary == 'stop',
                'revision': 1}}
        else:
            session.state.pop('research_control_scope')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'resolve_fact_conflict', 'id': 'late-decision', 'args': {
            'conflict_id': conflict_id, 'resolution': 'both_valid', 'reason': 'Delayed decision.', 'confidence': .9}})
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM fact_arbitration_events WHERE story_id=?', (sid,)).fetchone()[0] == 0
        assert db.execute('SELECT final_resolution FROM fact_conflicts WHERE story_id=?', (sid,)).fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM live_commands WHERE story_id=? AND command_id=?',
                          (sid, 'late-decision')).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_addressed_a_b_a_keeps_new_active_receipt_and_command_replay(tmp_path):
    service, adapter, session, _, _, conflict_id = journal_fixture(tmp_path)
    sid = session.resource_id
    for n, resolution in enumerate(['prefer_left', 'prefer_right', 'prefer_left']):
        call = {'name': 'resolve_fact_conflict', 'id': f'decision-{n}', 'args': {
            'conflict_id': conflict_id, 'resolution': resolution,
            'reason': 'Controlled addressed reassessment.', 'confidence': .9}}
        result = await adapter.execute_tool(session, call)
        assert await adapter.execute_tool(session, call) == result
    # A fresh adjacent identical command adds no assessment event either.
    await adapter.execute_tool(session, {**call, 'id': 'adjacent-identical'})
    with service.store.connection() as db:
        rows = list(db.execute('SELECT resolution,state FROM fact_arbitration_events WHERE story_id=? ORDER BY created_at', (sid,)))
        assert len(rows) == 3 and sum(row['state'] == 'active' for row in rows) == 1
        assert [row['resolution'] for row in rows] == ['prefer_left', 'prefer_right', 'prefer_left']
        assert rows[-1]['state'] == 'active'
        assert db.execute("SELECT COUNT(*) FROM live_diagnostics WHERE story_id=? AND source='fact_conflict' "
                          "AND event_type='fact_conflict_arbitrated'", (sid,)).fetchone()[0] == 3
