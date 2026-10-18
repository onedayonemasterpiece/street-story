"""Request accumulation checks with controlled semantic provider outputs."""
import json

import pytest

from test_mvp_research import WIKI_URL, add_voice, service
from street_story.service import InvalidStateError


def add_new_fact(result):
    text = 'История здания связана с послевоенной перестройкой центра города.'
    result['payload']['facts'].append({
        'claim_key': 'postwar-center', 'existing_fact_id': '', 'text': text,
        'confidence': .95, 'source_urls': [WIKI_URL],
    })
    result['grounding_supports'].append({
        'kind': 'google_grounding', 'source_url': WIKI_URL, 'text': text,
    })
    return result


@pytest.mark.asyncio
async def test_repeated_more_advances_request_and_adds_unselected_fact_preserving_author_draft(tmp_path):
    svc, gemini, story = service(tmp_path)
    sid = story['id']
    add_voice(svc, sid, 'voice-1')
    svc.mutate_facts(sid, 'first', {'action': 'research'})
    assert await svc.run_once()
    first = svc.story(sid)
    selected_id = next(f['fact_id'] for f in first['facts'] if f['evidence_supported'])
    draft = 'Согласованный владельцем текст без автоматической правки.'
    with svc.store.tx() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        research['publication_concept'] = 'Концепция владельца'
        research['draft_composed_by'] = 'owner'
        # Live editing does not require the legacy voice research marker.
        research.pop('input_revision', None)
        db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?', (draft, json.dumps(research), sid))
    original = gemini.research_v2
    transcripts = []

    async def additional(*args):
        transcripts.append(args[2])
        return add_new_fact(await original(*args))

    async def forbidden_compose(**kwargs):
        raise AssertionError('Cumulative research must preserve an existing publication draft')

    gemini.research_v2 = additional
    gemini.compose_publication = forbidden_compose
    request = {'action': 'research', 'coverage_goal': 'Исследуй послевоенную историю', 'extraction_scope': 'postwar history'}
    svc.mutate_facts(sid, 'more', request)
    # Replaying the HTTP request must not create another revision or job.
    svc.mutate_facts(sid, 'more', request)
    assert await svc.run_once()
    current = svc.story(sid)
    new = next(f for f in current['facts'] if 'послевоенной' in f['text'])
    assert new['evidence_supported'] and not new['selected']
    assert next(f for f in current['facts'] if f['fact_id'] == selected_id)['selected']
    assert current['draft_text'] == draft
    assert 'Исследуй послевоенную историю' in transcripts[0]
    with svc.store.connection() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert research['publication_concept'] == 'Концепция владельца'
        assert research['draft_composed_by'] == 'owner'
        assert research['fact_request_revision'] == 2
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE kind=?', ('research',)).fetchone()[0] == 2
        latest = db.execute('SELECT goal,extraction_scope FROM research_runs ORDER BY created_at DESC LIMIT 1').fetchone()
        assert dict(latest) == {'goal': 'Исследуй послевоенную историю', 'extraction_scope': 'postwar history'}
        assert db.execute('SELECT COUNT(*) FROM poi_research_facts WHERE text=?', (new['text'],)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_more_joins_running_attempt_then_schedules_one_followup_after_commit(tmp_path):
    svc, gemini, story = service(tmp_path)
    sid = story['id']
    add_voice(svc, sid, 'voice-1')
    original = gemini.research_v2

    async def joined(*args):
        result = await original(*args)
        if gemini.research_calls == 1:
            svc.mutate_facts(sid, 'more-during-call', {'action': 'research', 'coverage_goal': 'Дополнительная история'})
            with svc.store.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('ready','retry','running')").fetchone()[0] == 1
        else:
            result = add_new_fact(result)
        return result

    gemini.research_v2 = joined
    svc.mutate_facts(sid, 'first', {'action': 'research'})
    assert await svc.run_once()
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE state='ready'").fetchone()[0] == 1
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert 'pending_fact_request' not in research and research['fact_request_revision'] == 2
    assert await svc.run_once()
    assert gemini.research_calls == 2
    assert any('послевоенной' in f['text'] for f in svc.story(sid)['facts'])
    assert not await svc.run_once()


@pytest.mark.asyncio
async def test_explicit_fact_research_after_confirmation_works_without_any_voice(tmp_path):
    svc, gemini, story = service(tmp_path)
    sid = story['id']
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps({
            'visual_identity': {'status': 'match', 'candidate_id': 'wiki:1', 'candidate_name': 'Дом Советов'},
        }), sid))
    svc.mutate_facts(sid, 'more-no-microphone', {'action': 'research', 'coverage_goal': 'История здания'})
    assert await svc.run_once()
    assert gemini.transcribe_calls == [] and gemini.research_calls == 1
    assert any(f['evidence_supported'] for f in svc.story(sid)['facts'])


def test_no_voice_and_no_confirmed_identity_keeps_initial_guard(tmp_path):
    svc, _, story = service(tmp_path)
    with pytest.raises(InvalidStateError) as exc:
        svc.mutate_facts(story['id'], 'unconfirmed-no-voice', {'action': 'research'})
    assert exc.value.code == 'research_voice_required'


@pytest.mark.asyncio
async def test_distinct_joined_scopes_remain_queued_and_exact_repeats_coalesce(tmp_path):
    svc, gemini, story = service(tmp_path)
    sid = story['id']
    add_voice(svc, sid, 'voice-1')
    original = gemini.research_v2

    async def joined(*args):
        result = await original(*args)
        if gemini.research_calls == 1:
            for key, goal in [('architecture', 'Architecture'), ('history', 'History'), ('architecture-again', 'Architecture')]:
                svc.mutate_facts(sid, key, {'action': 'research', 'coverage_goal': goal, 'extraction_scope': goal})
        return result

    gemini.research_v2 = joined
    svc.mutate_facts(sid, 'first', {'action': 'research'})
    for _ in range(3):
        assert await svc.run_once()
    assert not await svc.run_once()
    assert gemini.research_calls == 3
    with svc.store.connection() as db:
        assert {row['extraction_scope'] for row in db.execute('SELECT extraction_scope FROM research_runs')} >= {'architecture', 'history'}


@pytest.mark.parametrize('guard', ['generation', 'photo', 'cancelled'])
def test_joined_request_is_discarded_after_binding_change_or_stop(tmp_path, guard):
    svc, _, story = service(tmp_path)
    sid = story['id']
    add_voice(svc, sid, 'voice-1')
    svc.mutate_facts(sid, 'first', {'action': 'research'})
    svc.mutate_facts(sid, 'more', {'action': 'research'})
    with svc.store.tx() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        if guard == 'generation':
            research['identity_generation'] = 1
        elif guard == 'cancelled':
            research['fact_research_cancelled'] = True
        else:
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b' * 64, sid))
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), sid))
        assert not svc._schedule_joined_fact_request(db, sid)
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert 'pending_fact_request' not in research
