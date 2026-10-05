from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from street_story.research_adapter import ProductResearchAdapter
from street_story.research_control import resume_research, stop_research
from street_story.service import ConflictError
from test_research_control import fixture


@pytest.mark.asyncio
async def test_admission_wait_cannot_send_old_worker_after_stop_resume(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter._active_binding = ContextVar('test_binding', default=None)
    sends = []
    class Lease:
        async def before_send(self, metadata):
            sends.append(metadata)
        async def finalize(self, metadata, state):
            pass
    @asynccontextmanager
    async def waiting(binding, workload):
        stop_research(service, sid, purpose='identity')
        resume_research(service, sid, purpose='identity')
        yield Lease()
    binding = {'story_id': sid, 'photo_sha256': photo, 'generation': 0,
               'purpose': 'identity', 'control_revision': 0}
    async with adapter.fenced_admission(waiting)(binding, {}) as lease:
        with pytest.raises(ConflictError, match='остановлено'):
            await lease.before_send({})
    assert not sends


def test_search_history_retains_source_reuse_context_without_search_call_ceiling(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    from street_story.service import canonical
    sources = [{'url': f'https://example.org/article/{i}', 'title': f'Article {i}'} for i in range(25)]
    with service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   ('search', 'logical', sid, 'search', canonical({'sources': sources, 'search_calls': [{'query': 'first query'}]}), 1, 1))
    history = adapter.search_history({'id': sid, 'photo_sha256': photo})
    assert len(history['found_sources']) == 25
    assert history['prior_search_queries'] == ['first query']
    assert history['completed_for_scope'] == []
    assert history['omitted_history_items'] == 0


@pytest.mark.asyncio
async def test_completed_native_pixels_reused_after_stop_resume_with_new_queue_id(tmp_path):
    from types import SimpleNamespace
    from street_story.service import canonical
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.primary_vision = SimpleNamespace(available=False)
    calls = []
    class Native:
        available = True
        async def compare_visual(self, snapshot, story, schema, context, binding):
            calls.append(binding)
            receipt = {'phase': 'completed', 'binding': binding, 'result': {'status': 'mismatch'}}
            await adapter.checkpoint(binding, receipt)
            return {'result': receipt['result'], 'receipt': receipt}
    adapter.native_vision = Native()
    adapter.client = None
    story = {'id': sid, 'photo_sha256': photo, '_identity_generation': 0}
    context = {'comparison_id': 'old-lease', 'remaining_illustrations': 3,
               'references': [{'candidate_id': 'wiki:1'}], 'physical_candidates': [{'candidate_id': 'wiki:1'}]}
    first = await adapter.visual_verdict(b'pixels', story, {}, canonical(context))
    stop_research(service, sid, purpose='identity')
    resume_research(service, sid, purpose='identity')
    second = await adapter.visual_verdict(b'pixels', {**story, '_identity_research_control_revision': 2}, {},
                                          canonical({**context, 'comparison_id': 'new-lease', 'remaining_illustrations': 8}))
    assert first['result'] == second['result'] and len(calls) == 1
    await adapter.visual_verdict(b'changed-pixels', story, {}, canonical(context))
    assert len(calls) == 2
