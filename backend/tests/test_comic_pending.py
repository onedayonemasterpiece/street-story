"""An accepted server-side album survives polling, reopen and owner continuation."""
import json
import uuid

from street_story import fact_ledger
from street_story.runtime import RuntimeStreetStoryService
from street_story.place_selection import save_draft
from street_story.service import canonical, digest
from test_multiphoto import make_runtime, shot, upload_three


async def test_pending_album_survives_polling_and_same_input_owner_request(tmp_path, monkeypatch):
    edit_ids = []
    monkeypatch.setattr(RuntimeStreetStoryService, '_queue_publication_edits',
                        lambda self, db, story_id, **kwargs: edit_ids.append(kwargs['change_id']))
    svc, sid, _, vp = make_runtime(tmp_path)
    photos = ['source', *upload_three(svc, sid), str(uuid.uuid4())]
    svc.add_story_photo(sid, 'fourth', photos[-1], shot('yellow'), 'image/jpeg')
    svc.order_story_photos(sid, 'order', {'photo_ids': photos})
    notes = [{'notes': ['История дома'], 'arrows': ['']} for _ in photos]
    research = {'caption_image_notes': {'photo_ids': photos, 'fact_ids': ['fact-1'], 'cards': notes}}
    monkeypatch.setattr(fact_ledger, 'eligibility_issues_for_ids', lambda *_: [])
    monkeypatch.setattr(fact_ledger, 'eligible_selected_fact_ids', lambda *_: ['fact-1'])
    monkeypatch.setattr(fact_ledger, 'selected_eligibility_issues', lambda *_: [])
    monkeypatch.setattr(fact_ledger, 'fact_revision_bundle', lambda *_: {'fact-1': 'revision-one'})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET research_json=?,draft_text='Пост готов' WHERE id=?", (canonical(research), sid))
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                   "VALUES(?, 'fact-1', 'История дома', 0.9, 1, 1, '[]')", (sid,))
    body = {'selected_fact_ids': ['fact-1']}
    await svc.request_visual(sid, 'first', body)
    # Legacy saved albums have no attempt field; preserve their original provider keys.
    with svc.store.tx() as db:
        saved = json.loads(svc._story_row(db, sid)['visual_context_json'])
        saved.pop('generation_attempt', None)
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (canonical(saved), sid))
    original_visual, original_status = vp.visual, vp.status
    tunes = {}

    async def visual(payload, key):
        if payload['command']['kind'] == 'tune' and key in tunes:
            return tunes[key]
        reply = await original_visual(payload, key)
        if payload['command']['kind'] == 'tune':
            tunes[key] = reply
        assert sum(not op['done'] for op in vp.operations.values()) <= 3
        return reply

    monkeypatch.setattr(vp, 'visual', visual)
    finished = selection_terminal = False

    async def status(operation):
        if selection_terminal and vp.operations[operation]['done']:
            return {'receipts': [{'operation_id': operation, 'state': 'failed'}]}
        if int(operation.removeprefix('visual-')) > 3 and not finished:
            return {'receipts': [{'operation_id': operation, 'state': 'processing'}]}
        return await original_status(operation)

    monkeypatch.setattr(vp, 'status', status)

    def snapshot():
        with svc.store.connection() as db:
            return json.loads(svc._story_row(db, sid)['visual_context_json'])

    async def advance(count):
        for _ in range(count):
            with svc.store.tx() as db:
                db.execute("UPDATE jobs SET available_at=0 WHERE story_id=? AND kind='visual'", (sid,))
            assert await svc.run_once(claim_kind='visual', claim_story_id=sid)

    # No client requests drive these ordinary server worker passes.
    await advance(11)
    saved = snapshot()
    assert [c.get('asset_ref') for c in saved['comic']['cards']] == ['card-1', 'card-2', 'card-3', None, None]
    for i, card in enumerate(saved['comic']['cards']):
        key = 'ss-vp-visual-' + digest([sid, saved['content_revision'], i, card['photo_id'], card['source_asset_ref']])[:48]
        assert tunes[key]['operation_id'] == card['operation_id']
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    assert job['state'] == 'retry' and job['attempts'] == 11 and job['last_error'] is None
    svc = RuntimeStreetStoryService(svc.settings, svc.providers)
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',lease_until=0 WHERE id=?", (job['id'],))
    svc.recover_jobs()  # Expired claim between healthy polls, then another startup.
    svc.recover_jobs()
    assert svc.story(sid)['state'] == 'visual_processing'
    # A prior release may already have stopped this same saved album.
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job['id'],))
        db.execute("UPDATE stories SET state='needs_review' WHERE id=?", (sid,))
    await svc.request_visual(sid, 'owner-continue', body)
    assert snapshot() == saved
    finished = True
    assert await svc.run_once(claim_kind='visual', claim_story_id=sid)
    reopened = RuntimeStreetStoryService(svc.settings, svc.providers).story(sid)
    assert reopened['state'] == 'ready_to_publish'
    assert [c['position'] for c in reopened['cards']] == list(range(5))
    assert [c['asset_ref'] for c in snapshot()['comic']['cards']] == [f'card-{i}' for i in range(1, 6)]
    assert edit_ids == ['comic:' + saved['content_revision']]
    assert len(vp.operations) == 5
    assert [c['kind'] for c in vp.commands].count('tune') == 5
    assert sum(not op['done'] for op in vp.operations.values()) == 0
    # An explicit fresh generation of the complete album must dispatch new work.
    await svc.request_visual(sid, 'regenerate-complete', body)
    assert snapshot()['generation_attempt'] == 'regenerate-complete'
    assert snapshot()['content_revision'] == saved['content_revision']
    await advance(2)
    assert len(tunes) == 10 and svc.story(sid)['state'] == 'ready_to_publish'
    assert edit_ids[-1] == 'comic:' + saved['content_revision'] + ':regenerate-complete'
    assert len(set(edit_ids)) == 2
    # A real text-only edit must invalidate even a legacy partial album with the same notes.
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing',visual_context_json=? WHERE id=?", (canonical(saved), sid))
    assert save_draft(svc, sid, 'text-only', {'text': 'Новый текст', 'selected_fact_ids': ['fact-1'],
                     'expected_identity_generation': 0})['draft_text'] == 'Новый текст'
    await svc.request_visual(sid, 'text-only-visual', body)
    assert snapshot()['generation_attempt'] == 'text-only-visual'
    assert snapshot()['content_revision'] != saved['content_revision']
    assert not any(c.get('operation_id') for c in snapshot()['comic']['cards'])
    # Changed editorial inputs must not reuse the old album snapshot.
    for key, target, field, value in (
            ('changed-concept', research, 'publication_concept', 'Другой ракурс истории'),
            ('changed-notes', notes[0], 'notes', ['Другой зачин'])):
        changed = snapshot()
        target[field] = value
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), sid))
        await svc.request_visual(sid, key, body)
        assert snapshot()['content_revision'] != changed['content_revision']
        assert not any(c.get('operation_id') for c in snapshot()['comic']['cards'])
    changed = snapshot()
    await svc.request_visual(sid, 'changed-instruction', {**body, 'visual_instruction': 'Чернильный скетч'})
    assert snapshot()['content_revision'] != changed['content_revision']
    assert not any(c.get('operation_id') for c in snapshot()['comic']['cards'])
    await advance(5)
    assert len(tunes) == 15 and svc.story(sid)['state'] == 'ready_to_publish'
    selection_terminal = True
    await svc.request_visual(sid, 'terminal-after-select', body)
    assert await svc.run_once(claim_kind='visual', claim_story_id=sid)
    with svc.store.connection() as db:
        assert svc._story_row(db, sid)['error_code'] == 'visual_generation_failed'
    assert svc.story(sid)['state'] == 'needs_review'
