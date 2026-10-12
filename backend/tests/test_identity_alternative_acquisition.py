"""Closed T contradictions advance acquisition without revoking ready REF."""
import copy
from types import SimpleNamespace

import pytest

from street_story import article_media, identity_discovery, poi_memory
from test_article_source_retention import article
from test_visual_search_continuation import prepared


def plan(status='structural_contradiction'):
    return {'entity_name': 'Old complex', 'wikipedia_queries': [],
        'visual_query': 'Visible facade', 'commons_query': '',
        'selected_wikipedia_page_ids': ['77'],
        'active_planned_queries': ['Alternative street A', 'Alternative street B'],
        'physical_research_priority': {'active_candidate_ids': ['osm:way:8']},
        'accepted_architectural_text': {'decision': 'uncertain',
            'correspondences': [{'status': status}],
            'material_alternatives': [{'candidate_id': 'osm:way:8'}]},
        'source_text_receipt': {'articles': [{'url': 'https://example.com/old'}]},
        'unconfirmed_reference_action': {'contract': 'closed-hypothesis-reference-action-v1',
            'source_urls': ['https://example.com/old']}}


@pytest.mark.parametrize('change', ['historical', 'accepted', 'no_active', 'no_queries', 'no_alternative'])
def test_non_structural_or_unaddressed_decision_does_not_advance_search(change):
    payload = plan()
    assert identity_discovery.alternative_acquisition_due(payload)
    if change == 'historical':
        payload['accepted_architectural_text']['correspondences'][0]['status'] = 'historical_change'
    elif change == 'accepted':
        payload['accepted_architectural_text']['decision'] = 'accepted_architectural_text'
    elif change == 'no_active':
        payload['physical_research_priority']['active_candidate_ids'] = ['osm:way:99']
    elif change == 'no_queries':
        payload['active_planned_queries'] = []
    else:
        payload['accepted_architectural_text']['material_alternatives'] = []
    assert not identity_discovery.alternative_acquisition_due(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('first_page_empty', [False, True])
async def test_alternative_query_and_read_precede_cached_old_gallery(tmp_path, monkeypatch, first_page_empty):
    svc, _, topic, _ = prepared(tmp_path)
    snapshot = svc._identity_snapshot(topic['id'])[0]
    old = article('https://example.com/old')
    payload = plan()
    identity_discovery._retain_article_discovery(svc, snapshot, [{'url': old['url']}],
        receipts=[{'url': old['url'], 'status': 'completed'}], articles=[old],
        search_plan={'payload': payload})
    calls = []

    async def search(service, name, visual, *, story, first_ready=False):
        query = story['_identity_search_query']
        calls.append(('search', query))
        return [{'url': 'https://example.com/' + ('a' if query.endswith('A') else 'b')}]

    async def fetch(service, context, sources, excluded, *, receipts, first_ready=False):
        assert first_ready
        assert [source['url'] for source in sources] != [old['url']]
        url = sources[0]['url']
        calls.append(('read', url))
        receipts.append({'url': url, 'status': 'completed'})
        return [] if first_page_empty and url.endswith('/a') else [article(url, 1)]

    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    result, media = await identity_discovery.recover(svc, snapshot, '', [], set())
    assert calls[:2] == [('search', 'Alternative street A'), ('read', 'https://example.com/a')]
    if first_page_empty:
        assert calls[2:] == [('search', 'Alternative street B'), ('read', 'https://example.com/b')]
    assert media[0]['url'].endswith('/b' if first_page_empty else '/a')
    assert result['status'] == 'uncertain' and result['_article_media_pending']
    history = svc._identity_snapshot(topic['id'])[1]['identity_article_discovery']
    assert history['pages'][old['url']]['candidates'] == [old]
    assert history['search_plan']['payload'] == payload
    assert history['queries']['Alternative street A']['status'] == 'completed'
    # Restart reuses the closed query and acquired alternative without a send.
    count = len(calls)
    await identity_discovery.recover(svc, svc._identity_snapshot(topic['id'])[0], '', [], set())
    assert len(calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize('structural', [False, True])
async def test_ready_poi_media_does_not_block_addressed_alternative_owner(tmp_path, monkeypatch, structural):
    svc, _, topic, _ = prepared(tmp_path)
    svc.providers.research = SimpleNamespace(plan_identity_search=lambda *args: None)
    calls = []
    payload = plan('structural_contradiction' if structural else 'historical_change')

    async def prepare(service, snapshot, transcript, candidates):
        snapshot['_identity_search_plan_payload'] = copy.deepcopy(payload)

    async def recover(service, snapshot, transcript, candidates, excluded):
        assert calls == ['ready_visual']
        assert service._identity_snapshot(topic['id'])[1]['visual_identity']['candidates']
        calls.append('alternative_recovery')
        return None

    monkeypatch.setattr(identity_discovery, 'prepare_search_plan', prepare)
    monkeypatch.setattr(identity_discovery, 'recover', recover)
    monkeypatch.setattr(poi_memory, 'candidate_reference_images', lambda *_: [{'url': 'https://example.com/old.jpg'}])
    monkeypatch.setattr(svc, '_schedule_identity_visual', lambda: calls.append('ready_visual'))
    await svc.resolve_identity(topic['id'])
    assert calls[0] == 'ready_visual'
    assert ('alternative_recovery' in calls) is structural
    assert svc.story(topic['id'])['visual_identity']['status'] == 'uncertain'
