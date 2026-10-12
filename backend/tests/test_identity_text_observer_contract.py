import asyncio
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context as context
from street_story import identity_discovery
from street_story.research_adapter import ProductResearchAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['normal', 'failed_child', 'stop', 'cancel'])
async def test_owned_text_observer_returns_iterable_and_keeps_successes(monkeypatch, terminal):
    from street_story.research_budget import ResearchTerminated
    released = asyncio.Event()
    started, saved, observers = [], [], []
    async def acquire(service, story, candidates, request):
        cid = request['candidate_ids'][0]
        started.append(cid)
        if cid == 'b':
            await released.wait()
            if terminal == 'failed_child':
                raise OSError('closed transport failure')
            if terminal == 'stop':
                raise ResearchTerminated('identity', 'owner_stopped')
        return [{'article_id': cid, 'url': 'https://example.org/' + cid}], {'status': 'completed'}
    monkeypatch.setattr(context, 'acquire_regional_text', acquire)
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery',
                        lambda service, story, sources, **kwargs: saved.extend(kwargs['acquired_text_articles']))
    service = SimpleNamespace(providers=SimpleNamespace(research=SimpleNamespace(retain_search_observer=observers.append)))
    ready, _ = await context.acquire_active_regional_text(service, {}, [], ['a', 'b'])
    assert ready[0]['article_id'] == 'a'
    await asyncio.sleep(0)
    if terminal == 'cancel':
        observers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await observers[0]
    else:
        released.set()
        outcomes = await observers[0]
        assert isinstance(outcomes, list)
    assert saved[0]['article_id'] == 'a'
    assert started == ['a', 'b']
    assert not any(task for task in asyncio.all_tasks() if task is not asyncio.current_task()
                   and not task.done() and 'acquire_active_regional_text' in task.get_coro().__qualname__)


@pytest.mark.asyncio
async def test_observer_callback_reports_closed_failure_without_loop_exception(caplog):
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        adapter = object.__new__(ProductResearchAdapter)
        async def legacy():
            return None
        async def failed():
            raise ValueError('closed diagnostic')
        tasks = [asyncio.create_task(legacy()), asyncio.create_task(failed())]
        for task in tasks:
            adapter.retain_search_observer(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert not errors
        assert 'error_type=ValueError' in caplog.text
        assert not adapter._search_observers
    finally:
        loop.set_exception_handler(previous)
