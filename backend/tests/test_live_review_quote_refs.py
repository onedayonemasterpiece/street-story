import json
from types import SimpleNamespace

import pytest

from test_review_topic_binding import review as review, unchanged_state
from street_story.research_budget import PAGE_UNITS, response_units
from street_story.service import ConflictError


def decision(packet, *, quote=None, evidence=0):
    item = packet['items'][0]
    return {'packet_ref': packet['packet_ref'], 'decisions': [{
        'fact': item['fact'], 'verdict': 'supported', 'reason': 'Controlled own-source semantic verdict.',
        'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'claims': [item['text']], 'evidence': [evidence],
        'basis_quotes': [item['quote_ref'] if quote is None else quote]}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}


@pytest.mark.asyncio
async def test_live_issued_quote_address_resolves_to_original_literal_and_keeps_raw_request(review):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    assert response_units('get_review_packet', packet) <= PAGE_UNITS
    assert 'quote_catalog' not in packet
    args = decision(packet)
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review-own-label', 'args': args})
    assert result['reviewed_assertion_count'] == 1 and result['eligible_count'] == 1
    with svc.store.connection() as db:
        row = db.execute('SELECT decisions_json,request_json FROM live_review_packets WHERE packet_ref=?',
                         (packet['packet_ref'],)).fetchone()
        assert json.loads(row['decisions_json'])['0']['basis_quotes'] == [packet['items'][0]['passage']]
        assert json.loads(row['request_json']) == args  # Never rewrite the model's retained request.
    assert args['decisions'][0]['basis_quotes'] == [packet['items'][0]['quote_ref']]


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['foreign_fact', 'other_packet', 'unknown', 'unissued',
                                    'unselected_evidence', 'changed_catalog'])
async def test_live_quote_address_never_repairs_wrong_scope_or_evidence(review, invalid):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id}})
    args = decision(packet)
    if invalid == 'foreign_fact':
        args['decisions'][0]['fact'] = 1  # The literal may be identical; the own assertion address differs.
    elif invalid == 'other_packet':
        replacement = await adapter.execute_tool(session, {'name': 'get_review_packet',
            'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
        args['packet_ref'] = replacement['packet_ref']
    elif invalid == 'unknown':
        args['decisions'][0]['basis_quotes'][0] += 'invented'
    elif invalid == 'unissued':
        from street_story.headless_review_quotes import with_quote_ref
        item = packet['items'][0]
        # A mechanically genuine substring is still not an issued read receipt.
        unissued = with_quote_ref(packet, {**item, 'offset': 1, 'passage': item['passage'][1:]})
        args['decisions'][0]['basis_quotes'] = [unissued['quote_ref']]
    elif invalid == 'unselected_evidence':
        args['decisions'][0]['evidence'] = []
    else:
        session.state['review_quote_packets'][packet['packet_ref']]['items'][0]['passage'] += 'altered'
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'invalid-own-label', 'args': args})
    assert exc.value.code == 'live_fact_review_evidence_invalid'
    assert unchanged_state(svc) == before


@pytest.mark.asyncio
async def test_new_session_must_read_frozen_packet_before_using_its_quote_refs(review):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    other = SimpleNamespace(**vars(session))
    other.state = {key: value for key, value in session.state.items() if key != 'review_quote_packets'}
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(other, {'name': 'finalize_fact_review', 'id': 'unread-reference', 'args': decision(packet)})
    assert exc.value.code == 'live_fact_review_evidence_invalid'
    assert unchanged_state(svc) == before
    reread = await adapter.execute_tool(other, {'name': 'get_review_packet', 'args': {'packet_ref': packet['packet_ref']}})
    assert reread['items'][0]['quote_ref'] == packet['items'][0]['quote_ref']
    result = await adapter.execute_tool(other, {'name': 'finalize_fact_review', 'id': 'reread-reference', 'args': decision(reread)})
    assert result['reviewed_assertion_count'] == 1 and result['eligible_count'] == 1


@pytest.mark.asyncio
async def test_valid_quote_reference_does_not_override_model_insufficient_verdict(review):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    args = decision(packet)
    args['decisions'][0].update(verdict='insufficient', support_complete=False)
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'negative-own-ref', 'args': args})
    assert result['eligible_count'] == 0 and result['withheld_count'] == 1
    with svc.store.connection() as db:
        row = db.execute('SELECT eligibility FROM fact_assertions WHERE assertion_id=?', (fact_id,)).fetchone()
        assert row['eligibility'] == 'withheld'


@pytest.mark.asyncio
async def test_valid_quote_reference_does_not_fill_or_override_semantic_checks(review):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    args = decision(packet)
    args['decisions'][0]['atomic'] = False
    before = unchanged_state(svc)
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'contradictory-own-ref', 'args': args})
    assert exc.value.code == 'live_review_decisions_invalid'
    assert unchanged_state(svc) == before


def test_long_live_quote_slices_fit_the_unchanged_page_limit_without_losing_bytes():
    from street_story.review_packets import bounded_review_slices, REVIEW_CHECKS
    from street_story.headless_review_quotes import with_quote_ref

    page = {'packet_ref': 'p_frozen', 'run_id': 'own-run', 'review_checks': REVIEW_CHECKS,
        'items': [], 'total_facts': 1, 'next_tool': 'finalize_fact_review'}
    text = 'Неизменённый кириллический собственный текст с датой и субъектом. ' * 60
    raw = [{'fact': 0, 'text': 'Самостоятельное утверждение. ' * 10, 'evidence': 0,
        'passage': text[start:start + 900], 'offset': start, 'passage_complete': start + 900 >= len(text),
        'source_url': 'https://example.com/own'} for start in range(0, len(text), 900)]
    slices = bounded_review_slices(page, raw, quote_refs=True)
    assert ''.join(item['passage'] for item in slices) == text
    for cursor, item in enumerate(slices, 1):
        assert item['quote_ref'] == with_quote_ref(page, item)['quote_ref']
        probe = {**page, 'items': [item], 'next_cursor': cursor, 'has_more': True,
            'next_tool': 'get_review_packet', 'next_args': {'packet_ref': page['packet_ref'], 'cursor': cursor}}
        assert response_units('get_review_packet', probe) <= PAGE_UNITS
