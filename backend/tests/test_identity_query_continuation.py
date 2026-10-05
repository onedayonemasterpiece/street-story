"""Autonomous query progression uses durable hypotheses and the same verdict gate."""
import json

import pytest

from street_story import article_media, identity_discovery
from street_story.research_control import stop_research
from street_story.service import ConflictError
from test_article_source_retention import article, images, reject
from test_visual_search_continuation import prepared


def hypotheses(svc, sid):
    candidates = [{'candidate_id': 'osm:way:1', 'name': 'Old gate', 'identity_eligible': True},
                  {'candidate_id': 'wiki:2', 'name': 'Brick tower', 'identity_eligible': True},
                  {'candidate_id': 'web:title', 'name': 'Article headline is not a physical subject'},
                  {'candidate_id': 'wiki:street', 'name': 'Whole street', 'identity_eligible': False}]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (json.dumps({'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), sid))
    return candidates


def test_query_variants_skip_equivalent_completed_queries_and_only_use_physical_hypotheses():
    identity = {'candidates': [
        {'candidate_id': 'osm:way:1', 'name': 'Old gate'},
        {'candidate_id': 'web:1', 'name': 'Unproved article claim'},
        {'candidate_id': 'wiki:street', 'name': 'Whole street', 'identity_eligible': False}]}
    searches = {'  OLD   GATE  ': {'status': 'completed'}}
    following = identity_discovery.next_visual_query(identity, 'Old gate', searches)
    assert following == 'Old gate другие ракурсы фасад вход'
    searches[following] = {'status': 'completed'}
    following = identity_discovery.next_visual_query(identity, 'Old gate', searches)
    assert following == 'Old gate вид сбоку сзади детали здания'
    searches[following] = {'status': 'completed'}
    assert not identity_discovery.next_visual_query(identity, 'Old gate', searches)


def test_query_exploration_has_no_total_search_ceiling_or_suffix_chain():
    identity = {'candidates': [{'candidate_id': f'osm:way:{index}', 'name': f'Physical object {index}'}
                               for index in range(30)]}
    searches = {}
    while query := identity_discovery.next_visual_query(identity, 'Initial visible hypothesis', searches):
        assert query not in searches
        searches[query] = {'status': 'completed'}
    assert len(searches) == 93
    assert all(query.count('другие ракурсы') <= 1 for query in searches)


@pytest.mark.asyncio
async def test_empty_query_continues_to_alternative_then_view_on_reconnect_without_false_identity(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    hypotheses(svc, topic['id'])
    searched = []
    all_sources = [{'url': f'https://example.com/discovered-{index}'} for index in range(85)]
    async def search(service, query, visual_query, **kwargs):
        searched.append(query)
        return all_sources if query.endswith('другие ракурсы фасад вход') else []
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    loaded = images(svc)
    async def fetch(service, topic, sources, excluded, *, receipts):
        url = sources[0]['url']
        receipts.append({'url': url, 'status': 'completed'})
        return [article(url)]
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    for expected in ('Old gate', 'Brick tower'):
        before = len(searched)
        session = sessions()
        reply = await adapter._compare_place_images(session, {})
        assert reply['partial'] and reply['autonomous_continuation'] and not reply.get('exhausted')
        assert len(searched) == before + 1 and searched[-1] == expected
        writes = []
        adapter.write = lambda session, message: writes.append(message)
        adapter._continue_identity(session)
        adapter._continue_identity(session)
        assert len(writes) == 1
    session = sessions()
    reply = await adapter._compare_place_images(session, {})
    assert searched == ['Old gate', 'Brick tower', 'Old gate другие ракурсы фасад вход']
    assert reply['comparison_id'] and len(loaded) == 1
    reject(adapter, session, reply)
    _, research = svc._identity_snapshot(topic['id'])
    assert len(research['visual_search_operation']['sources']) == 85
    assert len(research['identity_article_discovery']['sources']) == 85
    assert research['visual_identity']['status'] == 'uncertain'
    assert not research.get('poi_id')


@pytest.mark.asyncio
async def test_partial_gallery_and_temporary_failure_do_not_count_as_query_exhaustion(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    hypotheses(svc, topic['id'])
    async def forbidden(*args, **kwargs):
        pytest.fail('An unfinished gallery is not an exhausted query')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    story, _ = svc._identity_snapshot(topic['id'])
    session = sessions()
    session.state['visual_comparison'] = {'photo_sha256': story['photo_sha256'], 'generation': 0,
        'queue': [], 'seen_images': [], 'query': 'Old gate', 'query_seed': 'Old gate',
        'searches': {'Old gate': {'status': 'completed'}}, 'web_searched': True,
        'sources': {'https://example.com/gallery': {'source': {'url': 'https://example.com/gallery', 'gallery_cursor': 48},
                    'status': 'partial', 'attempts': 4, 'retry_at': svc.store.now() + 60}},
        'fetch_failures': [], 'browser_budget': {'remaining': 2}}
    reply = await adapter._compare_place_images(session, {})
    assert reply['partial'] and not reply.get('autonomous_continuation') and not reply.get('exhausted')
    saved = svc._identity_snapshot(topic['id'])[1]['visual_search_operation']
    assert saved['query'] == 'Old gate'
    assert saved['sources']['https://example.com/gallery']['source']['gallery_cursor'] == 48


@pytest.mark.asyncio
async def test_stop_fences_an_autonomous_next_query_before_search(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    hypotheses(svc, topic['id'])
    async def search(*args, **kwargs):
        return []
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    assert (await adapter._compare_place_images(sessions(), {}))['autonomous_continuation']
    stop_research(svc, topic['id'], purpose='identity')
    before = svc._identity_snapshot(topic['id'])[1]
    async def forbidden(*args, **kwargs):
        pytest.fail('Stopped continuation cannot search')
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    with pytest.raises(ConflictError):
        await adapter._compare_place_images(sessions(), {})
    assert svc._identity_snapshot(topic['id'])[1] == before


@pytest.mark.asyncio
async def test_persisted_uncertain_observations_and_alternatives_reach_next_search_context(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    candidates = hypotheses(svc, topic['id'])
    candidates[0].update(url='https://example.com/old-gate', reference_image_urls=['https://example.com/front.jpg'])
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (json.dumps({'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), topic['id']))
    images(svc)
    session = sessions()
    reply = await adapter._compare_place_images(session, {})
    observation = 'The arch matches, but the distinctive side tower is not visible in this reference.'
    adapter._record_place_comparison(session, 'uncertain-front', {
        'comparison_id': reply['comparison_id'], 'status': 'uncertain', 'candidate_id': 'osm:way:1',
        'confidence': .6, 'observations': [observation], 'alternative_candidate_ids': ['wiki:2']})
    captured = []
    async def search(service, query, visual_query, **kwargs):
        captured.append(kwargs['story']['_identity_query_context'])
        return []
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    await adapter._find_place_articles(sessions(), {'query': 'Old gate side tower'})
    context = captured[0]
    assert context['last_verdict']['observations'] == [observation]
    assert context['last_verdict']['status'] == 'uncertain'
    assert context['last_verdict']['alternative_candidate_ids'] == ['wiki:2']
    assert context['alternative_candidates'][0]['candidate_id'] == 'wiki:2'
    assert context['recent_verdicts'] == [context['last_verdict']]
    saved = svc._identity_snapshot(topic['id'])[1]['visual_search_operation']['verdict_history']
    assert saved == context['recent_verdicts']
