"""Frozen independent pairs use existing attempts, admission and original readback."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from direct_visual_fixture import visual_args
from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import canonical
from street_story.visual_attachments import visual_operation_unit
from test_research_control import fixture


def ready(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = service
    calls = []
    async def google(snapshot, story, schema, context):
        calls.append(('google', json.loads(context)['comparison_id']))
        return {'result': {'status': 'mismatch'}, 'receipt': {'provider': 'google'}}
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=google)
    class Client:
        endpoint, model_id, provider_id = 'http://127.0.0.1:4097', 'mimo-v2.6-flash-free', 'opencode'
        async def compare_image(self, parts, binding, schema, context):
            calls.append(('opencode', binding['phase'] if 'phase' in binding else 'created',
                          json.loads(context)['comparison_id'], [p['label'] for p in parts]))
            result = {'status': 'mismatch'}
            receipt = {'binding': binding, 'phase': 'completed', 'provider': 'opencode', 'result': result}
            await adapter.checkpoint(binding, receipt)
            return {'result': result, 'receipt': receipt}
    adapter.client = Client()
    async def native(snapshot, story, schema, context, binding):
        calls.append(('native', binding.get('phase', 'created'), binding.get('turn_id')))
        result = {'status': 'uncertain'}
        receipt = {'binding': binding, 'phase': 'completed', 'provider': 'codex_native', 'result': result}
        await adapter.checkpoint(binding, receipt)
        return {'result': result, 'receipt': receipt}
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    service.store.cache_put('research-vision-verification-v1', {
        'model_id': adapter.client.model_id, 'endpoint': adapter.client.endpoint,
        'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}, 3600)
    story = {'id': sid, 'photo_sha256': photo, '_identity_generation': 0}
    return adapter, story, calls


def pair(story, child='parent:reference-a'):
    return visual_args(b'opaque-camera-RAM', story, VERDICT_SCHEMA, canonical({
        'comparison_id': child, 'physical_shortlist': [{'candidate_id': 'wiki:1'}],
        'references': [{'candidate_id': 'wiki:1', 'reference_id': child.split(':')[-1],
                        'source_url': 'https://example.org/ref.jpg'}]}))


async def seed(adapter, args, role, phase, **fields):
    story, context = args[1], args[3]
    unit = canonical(visual_operation_unit(story, json.loads(context)))
    binding, _ = adapter.attempt(story, role, unit)
    receipt = {'binding': binding, 'phase': phase, **fields}
    await adapter.checkpoint(binding, receipt)
    return receipt


def test_only_qualified_lanes_include_native_subject_to_normal_admission(tmp_path):
    adapter, _, _ = ready(tmp_path)
    assert adapter.parallel_visual_routes() == ('google', 'opencode', 'native')
    adapter.primary_vision.available = False
    assert adapter.parallel_visual_routes() == ('opencode', 'native')
    adapter.service.store.cache_put('research-quota-health:opencode:mimo-v2.6-flash-free',
                                   {'category': 'research_provider_quota', 'retry_at': adapter.service.store.now()+60}, 60)
    assert adapter.parallel_visual_routes() == ('native',)


@pytest.mark.asyncio
async def test_google_unknown_blocks_other_route_but_not_independent_child(tmp_path):
    adapter, story, calls = ready(tmp_path)
    original = pair(story)
    await seed(adapter, original, 'vision_google_pair', 'unknown', provider_send_state='possibly_sent')
    with pytest.raises(RetryableProviderError, match='pair_outcome_unknown'):
        await adapter.visual_pair_route('opencode', *original)
    assert not calls
    result = await adapter.visual_pair_route('opencode', *pair(story, 'parent:reference-b'))
    assert result['receipt']['provider'] == 'opencode'
    assert calls == [('opencode', 'created', 'parent:reference-b', ['SOURCE', 'REF 1'])]


@pytest.mark.asyncio
async def test_opencode_original_readback_precedes_new_google_availability(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision', 'submitted', session_id='ses_original', message_id='msg_original',
               image_transport='inline_data_uri_v1')
    args[1]['_visual_pair_resume_only'] = True
    args[1]['_visual_pair_observe_only'] = True
    result = await adapter.visual_pair_route('google', *args)
    assert result['receipt']['provider'] == 'opencode'
    assert calls == [('opencode', 'submitted', 'parent:reference-a', ['SOURCE', 'REF 1'])]
    assert result['receipt']['binding']['session_id'] == 'ses_original'
    assert result['receipt']['binding']['message_id'] == 'msg_original'


@pytest.mark.asyncio
async def test_native_original_turn_reads_even_when_unavailable_after_acceptance(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_native', 'unknown', thread_id='thread_original', turn_id='turn_original')
    adapter.native_vision.available = False
    args[1]['_visual_pair_observe_only'] = True
    result = await adapter.visual_pair_route('opencode', *args)
    assert result['receipt']['provider'] == 'codex_native'
    assert calls == [('native', 'unknown', 'turn_original')]


@pytest.mark.asyncio
async def test_missing_attempt_after_submitted_parent_is_not_a_fresh_send(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    args[1]['_visual_pair_resume_only'] = True
    with pytest.raises(RetryableProviderError, match='pair_dispatch_unknown'):
        await adapter.visual_pair_route('google', *args)
    assert not calls and not adapter.visual_pair_receipts(args[1], args[3])


@pytest.mark.asyncio
async def test_created_attempt_proves_unsent_continuation_is_admitted(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'created')
    args[1]['_visual_pair_resume_only'] = True
    await adapter.visual_pair_route('google', *args)
    assert calls == [('google', 'parent:reference-a')]


@pytest.mark.asyncio
async def test_google_timeout_never_falls_back_or_repeats(tmp_path):
    adapter, story, calls = ready(tmp_path)
    async def timeout(*args):
        calls.append(('google', 'sent'))
        raise TimeoutError
    adapter.primary_vision.compare_visual = timeout
    args = pair(story)
    for route in ('google', 'opencode'):
        with pytest.raises(RetryableProviderError, match='outcome_unknown'):
            await adapter.visual_pair_route(route, *args)
    assert calls == [('google', 'sent')]
    assert adapter.visual_pair_receipts(args[1], args[3])['vision_google_pair']['phase'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('role,route,fields', [
    ('vision_google_pair', 'google', {'provider_send_state': 'not_sent', 'retry_safe': True}),
    ('vision_google_pair', 'google', {'provider_send_state': 'response_closed', 'retry_safe': True}),
    ('vision', 'opencode', {'provider_send_state': 'not_sent', 'retry_safe': True}),
    ('vision', 'opencode', {'provider_status': 400, 'error_type': 'ClosedMalformedResponse'}),
])
async def test_closed_primary_uses_native_once_without_unchanged_repeat(tmp_path, role, route, fields):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    if fields.get('provider_send_state') == 'not_sent':
        fields = {**fields, 'retry_at': adapter.service.store.now()+60}
    await seed(adapter, args, role, 'failed', **fields)
    for _ in range(2):
        result = await adapter.visual_pair_route(route, *args)
        assert result['receipt']['provider'] == 'codex_native'
    assert calls == [('native', 'created', None)]


@pytest.mark.asyncio
async def test_actual_opencode_closed_failure_uses_native_existing_adapter_run(tmp_path):
    adapter, story, calls = ready(tmp_path)
    async def closed(parts, binding, schema, context):
        calls.append(('opencode', 'closed'))
        receipt = {'binding': binding, 'phase': 'failed', 'provider_status': 400}
        await adapter.checkpoint(binding, receipt)
        raise ResearchUnavailable('research_response_invalid', receipt)
    adapter.client.compare_image = closed
    args = pair(story)
    await adapter.visual_pair_route('opencode', *args)
    await adapter.visual_pair_route('opencode', *args)
    assert calls == [('opencode', 'closed'), ('native', 'created', None)]


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,fields', [
    ('session_create_intent', {}),
    ('abort_outcome_unknown', {'session_id': 'ses_original', 'message_id': 'msg_original'}),
    ('aborted', {'abort_acknowledged': False}),
])
async def test_unaddressable_or_unacknowledged_creation_waits(tmp_path, phase, fields):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision', phase, **fields)
    if phase == 'abort_outcome_unknown':
        # Addressed abort recovery is an original readback, not another lane.
        await adapter.visual_pair_route('google', *args)
        assert calls[0][0:2] == ('opencode', 'abort_outcome_unknown')
    else:
        with pytest.raises(RetryableProviderError, match='outcome_unknown'):
            await adapter.visual_pair_route('google', *args)
        assert not calls


@pytest.mark.asyncio
async def test_accepted_observe_only_closes_unsent_without_native_or_primary(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'failed', provider_send_state='not_sent', retry_safe=True)
    args[1]['_visual_pair_observe_only'] = True
    with pytest.raises(PermanentProviderError, match='pair_observation_closed'):
        await adapter.visual_pair_route('google', *args)
    assert not calls


@pytest.mark.asyncio
async def test_closed_native_does_not_get_repeated_on_same_child(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_google_pair', 'failed', provider_send_state='response_closed')
    await seed(adapter, args, 'vision_native', 'failed', provider_send_state='response_closed')
    with pytest.raises(PermanentProviderError, match='pair_routes_closed'):
        await adapter.visual_pair_route('google', *args)
    assert not calls


@pytest.mark.asyncio
async def test_independent_pair_calls_can_progress_together_without_global_lock(tmp_path):
    adapter, story, calls = ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def google(*args):
        calls.append('google')
        entered.set()
        await release.wait()
        return {'result': {'status': 'mismatch'}, 'receipt': {'provider': 'google'}}
    adapter.primary_vision.compare_visual = google
    first = asyncio.create_task(adapter.visual_pair_route('google', *pair(story)))
    await entered.wait()
    second = await adapter.visual_pair_route('opencode', *pair(story, 'parent:reference-b'))
    assert second['receipt']['provider'] == 'opencode' and not first.done()
    release.set()
    await first


@pytest.mark.asyncio
async def test_concurrent_duplicate_child_is_one_dispatch_and_saved_observation(tmp_path):
    adapter, story, calls = ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def google(*args):
        calls.append('google')
        entered.set()
        await release.wait()
        return {'result': {'status': 'mismatch'}, 'receipt': {'provider': 'google'}}
    adapter.primary_vision.compare_visual = google
    args = pair(story)
    first = asyncio.create_task(adapter.visual_pair_route('google', *args))
    await entered.wait()
    second = asyncio.create_task(adapter.visual_pair_route('opencode', *args))
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == ['google'] and results[0]['receipt'] == results[1]['receipt']


@pytest.mark.asyncio
async def test_stop_generation_and_legacy_unknown_fences_still_apply(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    binding = {'story_id': story['id'], 'generation': 0, 'purpose': 'identity'}
    with adapter.service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)', (
            'legacy', 'legacy', story['id'], 'vision', canonical({'binding': binding, 'phase': 'submitted'}), 1, 1))
    with pytest.raises(RetryableProviderError, match='legacy_outcome_unknown'):
        await adapter.visual_pair_route('google', *args)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['vision_native', 'vision'])
async def test_source_ram_expiry_keeps_original_unknown_never_terminal_or_fresh(tmp_path, role):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    fields = {'thread_id': 'thread_original', 'turn_id': 'turn_original'} if role == 'vision_native' else {
        'session_id': 'ses_original', 'message_id': 'msg_original'}
    before = await seed(adapter, args, role, 'submitted', **fields)
    args[1]['_visual_image_parts'][0]['data'] = ''
    args[1]['_visual_pair_resume_only'] = args[1]['_visual_pair_observe_only'] = True
    with pytest.raises(RetryableProviderError, match='pair_source_required'):
        await adapter.visual_pair_route('google', *args)
    assert not calls and adapter.visual_pair_receipts(args[1], args[3])[role] == before


@pytest.mark.asyncio
async def test_completed_observation_requires_no_ram_source_or_new_call(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    saved = await seed(adapter, args, 'vision_native', 'completed', result={'status': 'match'})
    args[1]['_visual_image_parts'][0]['data'] = ''
    args[1]['_visual_pair_observe_only'] = True
    result = await adapter.visual_pair_route('google', *args)
    assert result['receipt'] == saved and result['result']['status'] == 'match' and not calls


@pytest.mark.asyncio
async def test_refetch_failure_during_native_readback_keeps_unknown_receipt(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    before = await seed(adapter, args, 'vision_native', 'unknown', thread_id='thread_original', turn_id='turn_original')
    async def bad_reference(*args):
        calls.append('original-readback-ref-fetch')
        raise PermanentProviderError('native_vision:reference_not_image')
    adapter.native_vision.compare_visual = bad_reference
    with pytest.raises(RetryableProviderError, match='pair_outcome_unknown'):
        await adapter.visual_pair_route('opencode', *args)
    assert calls == ['original-readback-ref-fetch']
    assert adapter.visual_pair_receipts(args[1], args[3])['vision_native'] == before


@pytest.mark.asyncio
async def test_current_resume_epoch_guards_original_binding_without_rebinding_turn(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    original = await seed(adapter, args, 'vision_native', 'unknown', thread_id='thread_original', turn_id='turn_original')
    with adapter.service.store.tx() as db:
        row = adapter.service._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['research_controls'] = {'identity': {'revision': 1, 'stopped': False, 'identity_generation': 0}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        job = db.execute("SELECT id,attempts FROM jobs WHERE story_id=? AND kind='identity_visual'", (story['id'],)).fetchone()
    args[1].update(_identity_research_control_revision=1, _research_job_id=job['id'], _research_job_attempt=job['attempts'])
    result = await adapter.visual_pair_route('google', *args)
    assert result['receipt']['binding']['control_revision'] == original['binding']['control_revision'] == 0
    assert calls == [('native', 'unknown', 'turn_original')]


@pytest.mark.asyncio
async def test_caller_lease_superseded_during_readback_cannot_return_committable_result(tmp_path):
    from street_story.service import ConflictError
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    await seed(adapter, args, 'vision_native', 'unknown', thread_id='thread_original', turn_id='turn_original')
    with adapter.service.store.connection() as db:
        job = db.execute("SELECT id,attempts FROM jobs WHERE story_id=? AND kind='identity_visual'", (story['id'],)).fetchone()
    args[1].update(_research_job_id=job['id'], _research_job_attempt=job['attempts'])
    original = adapter.native_vision.compare_visual
    async def finish_then_supersede(*call_args):
        result = await original(*call_args)
        with adapter.service.store.tx() as db:
            db.execute("UPDATE jobs SET state='cancelled',attempts=attempts+1 WHERE id=?", (job['id'],))
        return result
    adapter.native_vision.compare_visual = finish_then_supersede
    with pytest.raises(ConflictError) as failure:
        await adapter.visual_pair_route('google', *args)
    assert failure.value.code == 'research_worker_superseded'
    assert calls == [('native', 'unknown', 'turn_original')]
    # Provider's authoritative closed observation survives; stale caller may
    # not commit identity. A future owner reads the completed receipt locally.
    assert adapter.visual_pair_receipts(args[1], args[3])['vision_native']['phase'] == 'completed'


@pytest.mark.asyncio
async def test_tied_timestamps_recover_latest_submitted_attempt_not_old_created(tmp_path):
    adapter, story, calls = ready(tmp_path)
    args = pair(story)
    first = await seed(adapter, args, 'vision', 'created')
    latest_binding = {**first['binding'], 'attempt_id': 'rattempt_latest'}
    latest = {'binding': latest_binding, 'phase': 'submitted', 'session_id': 'ses_latest', 'message_id': 'msg_latest'}
    with adapter.service.store.tx() as db:
        old = db.execute('SELECT * FROM research_provider_attempts WHERE attempt_id=?', (first['binding']['attempt_id'],)).fetchone()
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)', (
            latest_binding['attempt_id'], old['logical_id'], story['id'], 'vision', canonical(latest), old['created_at'], old['updated_at']))
    result = await adapter.visual_pair_route('google', *args)
    assert result['receipt']['binding']['attempt_id'] == 'rattempt_latest'
    assert calls == [('opencode', 'submitted', 'parent:reference-a', ['SOURCE', 'REF 1'])]
