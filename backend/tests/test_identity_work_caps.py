"""Distinct new semantic work shares a durable wave cap; readback is separate."""
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from street_story.article_media import article_candidates
from street_story.errors import PermanentProviderError
from street_story.identity_discovery import _claim_article_query, _retain_article_discovery, suggest
from street_story.research_budget import ensure_budget, reserve_work, ResearchTerminated, ResearchWorkExhausted
from test_visual_search_continuation import prepared


def test_queries_share_cap_across_routes_and_unknown_readback_bypasses_exhausted_envelope(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    svc.settings = replace(svc.settings, identity_max_query_hypotheses=1)
    snapshot = svc._identity_snapshot(story['id'])[0]
    token, result = _claim_article_query(svc, snapshot, 'Observed City Address 1')
    assert token and result['status'] == 'in_progress'
    assert _claim_article_query(svc, snapshot, '  observed city   address 1 ')[0] is None
    assert len(ensure_budget(svc, story['id'])['work_units']['query_hypotheses']) == 1
    token2, deferred = _claim_article_query(svc, snapshot, 'Different address 2')
    assert token2 is None and deferred['status'] == 'deferred'
    assert not json.loads(svc._identity_snapshot(story['id'])[0]['research_json']).get('automatic_research_outcome')
    _retain_article_discovery(svc, snapshot, [], query_results={'Observed City Address 1': {
        'status': 'unknown', 'claim_id': token, 'sources': []}})
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['automatic_research_outcome'] = {'outcome': 'resource_blocked'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    with pytest.raises(ResearchTerminated, match='already_finished'):
        reserve_work(svc, story['id'], 'query_hypotheses', ['observed city address 1'])
    budget = ensure_budget(svc, story['id'])
    svc.store.now = lambda: budget['identity_deadline_at'] + 10
    # A receipt observer is selected before reservation/deadline admission.
    assert _claim_article_query(svc, snapshot, 'Observed City Address 1')[1]['status'] == 'unknown'
    assert len(ensure_budget(svc, story['id'])['work_units']['query_hypotheses']) == 1


@pytest.mark.asyncio
async def test_page_cap_uses_canonical_url_not_gallery_cursor_and_stops_new_http(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    svc.settings = replace(svc.settings, identity_max_pages=1)
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(404)
    async def resolver(host):
        return '93.184.216.34'
    async def forbidden(*args, **kwargs):
        pytest.fail('A missing page has no browser gallery work')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for cursor in (0, 3):
            await article_candidates(svc, story, [{'url': 'https://EXAMPLE.com:443/page#photo',
                'gallery_cursor': cursor}], set(), http=client, resolver=resolver, browser=forbidden)
        receipts = []
        assert await article_candidates(svc, story, [{'url': 'https://example.com/new-page'}], set(),
            http=client, resolver=resolver, browser=forbidden, receipts=receipts) == []
    assert receipts[0]['status'] == 'deferred'
    assert calls == ['/page', '/page']  # Public transport uses the resolved IP.
    assert ensure_budget(svc, story['id'])['work_units']['pages'] == ['https://example.com/page']


@pytest.mark.asyncio
async def test_planner_cap_counts_distinct_frozen_inputs_before_new_inference(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    svc.settings = replace(svc.settings, identity_max_planner_calls=2)
    calls = []
    class Executor:
        async def execute(self, operation, call):
            calls.append(operation)
            return await call('fixture', 5)
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': '', 'commons_query': '', 'article_queries': ['Observed address'], 'first_wave_hypotheses': []}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = svc._identity_snapshot(story['id'])[0]
    await suggest(svc, snapshot, 'First observed context', [])
    with pytest.raises(PermanentProviderError, match='identity_joint_initial_already_closed'):
        await suggest(svc, snapshot, 'New observed context', [])
    # A distinct optional joint input shares the same durable two-unit cap;
    # changing prose alone cannot reopen the closed initial operation.
    reserve_work(svc, story['id'], 'planner_calls', ['fixture-meaningful-followup-input'])
    assert reserve_work(svc, story['id'], 'planner_calls', ['fixture-meaningful-followup-input'])
    with pytest.raises(ResearchWorkExhausted) as exhausted:
        reserve_work(svc, story['id'], 'planner_calls', ['fixture-third-distinct-input'])
    assert exhausted.value.kind == 'planner_calls' and len(calls) == 1


def test_wave_caps_reset_only_for_explicit_owner_wave(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    svc.settings = replace(svc.settings, identity_max_pages=1)
    reserve_work(svc, story['id'], 'pages', ['https://example.com/one'])
    assert reserve_work(svc, story['id'], 'pages', ['https://example.com/one']) == ['https://example.com/one']
    ensure_budget(svc, story['id'], explicit=True)
    assert reserve_work(svc, story['id'], 'pages', ['https://example.com/two']) == ['https://example.com/two']
