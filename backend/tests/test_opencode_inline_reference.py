from street_story.reference_image_codec import normalize_reference
import base64
import copy

import pytest

from direct_visual_fixture import opencode_args
from street_story.opencode_research import ResearchUnavailable
from test_opencode_research import Harness, sheet


@pytest.mark.asyncio
async def test_public_reference_materialized_before_admission_and_both_parts_read_back():
    h = Harness()
    h.result = {'status': 'match'}
    async def load(url):
        assert h.requests == [] and h.admissions == [] and h.sends == []
        return 'image/png', sheet()
    adapter = h.adapter(public_image_loader=load)
    result = await adapter.compare_image(*opencode_args(sheet(), {'request_id': 'inline'}, {'type': 'object'}))
    files = [part for part in h.parts if part['type'] == 'file']
    assert len(files) == 2
    assert all(p['mime'] == 'image/jpeg' and base64.b64decode(p['url'].split(',')[1]) == normalize_reference(sheet())[1] for p in files)
    assert [p['filename'] for p in files] == ['SOURCE', 'REF 1']
    assert h.admissions[0][1]['image_bytes'] == len(normalize_reference(sheet())[1]) * 2
    assert result['receipt']['image_transport'] == 'inline_data_uri_v1'
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_public_fetch_failure_is_known_unsent_before_any_api_or_lease():
    h = Harness()
    async def load(url):
        raise ValueError('HTTP fetch denied')
    with pytest.raises(ResearchUnavailable, match='research_image_reference_unavailable') as caught:
        await h.adapter(public_image_loader=load).compare_image(*opencode_args(sheet(), {'request_id': 'inline'}, {'type': 'object'}))
    assert caught.value.receipt['phase'] == 'failed'
    assert caught.value.receipt['provider_send_state'] == 'not_sent'
    assert h.requests == [] and h.admissions == [] and h.sends == []


@pytest.mark.asyncio
async def test_legacy_submitted_https_is_observed_unchanged_without_new_send_or_fetch():
    h = Harness()
    h.result = {'status': 'mismatch'}
    async def no_fetch(url):
        raise AssertionError('Historical submitted input must remain unchanged')
    adapter = h.adapter(public_image_loader=no_fetch)
    args = opencode_args(sheet(), {'request_id': 'legacy'}, {'type': 'object'})
    # Create only the immutable message identity through a known closed fixture.
    fresh = h.adapter()
    result = await fresh.compare_image(*args)
    original = copy.deepcopy(result['receipt'])
    h.parts[-1]['url'] = 'https://example.org/reference-1.jpg'
    source_file = next(part for part in h.parts if part.get('type') == 'file')
    source_file['mime'] = 'image/png'
    source_file['url'] = 'data:image/png;base64,' + base64.b64encode(sheet()).decode()
    original['image_transport'] = None
    original['binding'].pop('image_preparation', None)
    binding = {**original['binding'], 'session_id': original['session_id'],
               'message_id': original['message_id'], 'phase': 'submitted'}
    sends = len(h.sends)
    result = await adapter.compare_image(args[0], binding, args[2], args[3])
    assert len(h.sends) == sends and result['receipt']['readback_only'] is True
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert 'image_transport' not in result['receipt']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['submitted', 'unknown'])
async def test_inline_submitted_resume_keeps_message_identity_and_original_parts_no_resend(phase):
    h = Harness()
    h.result = {'status': 'match'}
    adapter = h.adapter()
    args = opencode_args(sheet(), {'request_id': 'new-inline'}, {'type': 'object'})
    original = (await adapter.compare_image(*args))['receipt']
    binding = {**original['binding'], 'session_id': original['session_id'],
               'message_id': original['message_id'], 'phase': phase,
               'image_transport': original['image_transport']}
    old_sends, old_admissions = len(h.sends), len(h.admissions)
    async def failed_refetch(url):
        raise AssertionError('Unknown original send cannot be rewritten by current URL fetch')
    adapter.public_image_loader = failed_refetch
    result = await adapter.compare_image(args[0], binding, args[2], args[3])
    assert result['receipt']['message_id'] == original['message_id']
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert result['receipt']['readback_only'] is True
    assert len(h.sends) == old_sends and len(h.admissions) == old_admissions


@pytest.mark.asyncio
async def test_inline_unknown_readback_rejects_foreign_label_without_fetch_or_send():
    h = Harness()
    h.result = {'status': 'match'}
    adapter = h.adapter()
    args = opencode_args(sheet(), {'request_id': 'inline-unknown'}, {'type': 'object'})
    saved = (await adapter.compare_image(*args))['receipt']
    h.parts[-1]['filename'] = 'foreign-reference'
    async def failed_refetch(url):
        raise ValueError('Temporary URL fetch failed')
    adapter.public_image_loader = failed_refetch
    binding = {**saved['binding'], 'session_id': saved['session_id'],
               'message_id': saved['message_id'], 'phase': 'submitted',
               'image_transport': saved['image_transport']}
    old_sends = len(h.sends)
    with pytest.raises(ResearchUnavailable, match='research_image_delivery_unverified') as caught:
        await adapter.compare_image(args[0], binding, args[2], args[3])
    assert len(h.sends) == old_sends
    assert not caught.value.receipt.get('provider_send_state')
    assert not caught.value.receipt.get('retry_safe')
    assert caught.value.receipt['session_id'] == saved['session_id']
    assert caught.value.receipt['message_id'] == saved['message_id']


@pytest.mark.asyncio
async def test_provider_normalized_inline_file_is_verified_without_byte_equality():
    h = Harness()
    h.result = {'status': 'match'}
    original = h.messages
    def messages():
        returned = copy.deepcopy(original())
        for message in returned:
            if message.get('info', {}).get('role') != 'user':
                continue
            for part in message['parts']:
                if part.get('type') == 'file':
                    part['mime'] = 'image/jpeg'
                    part['url'] = 'data:image/jpeg;base64,' + base64.b64encode(b'provider-normalized-RAM').decode()
        return returned
    h.messages = messages
    result = await h.adapter().compare_image(*opencode_args(sheet(), {'request_id': 'normalize'}, {'type': 'object'}))
    assert result['receipt']['phase'] == 'completed'
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert result['receipt']['image_delivery_verification'] == 'original_server_inline_parts_labels_mime'
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_addressed_unknown_missing_original_message_cannot_send_another_prompt():
    h = Harness()
    h.result = {'status': 'match'}
    adapter = h.adapter()
    args = opencode_args(sheet(), {'request_id': 'unknown-missing'}, {'type': 'object'})
    saved = (await adapter.compare_image(*args))['receipt']
    existing = h.messages()
    h.messages = lambda: [message for message in existing if message.get('info', {}).get('role') != 'user']
    async def forbidden(url):
        pytest.fail('Addressed unknown readback must not fetch any REF')
    adapter.public_image_loader = forbidden
    binding = {**saved['binding'], 'session_id': saved['session_id'], 'message_id': saved['message_id'],
               'phase': 'unknown', 'image_transport': saved['image_transport']}
    sends, admissions = len(h.sends), len(h.admissions)
    with pytest.raises(ResearchUnavailable, match='research_submit_outcome_unknown') as caught:
        await adapter.compare_image(args[0], binding, args[2], args[3])
    assert caught.value.receipt['phase'] == 'unknown'
    assert not caught.value.receipt.get('provider_send_state')
    assert len(h.sends) == sends and len(h.admissions) == admissions
