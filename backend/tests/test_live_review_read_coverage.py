"""A delivered navigation page cannot stand in for unread own evidence."""
import json
import asyncio
from types import SimpleNamespace

import pytest

from test_review_topic_binding import review as review, unchanged_state
from test_live_review_quote_refs import decision
from street_story.headless_review_quotes import (
    remember_live_layout, remember_live_page, required_live_read, with_quote_ref,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['finalize_fact_review', 'repair_research_fact'])
async def test_partial_own_read_defers_without_writes_then_reuses_same_operation(review, operation):
    svc, adapter, session, run_id, fact_id = review
    first = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    ref = first['packet_ref']
    # Controlled frozen input: actual paging and mutation handlers, no provider.
    with svc.store.tx() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                                       (ref,)).fetchone()[0])
        payload['items'][0]['evidence'][0]['text'] = (
            'Navigation menu. ' * 180 + payload['items'][0]['evidence'][0]['text']
            + ' Retained own context. ' * 140)
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?',
                   (json.dumps(payload), ref))
    session.state.pop('review_quote_packets', None)
    page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'packet_ref': ref}})
    assert page['has_more'] and not page['items'][0]['passage_complete']
    if operation == 'finalize_fact_review':
        args = decision(page)
    else:
        args = {'packet_ref': ref, 'repairs': [{'fact': 0, 'reason': 'Controlled atomic replacement.',
            'facts': [{'text': 'Controlled replacement.', 'claim_key': 'replacement', 'evidence': [0]}]}]}
    before = unchanged_state(svc)
    deferred = await adapter.execute_tool(session, {'name': operation, 'id': 'same-operation', 'args': args})
    assert deferred['read_required'] and deferred['review_saved'] is False
    assert deferred['next_tool'] == 'get_review_packet'
    assert deferred['next_args']['cursor'] == page['next_cursor']
    assert unchanged_state(svc) == before
    assert not session.state.get('invalid_review_attempts')
    from street_story.live import _forward_committed_output
    delivered = []
    for event in [{'type': 'output_transcript', 'text': 'All findings are checked and saved.'},
                  {'type': 'audio', 'data': 'uncommitted speech'}, {'type': 'turn_complete'}]:
        _forward_committed_output(svc, session, event, delivered.append)
    assert delivered == [{'type': 'turn_complete'}]
    assert session.state['research_output_pending'] is True
    # Receiving the tail is insufficient when a middle page was skipped.
    layout = session.state['review_quote_layouts'][ref]
    await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'packet_ref': ref, 'cursor': len(layout) - 1}})
    still_missing = await adapter.execute_tool(session, {'name': operation, 'id': 'same-operation', 'args': args})
    assert still_missing['next_args'] == deferred['next_args']
    assert unchanged_state(svc) == before
    while deferred.get('read_required'):
        await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': deferred['next_args']})
        deferred = await adapter.execute_tool(session, {'name': operation, 'id': 'same-operation', 'args': args})
    with svc.store.connection() as db:
        if operation == 'repair_research_fact':
            assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 1
            assert db.execute('SELECT eligibility FROM fact_assertions WHERE assertion_id=?',
                              (fact_id,)).fetchone()[0] == 'withheld'
        else:
            assert deferred['reviewed_assertion_count'] == 1
    assert session.state['research_output_pending'] is (operation == 'repair_research_fact')
    if operation == 'finalize_fact_review':
        committed = {'type': 'output_transcript', 'text': 'Review saved.'}
        _forward_committed_output(svc, session, committed, delivered.append)
        assert delivered[-1] == committed
    assert svc.story(session.resource_id)['source_available']


def test_own_read_coverage_is_per_fact_and_uses_actual_split_cursor():
    session = SimpleNamespace(state={})
    packet = {'packet_ref': 'p-own'}
    own = 'Immutable own content.'
    payload = {'items': [{'evidence': [{'text': own}]}, {'evidence': [{'text': 'Other evidence.'}]}]}
    slices = [with_quote_ref(packet, {'fact': 0, 'evidence': 0, 'offset': start,
        'passage': own[start:end]}) for start, end in [(0, 5), (5, 9), (9, len(own))]]
    slices.append(with_quote_ref(packet, {'fact': 1, 'evidence': 0, 'offset': 0, 'passage': 'Other evidence.'}))
    remember_live_layout(session, packet, slices)
    remember_live_page(session, {**packet, 'items': [slices[0], slices[2]]})
    assert required_live_read(session, payload, packet['packet_ref'], [0])['next_args']['cursor'] == 1
    remember_live_page(session, {**packet, 'items': [slices[1]]})
    assert required_live_read(session, payload, packet['packet_ref'], [0]) is None
    assert required_live_read(session, payload, packet['packet_ref'], [1])['next_args']['cursor'] == 3


