"""Used own citations need prior delivery; unrelated pages are not a gate."""
import asyncio
import json

import pytest

from test_review_topic_binding import review as review, unchanged_state
from test_live_review_quote_refs import decision
from street_story.live import _forward_committed_output, StreetStoryLiveAdapter
from street_story.service import ConflictError


async def long_packet(review):
    svc, adapter, session, run_id, fact_id = review
    first = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    ref = first['packet_ref']
    with svc.store.tx() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                                       (ref,)).fetchone()[0])
        payload['items'][0]['evidence'][0]['text'] *= 200
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?', (json.dumps(payload), ref))
    session.state.pop('review_quote_packets', None)
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'packet_ref': ref}})
    assert packet['has_more']
    return packet


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['finalize_fact_review', 'repair_research_fact'])
async def test_unrelated_unread_pages_do_not_block_model_owned_operation(review, operation):
    svc, adapter, session, _, fact_id = review
    packet = await long_packet(review)
    args = (decision(packet) if operation == 'finalize_fact_review' else
            {'packet_ref': packet['packet_ref'], 'repairs': [{'fact': 0, 'reason': 'Controlled replacement.',
                'facts': [{'text': 'Controlled replacement.', 'claim_key': 'replacement', 'evidence': [0]}]}]})
    result = await adapter.execute_tool(session, {'name': operation, 'id': 'own-operation', 'args': args})
    assert not result.get('read_required')
    with svc.store.connection() as db:
        if operation == 'repair_research_fact':
            assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 1
            assert db.execute('SELECT eligibility FROM fact_assertions WHERE assertion_id=?',
                              (fact_id,)).fetchone()[0] == 'withheld'
            assert session.state['research_output_pending'] is True
        else:
            assert result['reviewed_assertion_count'] == 1
            assert session.state['research_output_pending'] is False


@pytest.mark.asyncio
async def test_staged_review_is_not_permission_to_speak_completion(review):
    svc, adapter, session, _, _ = review
    packet = await long_packet(review)
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'staged',
        'args': {**decision(packet), 'relations_complete': False}})
    assert staged['review_saved'] and not staged['reviewed_assertion_count']
    delivered = []
    for event in [{'type': 'output_transcript', 'text': 'Everything was checked and saved.'},
                  {'type': 'audio', 'data': 'uncommitted speech'}, {'type': 'turn_complete'}]:
        _forward_committed_output(svc, session, event, delivered.append)
    assert delivered == [{'type': 'turn_complete'}]
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'finish',
        'args': {**decision(packet), 'decisions': []}})
    assert result['reviewed_assertion_count'] == 1
    committed = {'type': 'output_transcript', 'text': 'Review saved.'}
    _forward_committed_output(svc, session, committed, delivered.append)
    assert delivered[-1] == committed


@pytest.mark.asyncio
async def test_retired_parent_scope_returns_only_current_own_replacements_without_writes(review):
    svc, adapter, session, run_id, fact_id = review
    packet = await long_packet(review)
    repaired = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'retire-parent',
        'args': {'packet_ref': packet['packet_ref'], 'repairs': [{'fact': 0, 'reason': 'Controlled replacement.',
                 'facts': [{'text': 'Controlled replacement.', 'claim_key': 'replacement', 'evidence': [0]}]}]}})
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'get_review_packet',
            'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    assert error.value.code == 'live_review_decisions_invalid'
    assert 'not necessarily quarantined' in str(error.value)
    assert all(child in str(error.value) for child in repaired['repairs'][0]['fact_ids'])
    assert unchanged_state(svc) == before


@pytest.mark.asyncio
async def test_shared_host_batch_must_receive_the_used_quote_before_acceptance(review):
    from live_interaction import LiveSocketSessionHost
    svc, adapter, original_session, _, _ = review
    packet = await long_packet(review)
    ref = packet['packet_ref']
    commands, replies = asyncio.Queue(), asyncio.Queue()

    async def provider(*, load_key, reader, on_event):
        await reader.readline()
        on_event({'type': 'ready'})
        while True:
            calls = await commands.get()
            on_event({'type': 'tool_call', 'calls': calls})
            while raw := await reader.readline():
                message = json.loads(raw)
                if message['type'] == 'tool_response':
                    await replies.put([r['response'] for r in message['responses']])
                    break

    host = LiveSocketSessionHost(
        adapter_factory=lambda **hooks: StreetStoryLiveAdapter(svc, hooks['emit'], hooks['write']),
        key_resolver=lambda *_: 'fixture-no-provider', provider_run=provider, ready_timeout_ms=1000)
    started = await host.start(resource_id=original_session.resource_id, actor=None)
    session = host.sessions[started['session_id']]

    async def batch(calls):
        await commands.put(calls)
        return await asyncio.wait_for(replies.get(), timeout=5)

    try:
        before = unchanged_state(svc)
        unread = (await batch([{'id': 'unread', 'name': 'finalize_fact_review',
            'args': decision(packet, quote=packet['items'][0]['passage'])}]))[0]
        assert unread['error']['code'] == 'live_fact_review_evidence_invalid'
        assert unchanged_state(svc) == before
        page = (await batch([{'id': 'first', 'name': 'get_review_packet', 'args': {'packet_ref': ref}}]))[0]['result']
        assert page['has_more']
        # Learn the future label in a different controlled session. This caller
        # still cannot use it until its own provider received that page.
        future = await adapter.execute_tool(original_session, {'name': 'get_review_packet', 'args': page['next_args']})
        assert future['has_more']
        args = decision(future)
        responses = await batch([
            {'id': 'future', 'name': 'get_review_packet', 'args': page['next_args']},
            {'id': 'same-batch-review', 'name': 'finalize_fact_review', 'args': args}])
        assert responses[-1]['error']['code'] == 'live_fact_review_evidence_invalid'
        assert unchanged_state(svc) == before
        assert not session.state.get('review_call_read_snapshots')
        # The next batch may use this exact own fragment, even with other
        # evidence pages unread. Controlled verdicts are not semantic proof.
        result = (await batch([{'id': 'next-batch-review', 'name': 'finalize_fact_review', 'args': args}]))[0]['result']
        assert result['reviewed_assertion_count'] == 1
    finally:
        await host.stop(resource_id=session.resource_id, session_id=session.id, actor=None)
