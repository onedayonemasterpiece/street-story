import asyncio
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.errors import RetryableProviderError
from street_story.research_control import stop_research
from street_story.service import ConflictError
from test_visual_search_continuation import prepared


@pytest.mark.asyncio
async def test_independent_searches_overlap_and_partial_sources_are_saved_before_slow_route(tmp_path):
    svc, _, topic, _ = prepared(tmp_path)
    story = svc._identity_snapshot(topic['id'])[0]
    started, completed, release = set(), asyncio.Event(), asyncio.Event()
    async def opencode(query, snapshot):
        started.add('opencode')
        await release.wait()
        return {'sources': [{'url': 'https://archive.example/general'}], 'source_selection': {'status': 'model_selected'}}
    async def google(query, *, purpose):
        started.add('google')
        await release.wait()
        return SimpleNamespace(grounding_sources=[{'url': 'https://news.example/nearby'}], payload={'source_selection': {'status': 'model_selected'}})
    async def public(query):
        started.add('public')
        completed.set()
        return SimpleNamespace(grounding_sources=[{'url': 'https://official.example/building'}])
    async def select(query, observed, snapshot):
        return {'sources': observed, 'source_selection': {'status': 'model_selected'}}
    svc.providers.research = SimpleNamespace(search_articles=opencode, select_identity_sources=select)
    svc.providers.gemini.discover_article_urls = google
    svc.providers.gemini._public_web_search = public
    task = asyncio.create_task(identity_discovery.web_image_sources(svc, 'Unproved', '', story=story))
    try:
        await asyncio.wait_for(completed.wait(), 2)
        assert started == {'opencode', 'google', 'public'}
        assert not task.done()
        history = svc._identity_snapshot(story['id'])[1]['identity_article_discovery']
        assert history['sources'] == [{'url': 'https://official.example/building'}]
        release.set()
        sources = await task
        assert {s['url'] for s in sources} == {'https://archive.example/general',
            'https://news.example/nearby', 'https://official.example/building'}
        assert len(svc._identity_snapshot(story['id'])[1]['identity_article_discovery']['sources']) == 3
        assert svc.story(story['id'])['place_name'] is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_search_is_not_resent_or_cancelled_and_does_not_discard_other_routes(tmp_path):
    svc, _, topic, _ = prepared(tmp_path)
    story = svc._identity_snapshot(topic['id'])[0]
    calls = []
    async def opencode(query, snapshot):
        calls.append('original-opencode-readback')
        raise RetryableProviderError('research_provider_outcome_unknown')
    async def google(query, *, purpose):
        calls.append('google')
        return SimpleNamespace(grounding_sources=[{'url': 'https://news.example/building'}], payload={'source_selection': {'status': 'model_selected'}})
    async def public(query):
        calls.append('public')
        return SimpleNamespace(grounding_sources=[{'url': 'https://news.example/building'},
            {'url': 'https://official.example/building'}])
    async def select(query, observed, snapshot):
        return {'sources': observed, 'source_selection': {'status': 'model_selected'}}
    svc.providers.research = SimpleNamespace(search_articles=opencode, select_identity_sources=select)
    svc.providers.gemini.discover_article_urls = google
    svc.providers.gemini._public_web_search = public
    sources = await identity_discovery.web_image_sources(svc, 'Unproved', '', story=story)
    assert calls == ['original-opencode-readback', 'google', 'public']
    assert len(sources) == 2


@pytest.mark.asyncio
async def test_late_search_results_cannot_bypass_owner_stop(tmp_path):
    svc, _, topic, _ = prepared(tmp_path)
    story = svc._identity_snapshot(topic['id'])[0]
    async def opencode(query, snapshot):
        stop_research(svc, story['id'], purpose='identity')
        return {'sources': [{'url': 'https://archive.example/general'}], 'source_selection': {'status': 'model_selected'}}
    async def select(query, observed, snapshot):
        return {'sources': observed, 'source_selection': {'status': 'model_selected'}}
    svc.providers.research = SimpleNamespace(search_articles=opencode, select_identity_sources=select)
    with pytest.raises(ConflictError):
        await identity_discovery.web_image_sources(svc, 'Unproved', '', story=story)
    assert not svc._identity_snapshot(story['id'])[1].get('identity_article_discovery')