def test_changed_received_slice_cannot_complete_own_read():
    from street_story.service import ConflictError
    session = SimpleNamespace(state={})
    packet = {'packet_ref': 'p-own'}
    item = with_quote_ref(packet, {'fact': 0, 'evidence': 0, 'offset': 0, 'passage': 'Own snapshot.'})
    remember_live_layout(session, packet, [item])
    remember_live_page(session, {**packet, 'items': [item]})
    session.state['review_quote_packets']['p-own']['items'][0]['passage'] = 'Altered snapshot.'
    with pytest.raises(ConflictError, match='Issued own evidence changed'):
        required_live_read(session, {'items': [{'evidence': [{'text': 'Own snapshot.'}]}]}, 'p-own', [0])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['finalize_fact_review', 'repair_research_fact'])
async def test_shared_host_batch_cannot_use_its_own_not_yet_delivered_reads(review, operation):
    from live_interaction import LiveSocketSessionHost
    from street_story.live import StreetStoryLiveAdapter
    svc, adapter, original_session, run_id, fact_id = review
    packet = await adapter.execute_tool(original_session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    ref = packet['packet_ref']
    with svc.store.tx() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                                       (ref,)).fetchone()[0])
        payload['items'][0]['evidence'][0]['text'] += ' Extra retained own context. ' * 180
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?', (json.dumps(payload), ref))
    commands, replies = asyncio.Queue(), asyncio.Queue()

    async def provider(*, load_key, reader, on_event):
        await reader.readline()  # Normal start envelope.
        on_event({'type': 'ready'})
        while True:
            calls = await commands.get()
            on_event({'type': 'tool_call', 'calls': calls})
            while raw := await reader.readline():
                message = json.loads(raw)
                if message['type'] == 'tool_response':
                    await replies.put([r['response']['result'] for r in message['responses']])
                    break

    host = LiveSocketSessionHost(
        adapter_factory=lambda **hooks: StreetStoryLiveAdapter(svc, hooks['emit'], hooks['write']),
        key_resolver=lambda *_: 'fixture-no-provider', provider_run=provider, ready_timeout_ms=1000)
    started = await host.start(resource_id=original_session.resource_id, actor=None)
    live_session = host.sessions[started['session_id']]

    async def batch(calls):
        await commands.put(calls)
        return await asyncio.wait_for(replies.get(), timeout=5)

    try:
        args = (decision(packet, quote=packet['items'][0]['passage'])
                if operation == 'finalize_fact_review' else
                {'packet_ref': ref, 'repairs': [{'fact': 0, 'reason': 'Controlled replacement.',
                    'facts': [{'text': 'Controlled replacement.', 'claim_key': 'replace', 'evidence': [0]}]}]})
        before = unchanged_state(svc)
        # A real Live batch cannot exploit the API's legacy literal compatibility.
        unread = (await batch([{'id': 'unread', 'name': operation, 'args': args}]))[0]
        assert unread['read_required'] and unread['next_args'] == {'packet_ref': ref, 'cursor': 0}
        assert unchanged_state(svc) == before
        page = (await batch([{'id': 'first', 'name': 'get_review_packet', 'args': {'packet_ref': ref}}]))[0]
        assert page['has_more']
        if operation == 'finalize_fact_review':
            args = decision(page)
        layout = live_session.state['review_quote_layouts'][ref]
        calls = [{'id': f'read-{i}', 'name': 'get_review_packet',
                  'args': {'packet_ref': ref, 'cursor': i}} for i in range(page['next_cursor'], len(layout))]
        calls.append({'id': 'same-batch-mutation', 'name': operation, 'args': args})
        outcome = (await batch(calls))[-1]
        assert outcome['read_required'] and not outcome['review_saved']
        assert unchanged_state(svc) == before
        assert not live_session.state.get('review_call_read_snapshots')
        # Only the next provider batch can use those delivered response pages.
        outcome = (await batch([{'id': 'next-batch-mutation', 'name': operation, 'args': args}]))[0]
        assert not outcome.get('read_required')
        with svc.store.connection() as db:
            if operation == 'repair_research_fact':
                assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 1
            else:
                assert outcome['reviewed_assertion_count'] == 1
    finally:
        await host.stop(resource_id=live_session.resource_id, session_id=live_session.id, actor=None)
