import json

from street_story.service import canonical
from test_live_editor import make_service


def test_partial_research_is_continued_later_at_most_three_times(tmp_path):
    # Owner, 21.10.2026: the Government building kept 3 facts from one source all night after a partial run.
    svc, _adapter, session, _events = make_service(tmp_path)
    sid = session.resource_id
    calls = []
    svc.mutate_facts = lambda story_id, key, body: calls.append((story_id, key, body)) or {}

    def mark(at):
        with svc.store.tx() as db:
            research = json.loads(svc._story_row(db, sid)['research_json'] or '{}')
            research['auto_continue'] = {**(research.get('auto_continue') or {}), 'at': at, 'reason': 'research_agent_original_pending'}
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), sid))

    mark(svc.store.now() + 600)
    svc._schedule_auto_continuations()
    assert calls == []  # not due yet
    for _ in range(4):
        mark(svc.store.now() - 1)
        svc._schedule_auto_continuations()
    assert len(calls) == 3
    assert all(body['action'] == 'research' for _sid, _key, body in calls)
    assert len({key for _sid, key, _body in calls}) == 3
